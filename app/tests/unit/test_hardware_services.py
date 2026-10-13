from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import httpx
import numpy as np
import pytest

from app.scripts.hw.audio_probe import channel_metrics
from app.scripts.hw.doa_survey import bearing_error
from app.services.audio.doa import DoaMonitor, DoaSample
from app.services.audio.xvf import XvfController
from app.services.reachy.daemon_client import DaemonClient, stop_move
from app.services.reachy.motion.manager import MotionManager, MotionMode, clamp_pose
from app.services.reachy.power import RobotPowerManager
from app.services.system.heartbeat import Heartbeat
from app.services.system.supervisor import SupervisedTask


def test_heartbeat_is_measured_and_reading_does_not_renew():
    beat = Heartbeat(interval_seconds=1)
    assert not beat.healthy(now=0)
    beat.beat(now=0)
    assert beat.healthy(now=2.9)
    assert not beat.healthy(now=3)
    beat.fail("io")
    assert not beat.healthy(now=1)


def test_usb_rediscovery_does_not_reuse_a_different_devices_index(settings, monkeypatch):
    import sys

    from app.services.reachy.media import ReachyMediaAdapter
    from app.services.system.telemetry import TelemetryBroker

    media = ReachyMediaAdapter(settings=settings, telemetry=TelemetryBroker())
    media._direct_input_device = media._direct_output_device = 0
    monkeypatch.setitem(
        sys.modules,
        "sounddevice",
        SimpleNamespace(
            query_devices=lambda: [
                {"name": "Built-in Microphone", "max_input_channels": 2, "max_output_channels": 2}
            ]
        ),
    )
    with pytest.raises(RuntimeError, match="USB audio"):
        media._open_direct_devices()
    assert media._direct_input_device is None and media._direct_output_device is None


@pytest.mark.asyncio
async def test_supervision_recovers_an_exception_and_records_restart():
    ready = asyncio.Event()
    calls = 0

    async def worker():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected")
        ready.set()
        await asyncio.Event().wait()

    supervised = SupervisedTask("test", worker, initial_backoff=0.001)
    await supervised.start()
    await asyncio.wait_for(ready.wait(), 1)
    await supervised.stop()
    assert calls == 2
    assert len(supervised.restarts) == 1


def test_loopback_daemon_and_no_origin_and_stop_command():
    pytest.importorskip("reachy_mini")
    with pytest.raises(ValueError):
        DaemonClient("192.168.1.2")
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"mode": "disabled"})

    daemon = DaemonClient(
        client=httpx.Client(base_url="http://127.0.0.1:8000/api", transport=httpx.MockTransport(handle))
    )
    assert daemon.motors() == {"mode": "disabled"}
    assert str(calls[0].url) == "http://127.0.0.1:8000/api/motors/status"
    assert "Origin" not in calls[0].headers
    commands = []
    stop_move(SimpleNamespace(client=SimpleNamespace(send_command=commands.append)))
    assert commands[0].type == "stop_move"
    daemon.close()


def test_doa_ring_geometry_stale_and_majority():
    monitor = DoaMonitor(None)
    for theta in (0.0, np.pi / 2, np.pi):
        assert DoaSample(theta, True, 0).bearing == pytest.approx(np.pi / 2 - theta)
    monitor.samples.extend(DoaSample(0, True, 1) for _ in range(3))
    assert monitor.wake_bearing(now=1.1) == pytest.approx(np.radians(35))
    assert monitor.wake_bearing(now=3) == 0


def test_xvf_verified_profile_and_no_flash_configuration():
    calls = []
    chip = SimpleNamespace(
        read=lambda name: [0, 2, 1, 4] if name == "VERSION" else [0],
        apply_audio_config=lambda cfg, **kwargs: calls.append((cfg, kwargs)) or True,
    )
    doa = SimpleNamespace(
        available=True, respeaker=chip, close=lambda: None, get_DoA=lambda: (np.pi / 2, True)
    )
    controller = XvfController(profile="door_v1", factory=lambda: doa)
    controller.open()
    assert calls[0][1] == {"verify": True}
    assert all(name != "SAVE_CONFIGURATION" for name, _ in calls[0][0])
    assert controller.last_error is None
    controller.close()


def test_channel_metrics_do_not_mistake_identical_channels_for_spatial_audio():
    values = np.arange(160, dtype=np.float32) / 160
    result = channel_metrics(np.column_stack([values, values]))
    assert result["correlation"] == pytest.approx(1)
    assert not result["all_zeros"]
    assert channel_metrics(np.zeros((48000, 2)))["longest_zero_seconds"] == 3
    assert bearing_error([(np.pi / 4, True)] * 10, 45)["correct_side_fraction"] == 1


