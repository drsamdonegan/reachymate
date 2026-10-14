"""Single-owner Reachy Mini media connection."""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import threading
import time
from collections import deque
from typing import Any

import numpy as np

from app.config import Settings
from app.services.system.telemetry import TelemetryBroker

logger = logging.getLogger(__name__)


class AudioStalled(RuntimeError):
    """No samples for two seconds; supervised router must reopen the stream."""


class ZeroAudio(AudioStalled):
    """Three seconds of exact zero input; check TCC before chip recovery."""


class ReachyMediaAdapter:
    """Own the Reachy SDK connection and expose bounded microphone/playback I/O."""

    def __init__(self, *, settings: Settings, telemetry: TelemetryBroker) -> None:
        self.settings = settings
        self.telemetry = telemetry
        self._reachy: Any | None = None
        self._connected = False
        self._recording = False
        self._playing = False
        self._input_bytes = bytearray()
        self._dropped_input_bytes = 0
        self._empty_reads = 0
        self._max_input_bytes = settings.sample_rate_hz * 2 * 2
        self._direct_camera: Any | None = None
        self._direct_input_stream: Any | None = None
        self._direct_output_stream: Any | None = None
        # Preserve roughly five seconds across an occasional blocking camera
        # diagnostic while callbacks continue in PortAudio's real-time thread.
        self._direct_input_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=512)
        self._direct_output_queue: deque[np.ndarray] = deque(maxlen=64)
        self._direct_output_lock = threading.Lock()
        self._far_reference_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=64)
        self._direct_input_device: int | None = None
        self._direct_output_device: int | None = None
        self._output_frames_accepted = 0
        self._output_frames_played = 0
        self._last_output_activity = 0.0
        self._audio_processor: Any | None = None
        self._audio_processing_error: str | None = None
        self._aec_processed_frames = 0
        self._aec_reference_misses = 0
        self._last_sample_at = 0.0
        self._zero_run_seconds = 0.0
        self._output_tap = None
        self._output_reset = None
        self.xvf = None
        self.denoiser = None
        self._fault_injected = None
        self._last_raw_nonzero = 0.0
        self._camera_lock = threading.RLock()

    def set_output_tap(self, tap, reset=None) -> None:
        self._output_tap, self._output_reset = tap, reset

    def inject_fault(self, kind: str) -> None:
        if kind not in {"audio_stall", "zero_audio"}:
            raise ValueError("Unsupported media fault")
        self._fault_injected = kind

    @property
    def reachy(self) -> Any | None:
        return self._reachy

    @property
    def connected(self) -> bool:
        return self._connected and self._reachy is not None

    async def connect(self) -> None:
        if self.connected:
            return

        # GStreamer 1.28's helper scanner can stall on macOS. In-process
        # registry generation is deterministic and cached after the first run.
        os.environ.setdefault("GST_REGISTRY_FORK", "no")
        try:
            from reachy_mini import ReachyMini

            from app.services.reachy.daemon_client import DaemonClient

            daemon = DaemonClient(self.settings.reachy_host, self.settings.reachy_port)
            try:
                await asyncio.to_thread(daemon.wait_ready, self.settings.reachy_connection_timeout_seconds)
            finally:
                daemon.close()

            self._reachy = await asyncio.to_thread(
                ReachyMini,
                host=self.settings.reachy_host,
                port=self.settings.reachy_port,
                connection_mode=self.settings.reachy_connection_mode,
                spawn_daemon=False,
                use_sim=False,
                timeout=self.settings.reachy_connection_timeout_seconds,
                automatic_body_yaw=False,
                media_backend=(
                    "no_media"
                    if self.settings.reachy_media_backend == "lite_direct"
                    else self.settings.reachy_media_backend
                ),
                log_level=self.settings.log_level,
            )
        except Exception:
            self._reachy = None
            self._connected = False
            raise

        self._connected = True
        if self.settings.reachy_media_backend == "lite_direct":
            try:
                await asyncio.to_thread(self._open_direct_devices)
            except Exception:
                await self.disconnect()
                raise
        self.telemetry.publish(
            "reachy_media",
            {
                "event": "connected",
                "backend": self.settings.reachy_media_backend,
            },
        )

    async def disconnect(self) -> None:
        await self.stop()
        await self.stop_playing()
        await asyncio.to_thread(self.release_idle_camera)
        reachy = self._reachy
        self._reachy = None
        self._connected = False
        if reachy is not None:
            try:
                await asyncio.to_thread(reachy.media.close)
            finally:
                await asyncio.to_thread(reachy.client.disconnect)
        self.telemetry.publish("reachy_media", {"event": "disconnected"})

    async def start(self) -> None:
        """Start the microphone stream after the robot connection exists."""

        if self._recording:
            return
        if not self.connected:
            raise RuntimeError("Reachy media is not connected")
        path = getattr(self.settings, "gtcrn_model_path", None)
        if path and self.denoiser is None:
            from app.services.audio.denoise import GtcrnDenoiser

            self.denoiser = await asyncio.to_thread(
                GtcrnDenoiser, path, sample_rate=self.settings.sample_rate_hz
            )
        if self.settings.reachy_media_backend == "lite_direct":
            await asyncio.to_thread(self._start_direct_recording)
        else:
            await asyncio.to_thread(self._reachy.media.start_recording)
        self._recording = True
        self._last_sample_at = time.monotonic()
        self._last_raw_nonzero = self._last_sample_at
        self._zero_run_seconds = 0.0
        self._input_bytes.clear()

    async def stop(self) -> None:
        if self._recording and self._reachy is not None:
            if self.settings.reachy_media_backend == "lite_direct":
                await asyncio.to_thread(self._stop_direct_recording)
            else:
                await asyncio.to_thread(self._reachy.media.stop_recording)
        self._recording = False
        self._input_bytes.clear()

    async def read_chunk(self) -> bytes:
        if not self._recording or self._reachy is None:
            raise RuntimeError("Reachy microphone is not recording")

        chunk_bytes = int(self.settings.sample_rate_hz * (self.settings.wakeword_chunk_ms / 1000.0) * 2)
        while len(self._input_bytes) < chunk_bytes:
            if self.settings.reachy_media_backend == "lite_direct":
                try:
                    sample = await asyncio.to_thread(
                        self._direct_input_queue.get,
                        True,
                        0.25,
                    )
                except queue.Empty:
                    sample = None
            else:
                sample = await asyncio.to_thread(self._reachy.media.get_audio_sample)
            now = time.monotonic()
            if self._fault_injected == "audio_stall":
                sample = None
            elif self._fault_injected == "zero_audio" and sample is not None:
                sample = np.zeros_like(sample)
            if sample is None or not sample.size:
                self._empty_reads += 1
                if now - self._last_sample_at >= 2:
                    raise AudioStalled("Reachy microphone produced no samples for 2 seconds")
                await asyncio.sleep(0.005)
                continue
            self._last_sample_at = now
            if self._fault_injected == "zero_audio":
                self._zero_run_seconds += len(sample) / self.settings.sample_rate_hz
            elif self._direct_input_stream is not None:
                self._zero_run_seconds = now - self._last_raw_nonzero
            elif not np.any(sample):
                self._zero_run_seconds += len(sample) / self.settings.sample_rate_hz
            else:
                self._zero_run_seconds = 0.0
            if self._zero_run_seconds >= 3:
                raise ZeroAudio("Reachy microphone produced exact zero input for 3 seconds")
            mono = _float_audio_to_mono_int16(sample)
            self._input_bytes.extend(mono.tobytes())
            if len(self._input_bytes) > self._max_input_bytes:
                overflow = len(self._input_bytes) - self._max_input_bytes
                del self._input_bytes[:overflow]
                self._dropped_input_bytes += overflow

        result = bytes(self._input_bytes[:chunk_bytes])
        del self._input_bytes[:chunk_bytes]
        if self.denoiser:
            result = await asyncio.to_thread(self.denoiser.process, result)
        return result

    async def restart_streams(self) -> None:
        from app.services.system.tcc import permission_status

        permission = await asyncio.to_thread(permission_status)
        if permission.get("microphone") in {"denied", "restricted"}:
            raise RuntimeError("microphone_tcc_denied_safe_hold")
        zero = self._zero_run_seconds >= 3
        playing = self._playing
        await self.stop()
        await self.stop_playing()
        if zero:
            if self.xvf is None:
                raise RuntimeError("zero_audio_chip_recovery_unavailable")
            await asyncio.to_thread(self.xvf.reboot)
        self._fault_injected = None
        if self.denoiser:
            await asyncio.to_thread(self.denoiser.reset)
        await asyncio.to_thread(self._open_direct_devices)
        await self.start()
        if playing:
            await self.start_playing()

    def get_frame(self) -> np.ndarray | None:
        if self._reachy is None:
            return None
        if self.settings.reachy_media_backend == "lite_direct":
            with self._camera_lock:
                if self._direct_camera is None:
                    self._open_direct_camera()
                ok, frame = self._direct_camera.read()
                return frame if ok else None
        return self._reachy.media.get_frame()

    def release_idle_camera(self) -> None:
        """Power down the direct camera outside explicit access/diagnostic capture."""

        with self._camera_lock:
            if self._direct_camera is not None:
                self._direct_camera.release()
                self._direct_camera = None

    async def start_playing(self) -> None:
        if self._playing:
            return
        if self._reachy is None:
            raise RuntimeError("Reachy media is not connected")
        if self.settings.reachy_media_backend == "lite_direct":
            await asyncio.to_thread(self._start_direct_playing)
        else:
            await asyncio.to_thread(self._reachy.media.start_playing)
        with self._direct_output_lock:
            self._output_frames_accepted = 0
            self._output_frames_played = 0
        self._playing = True

    async def push_audio(self, samples: np.ndarray) -> None:
        if not self._playing or self._reachy is None:
            raise RuntimeError("Reachy playback is not running")
        values = samples.astype(np.float32, copy=False)
        if self.settings.reachy_media_backend == "lite_direct":
            channels = self.output_channels()
            if values.ndim == 1:
                values = np.repeat(values[:, None], channels, axis=1)
            while True:
                with self._direct_output_lock:
                    if len(self._direct_output_queue) < self._direct_output_queue.maxlen:
                        self._direct_output_queue.append(values.copy())
                        self._output_frames_accepted += len(values)
                        break
                await asyncio.sleep(0.005)
        else:
            await asyncio.to_thread(self._reachy.media.push_audio_sample, values)
            self._output_frames_accepted += len(values)

    async def clear_output(self) -> None:
        if self._output_reset:
            self._output_reset()
        if self.settings.reachy_media_backend == "lite_direct":
            with self._direct_output_lock:
                self._direct_output_queue.clear()
                self._last_output_activity = 0.0
            self._drain_far_reference()
            return
        if self._reachy is None or self._reachy.media.audio is None:
            return
        clear = getattr(self._reachy.media.audio, "clear_output_buffer", None)
        if clear is None:
            clear = getattr(self._reachy.media.audio, "clear_player", None)
        if clear is not None:
            await asyncio.to_thread(clear)

    async def stop_playing(self) -> None:
        if self._playing and self._reachy is not None:
            if self.settings.reachy_media_backend == "lite_direct":
                await asyncio.to_thread(self._stop_direct_playing)
            else:
                await asyncio.to_thread(self._reachy.media.stop_playing)
        self._playing = False

    def input_sample_rate(self) -> int:
        if self.settings.reachy_media_backend == "lite_direct":
            return self.settings.sample_rate_hz if self.connected else -1
        if self._reachy is None:
            return -1
        return int(self._reachy.media.get_input_audio_samplerate())

    def output_sample_rate(self) -> int:
        if self.settings.reachy_media_backend == "lite_direct":
            return self.settings.sample_rate_hz if self.connected else -1
        if self._reachy is None:
            return -1
        return int(self._reachy.media.get_output_audio_samplerate())

    def output_channels(self) -> int:
        if self.settings.reachy_media_backend == "lite_direct":
            return 2 if self.connected else -1
        if self._reachy is None:
            return -1
        return int(self._reachy.media.get_output_channels())

    def output_frames_played(self) -> int:
        """Return frames handed to the target output device for truncation accounting."""

        with self._direct_output_lock:
            if self.settings.reachy_media_backend == "lite_direct":
                return self._output_frames_played
            # The upstream media API doesn't expose a playback cursor. Its
            # accepted count is an upper bound; production Mini Lite uses the
            # explicit direct backend where the callback cursor is measured.
            return self._output_frames_accepted

    def output_buffered_frames(self) -> int:
        if self.settings.reachy_media_backend == "lite_direct":
            with self._direct_output_lock:
                return sum(len(chunk) for chunk in self._direct_output_queue)
        return 0

    def output_active(self) -> bool:
        """Return whether non-silent audio was handed to Reachy's speaker recently."""

        with self._direct_output_lock:
            queued = bool(self._direct_output_queue)
            recently_played = (time.monotonic() - self._last_output_activity) < 0.2
        return self._playing and (queued or recently_played)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "recording": self._recording,
            "playing": self._playing,
            "input_rate": self.input_sample_rate(),
            "output_rate": self.output_sample_rate(),
            "output_channels": self.output_channels(),
            "output_frames_accepted": self._output_frames_accepted,
            "output_frames_played": self.output_frames_played(),
            "output_active": self.output_active(),
            "audio_processing_enabled": self._audio_processor is not None,
            "audio_processing_error": self._audio_processing_error,
            "aec_processed_frames": self._aec_processed_frames,
            "aec_reference_misses": self._aec_reference_misses,
            "aec_reference_queue_depth": self._far_reference_queue.qsize(),
            "buffered_input_bytes": len(self._input_bytes),
            "dropped_input_bytes": self._dropped_input_bytes,
            "empty_reads": self._empty_reads,
            "last_sample_age_seconds": None
            if not self._last_sample_at
            else time.monotonic() - self._last_sample_at,
            "zero_run_seconds": self._zero_run_seconds,
            "gtcrn_enabled": self.denoiser is not None,
        }

    def _open_direct_devices(self) -> None:
        import sounddevice as sd

        # CoreAudio reuses indices after USB removal; an old index must never
        # silently select the Mac's microphone or a different output device.
        self._direct_input_device = None
        self._direct_output_device = None
        devices = sd.query_devices()
        for index, device in enumerate(devices):
            name = str(device.get("name", "")).lower()
            if "reachy mini audio" not in name:
                continue
            if int(device.get("max_input_channels", 0)) >= 2:
                self._direct_input_device = index
            if int(device.get("max_output_channels", 0)) >= 2:
                self._direct_output_device = index

        if self._direct_input_device is None or self._direct_output_device is None:
            raise RuntimeError("Reachy Mini USB audio input/output was not found")

    def _open_direct_camera(self) -> None:
        if self._direct_camera is not None:
            return
        import cv2

        backend = cv2.CAP_AVFOUNDATION if os.uname().sysname == "Darwin" else cv2.CAP_ANY
        from app.services.system.tcc import permission_status, resolve_camera

        if permission_status().get("camera") in {"denied", "restricted"}:
            raise RuntimeError("camera_tcc_denied")
        index = (
            resolve_camera(
                name=getattr(self.settings, "reachy_camera_name", "Reachy Mini Camera"),
                unique_id=getattr(self.settings, "reachy_camera_unique_id", None),
            )
            if os.uname().sysname == "Darwin"
            else self.settings.reachy_camera_device_index
        )
        self._direct_camera = cv2.VideoCapture(index, backend)
        if not self._direct_camera.isOpened():
            self._direct_camera.release()
            self._direct_camera = None
            raise RuntimeError("Reachy Mini USB camera could not be opened")
        self._direct_camera.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        self._direct_camera.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
        self._direct_camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
        for _ in range(15):
            self._direct_camera.read()

    def _start_direct_recording(self) -> None:
        import sounddevice as sd

        self._drain_direct_input()
        self._drain_far_reference()
        self._configure_audio_processor()

        def callback(indata, _frames, _time, status) -> None:  # type: ignore[no-untyped-def]
            if status:
                logger.warning("Reachy input status: %s", status)
            raw = np.asarray(indata, dtype=np.float32)
            if np.any(raw):
                self._last_raw_nonzero = time.monotonic()
            values = self._process_direct_input(raw)
            if self._direct_input_queue.full():
                try:
                    self._direct_input_queue.get_nowait()
                    self._dropped_input_bytes += values.nbytes
                except queue.Empty:
                    pass
            try:
                self._direct_input_queue.put_nowait(values)
            except queue.Full:
                self._dropped_input_bytes += values.nbytes

        self._direct_input_stream = sd.InputStream(
            device=self._direct_input_device,
            samplerate=self.settings.sample_rate_hz,
            channels=2,
            dtype="float32",
            blocksize=int(self.settings.sample_rate_hz * 0.01),
            callback=callback,
        )
        self._direct_input_stream.start()

    def _stop_direct_recording(self) -> None:
        if self._direct_input_stream is not None:
            self._direct_input_stream.stop()
            self._direct_input_stream.close()
            self._direct_input_stream = None
        self._drain_direct_input()
        self._drain_far_reference()
        self._audio_processor = None

    def _start_direct_playing(self) -> None:
        import sounddevice as sd

        with self._direct_output_lock:
            self._direct_output_queue.clear()
            self._last_output_activity = 0.0
        self._drain_far_reference()

        def callback(outdata, frames, _time, status) -> None:  # type: ignore[no-untyped-def]
            if status:
                logger.warning("Reachy output status: %s", status)
            outdata.fill(0)
            filled = 0
            with self._direct_output_lock:
                while filled < frames and self._direct_output_queue:
                    values = self._direct_output_queue[0]
                    take = min(frames - filled, len(values))
                    outdata[filled : filled + take] = values[:take]
                    filled += take
                    self._output_frames_played += take
                    if take == len(values):
                        self._direct_output_queue.popleft()
                    else:
                        self._direct_output_queue[0] = values[take:]
                if filled:
                    self._last_output_activity = time.monotonic()
            self._queue_far_reference(outdata)
            if filled and self._output_tap:
                self._output_tap(outdata[:filled], self.settings.sample_rate_hz)

        self._direct_output_stream = sd.OutputStream(
            device=self._direct_output_device,
            samplerate=self.settings.sample_rate_hz,
            channels=2,
            dtype="float32",
            blocksize=int(self.settings.sample_rate_hz * 0.01),
            callback=callback,
        )
        self._direct_output_stream.start()

    def _stop_direct_playing(self) -> None:
        if self._direct_output_stream is not None:
            self._direct_output_stream.stop()
            self._direct_output_stream.close()
            self._direct_output_stream = None
        with self._direct_output_lock:
            self._direct_output_queue.clear()
            self._last_output_activity = 0.0
        self._drain_far_reference()

    def _drain_direct_input(self) -> None:
        while True:
            try:
                self._direct_input_queue.get_nowait()
            except queue.Empty:
                return

    def _configure_audio_processor(self) -> None:
        self._audio_processor = None
        self._audio_processing_error = None
        self._aec_processed_frames = 0
        self._aec_reference_misses = 0
        if not self.settings.audio_processing_enabled:
            return
        try:
            from pywebrtc_audio import AudioProcessor

            self._audio_processor = AudioProcessor(
                sample_rate=self.settings.sample_rate_hz,
                echo_cancellation=True,
                noise_suppression=True,
                # Reachy's microphones already have enough gain. Adaptive gain
                # turns a busy-room noise floor back into apparent speech after
                # suppression, so keep it off for command/VAD reliability.
                auto_gain_control=False,
                ns_level=self.settings.audio_noise_suppression_level,
                stream_delay_ms=self.settings.audio_aec_stream_delay_ms,
            )
        except Exception as exc:
            self._audio_processing_error = f"{type(exc).__name__}: {exc}"
            logger.exception("Reachy audio processing is unavailable")

    def _process_direct_input(self, values: np.ndarray) -> np.ndarray:
        """Downmix microphone input and remove the synchronized speaker reference."""

        near = np.asarray(values, dtype=np.float32)
        if near.ndim == 2:
            channel = getattr(self.settings, "reachy_input_channel", "mean")
            near = (
                near[:, 0] if channel == "left" else near[:, -1] if channel == "right" else near.mean(axis=1)
            )
        near = near.copy()
        if self._audio_processor is None:
            return near
        try:
            far = self._far_reference_queue.get_nowait()
        except queue.Empty:
            far = np.zeros(len(near), dtype=np.float32)
            if self._playing:
                self._aec_reference_misses += 1
        if len(far) != len(near):
            aligned = np.zeros(len(near), dtype=np.float32)
            take = min(len(far), len(near))
            aligned[:take] = far[:take]
            far = aligned
        try:
            cleaned = self._audio_processor.process(near, far)
        except Exception as exc:
            if self._audio_processing_error is None:
                self._audio_processing_error = f"{type(exc).__name__}: {exc}"
                logger.exception("Reachy audio processing failed")
            return near
        self._aec_processed_frames += len(near)
        return np.asarray(cleaned, dtype=np.float32)

    def _queue_far_reference(self, output: np.ndarray) -> None:
        """Queue the exact mono signal handed to the Reachy speaker."""

        far = np.asarray(output, dtype=np.float32)
        if far.ndim == 2:
            far = far.mean(axis=1)
        far = far.copy()
        if self._far_reference_queue.full():
            try:
                self._far_reference_queue.get_nowait()
            except queue.Empty:
                pass
        try:
            self._far_reference_queue.put_nowait(far)
        except queue.Full:
            pass

    def _drain_far_reference(self) -> None:
        while True:
            try:
                self._far_reference_queue.get_nowait()
            except queue.Empty:
                return


def _float_audio_to_mono_int16(sample: np.ndarray) -> np.ndarray:
    values = np.asarray(sample, dtype=np.float32)
    if values.ndim == 2:
        values = values.mean(axis=1)
    values = np.clip(values, -1.0, 1.0)
    return (values * 32767.0).astype(np.int16)