@pytest.mark.asyncio
async def test_motion_serialises_goto_and_streaming_and_clamps():
    pytest.importorskip("reachy_mini")
    from reachy_mini.utils import create_head_pose
    from reachy_mini.utils.rotation import Rotation

    operations = []
    mini = SimpleNamespace(
        set_target=lambda **kwargs: operations.append(("set", threading.get_ident(), kwargs))
    )
    motion = MotionManager(mini)
    motion.set_tracking_target(create_head_pose(yaw=90, pitch=45, roll=45))
    angles = np.degrees(Rotation.from_matrix(motion.target[:3, :3]).as_euler("xyz"))
    assert angles == pytest.approx([10, 20, 35])
    motion.start()
    motion.set_mode(MotionMode.ENGAGED)

    def goto():
        operations.append(("goto_begin", threading.get_ident(), {}))
        time.sleep(0.06)
        operations.append(("goto_end", threading.get_ident(), {}))

    await motion.execute(goto)
    await asyncio.sleep(0.025)
    await motion.stop()
    names = [name for name, _, _ in operations]
    assert names[names.index("goto_begin") + 1] == "goto_end"
    assert len(set(tid for _, tid, _ in operations)) == 1
    assert all(call[2].get("body_yaw") is None for call in operations if call[0] == "set")
    with pytest.raises(ValueError):
        clamp_pose(np.zeros((3, 3)))


@pytest.mark.asyncio
async def test_power_call_order_and_sag_disable_finally():
    pytest.importorskip("reachy_mini")
    from reachy_mini.reachy_mini import SLEEP_HEAD_POSE

    calls = []
    mini = SimpleNamespace(
        goto_sleep=lambda: calls.append("sleep"), disable_motors=lambda: calls.append("disable")
    )
    state = {"head_joints": [0.0] * 7, "head_pose": {"m": SLEEP_HEAD_POSE.reshape(-1).tolist()}}
    daemon = SimpleNamespace(state=lambda: state, motors=lambda: {"mode": "disabled"})
    motion = MotionManager(mini)
    power = RobotPowerManager(motion, daemon, sag_delay=0)
    power._park()
    assert calls == ["sleep", "disable"]
    bad = SimpleNamespace(
        goto_sleep=lambda: (_ for _ in ()).throw(RuntimeError("broken")),
        disable_motors=lambda: calls.append("disabled_after_error"),
    )
    with pytest.raises(RuntimeError):
        RobotPowerManager(MotionManager(bad), daemon, sag_delay=0)._park()
    assert calls[-1] == "disabled_after_error"


def test_stereo_corpus_channel_selection_is_explicit(tmp_path):
    import wave

    from app.services.audio.evaluation import read_pcm16_mono

    path = tmp_path / "stereo.wav"
    samples = np.column_stack([np.full(160, 100, dtype=np.int16), np.full(160, 300, dtype=np.int16)])
    with wave.open(str(path), "wb") as target:
        target.setnchannels(2)
        target.setsampwidth(2)
        target.setframerate(16000)
        target.writeframes(samples.tobytes())
    assert np.all(read_pcm16_mono(path, sample_rate=16000, input_channel="left") == 100)
    assert np.all(read_pcm16_mono(path, sample_rate=16000, input_channel="right") == 300)
    assert np.all(read_pcm16_mono(path, sample_rate=16000, input_channel="mean") == 200)


def test_pilot_feature_evaluation_cannot_pass_empty_or_missing_noise():
    from app.scripts.evaluate_av_gate import evaluate_features

    assert not evaluate_features([])["passed"]
    report = evaluate_features(
        [{"session": "one", "noise_dba": 55, "time_s": 1, "label": "bystander", "vad": 1}]
    )
    assert not report["passed"]
    assert report["noise_levels"]["55.0"]["bystander_false_open_fraction"] == 0


def test_recorder_requires_private_new_wav_path(tmp_path):
    from app.scripts.record_consented_session import validate_recording_path

    with pytest.raises(ValueError):
        validate_recording_path(tmp_path / "outside.wav")


@pytest.mark.asyncio
async def test_router_reopens_stalled_stream_with_measured_heartbeat(settings):
    from app.services.audio.audio_router import AudioRouter
    from app.services.audio.barge_in_guard import BargeInGuard
    from app.services.reachy.media import AudioStalled
    from app.services.system.telemetry import TelemetryBroker

    ready = asyncio.Event()

    class Microphone:
        starts = 0

        async def start(self):
            self.starts += 1

        async def stop(self):
            pass

        async def read_chunk(self):
            if self.starts == 1:
                raise AudioStalled("injected")
            ready.set()
            await asyncio.Event().wait()

    mic = Microphone()
    router = AudioRouter(
        settings=settings,
        microphone=mic,
        vad=SimpleNamespace(score=lambda _: 0),
        wakeword=SimpleNamespace(score=lambda _: 0),
        asr=SimpleNamespace(recognize=lambda _: None),
        guard=BargeInGuard(0),
        telemetry=TelemetryBroker(),
        robot_is_speaking=lambda: False,
        face_present_supplier=lambda: False,
        on_wake=lambda: asyncio.sleep(0),
        on_command=lambda _: asyncio.sleep(0),
    )
    router._supervisor.initial_backoff = 0.001
    await router.start()
    try:
        await asyncio.wait_for(ready.wait(), 1)
        assert mic.starts == 2
        assert router.diagnostics()["restarts"] == 1
        assert not router.heartbeat.healthy()
    finally:
        await router.stop()


def test_probe_does_not_report_control_reopen_or_partial_audio_as_recovered():
    from app.scripts.hw.audio_probe import capture_complete
    from app.scripts.hw.xvf_probe import recovery_complete

    assert not capture_complete({"frames": 68960, "all_zeros": False}, 10)
    assert capture_complete({"frames": 160000, "all_zeros": False}, 10)
    kwargs = dict(usb_present=True, audio_present=True, frames=16000, nonzero_frames=100, status_flags=0)
    assert recovery_complete(**kwargs)
    assert not recovery_complete(**{**kwargs, "usb_present": False})
    assert not recovery_complete(**{**kwargs, "audio_present": False})
    assert not recovery_complete(**{**kwargs, "nonzero_frames": 0})
    assert not recovery_complete(**{**kwargs, "frames": 1000})


@pytest.mark.asyncio
async def test_transient_audio_restart_error_does_not_enter_safe_hold(settings):
    from app.services.audio.audio_router import AudioRouter
    from app.services.audio.barge_in_guard import BargeInGuard
    from app.services.system.telemetry import TelemetryBroker

    faults = []

    async def restart():
        raise RuntimeError("temporary stream busy")

    async def fault(reason):
        faults.append(reason)

    router = AudioRouter(
        settings=settings,
        microphone=SimpleNamespace(restart_streams=restart),
        vad=None,
        wakeword=None,
        asr=None,
        guard=BargeInGuard(0),
        telemetry=TelemetryBroker(),
        robot_is_speaking=lambda: False,
        face_present_supplier=lambda: False,
        on_wake=lambda: asyncio.sleep(0),
        on_command=lambda _: asyncio.sleep(0),
        on_fault=fault,
    )
    await router._recover("AudioStalled")
    assert not faults
    await router._recover("restart_limit")
    assert faults == ["restart_limit"]


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [True, False])
async def test_local_barge_in_requires_gated_speech_and_notice_permission(settings, allowed):
    from app.services.audio.audio_router import AudioRouter
    from app.services.audio.barge_in_guard import BargeInGuard
    from app.services.system.telemetry import TelemetryBroker
    from app.services.vision.attention import AttentionObservation
    from app.services.vision.av_gate import AVGate

    done = asyncio.Event()
    cancelled = []

    class Microphone:
        reads = 0

        async def start(self):
            pass

        async def stop(self):
            pass

        async def read_chunk(self):
            self.reads += 1
            if self.reads > 3:
                done.set()
                await asyncio.Event().wait()
            return np.full(1280, 100, dtype=np.int16).tobytes()

    async def cancel():
        cancelled.append(True)

    guard = BargeInGuard(0)
    guard.begin_engagement(10)
    router = AudioRouter(
        settings=settings,
        microphone=Microphone(),
        vad=SimpleNamespace(score=lambda _: 1),
        wakeword=None,
        asr=None,
        guard=guard,
        telemetry=TelemetryBroker(),
        robot_is_speaking=lambda: True,
        face_present_supplier=lambda: True,
        on_wake=lambda: asyncio.sleep(0),
        on_command=lambda _: asyncio.sleep(0),
        av_gate=AVGate(attack_seconds=0),
        attention_supplier=lambda: AttentionObservation(1, (0, 0, 150, 150), 0, time.monotonic()),
        on_barge_in=cancel,
        barge_in_allowed=lambda: allowed,
    )
    await router.start()
    try:
        await asyncio.wait_for(done.wait(), 1)
        await asyncio.sleep(0.01)
    finally:
        await router.stop()
    assert cancelled == ([True] if allowed else [])


def test_optional_gtcrn_never_falls_back_to_raw_audio_during_warmup():
    from app.services.audio.denoise import GtcrnDenoiser

    model = SimpleNamespace(
        sample_rate=16000, run=lambda samples, sr: SimpleNamespace(samples=[]), reset=lambda: None
    )
    denoiser = GtcrnDenoiser("unused", model=model)
    raw = np.full(1280, 1000, dtype=np.int16).tobytes()
    assert denoiser.process(raw) == bytes(len(raw))
    model.run = lambda samples, sr: SimpleNamespace(samples=np.full(1280, 0.01, dtype=np.float32))
    assert np.all(np.frombuffer(denoiser.process(raw), dtype=np.int16) == 327)
    denoiser.reset()
    assert not len(denoiser.pending)
