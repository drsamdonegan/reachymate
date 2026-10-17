"""Current server-to-server OpenAI Realtime WebSocket voice integration."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import random
import re
import secrets
import time
from collections import deque
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol
from urllib.parse import urlencode

import numpy as np
import soxr

from app.config import Settings
from app.services.conversation.arbiter import SpeechArbiter
from app.services.knowledge import PublicKnowledgeClient, PublicKnowledgeError
from app.services.realtime.playback import PlaybackWriter
from app.services.realtime.prompt import AI_DISCLOSURE, ROO_LOBBY_INSTRUCTIONS, ROO_LOBBY_PROMPT_VERSION
from app.services.realtime.tools import ToolRegistry, tool_schemas
from app.services.system.telemetry import TelemetryBroker

logger = logging.getLogger(__name__)

OPENAI_AUDIO_RATE = 24_000
PUBLIC_KNOWLEDGE_TOOL = tool_schemas()[0]
DOOR_CLAIM = re.compile(
    r"\b(?:door(?:'s| is| has been)? (?:open(?:ed)?|unlocked)|(?:opened|unlocked) (?:the |your )?door)\b",
    re.I,
)


class RealtimeConnection(Protocol):
    async def send(self, message: str) -> None: ...
    async def recv(self) -> str | bytes: ...
    async def close(self) -> None: ...


class PlaybackSink(Protocol):
    async def start_playing(self) -> None: ...
    async def stop_playing(self) -> None: ...
    async def push_audio(self, samples: np.ndarray) -> None: ...
    async def clear_output(self) -> None: ...
    def output_sample_rate(self) -> int: ...
    def output_frames_played(self) -> int: ...
    def output_active(self) -> bool: ...


ConnectionFactory = Callable[..., AbstractAsyncContextManager[RealtimeConnection]]


def build_session_update(settings: Settings) -> dict[str, Any]:
    """Build the current Realtime session.update event."""

    session: dict[str, Any] = {
        "type": "realtime",
        "model": settings.openai_realtime_model,
        "output_modalities": ["audio"],
        "instructions": ROO_LOBBY_INSTRUCTIONS,
        "audio": {
            "input": {
                "format": {"type": "audio/pcm", "rate": OPENAI_AUDIO_RATE},
                "noise_reduction": {"type": "far_field"},
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": settings.realtime_vad_threshold,
                    "prefix_padding_ms": settings.realtime_vad_prefix_padding_ms,
                    "silence_duration_ms": settings.realtime_vad_silence_duration_ms,
                    "interrupt_response": not settings.local_barge_in_enabled,
                    "create_response": True,
                },
            },
            "output": {
                "format": {"type": "audio/pcm", "rate": OPENAI_AUDIO_RATE},
                "voice": settings.openai_realtime_voice,
            },
        },
        "tools": tool_schemas(),
        "tool_choice": "auto",
        "truncation": {"type": "retention_ratio", "retention_ratio": 0.8},
    }
    effort = settings.openai_realtime_reasoning_effort.strip()
    if effort and settings.openai_realtime_model.startswith("gpt-realtime-2"):
        session["reasoning"] = {"effort": effort}
    return {"type": "session.update", "session": session}


class OpenAIRealtimeSession:
    """One wake-scoped, bounded, interruptible speech-to-speech session."""

    def __init__(
        self,
        *,
        settings: Settings,
        playback: PlaybackSink,
        knowledge: PublicKnowledgeClient,
        telemetry: TelemetryBroker,
        connection_factory: ConnectionFactory | None = None,
        on_closed: Callable[[], Any] | None = None,
        on_activity: Callable[[], Any] | None = None,
        on_fallback: Callable[[], Any] | None = None,
        tools: ToolRegistry | None = None,
    ) -> None:
        self.settings = settings
        self.playback = playback
        self.knowledge = knowledge
        self.telemetry = telemetry
        self.connection_factory = connection_factory or _default_connection_factory
        self.on_closed = on_closed
        self.on_activity = on_activity
        self.on_fallback = on_fallback
        self.tools = tools or ToolRegistry(session_limit=getattr(settings, "realtime_tool_limit", 20))
        self.tools.set_handler("search_public_knowledge", self._search_knowledge)

        self.connection: RealtimeConnection | None = None
        self._connection_context: AbstractAsyncContextManager[RealtimeConnection] | None = None
        self._input_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=settings.realtime_input_queue_chunks)
        self._tasks: set[asyncio.Task[Any]] = set()
        self._send_lock = asyncio.Lock()
        self._running = False
        self._input_enabled = False
        self._started_at = 0.0
        self._last_activity = 0.0
        self._input_resampler = soxr.ResampleStream(
            settings.sample_rate_hz,
            OPENAI_AUDIO_RATE,
            1,
            dtype="int16",
        )
        self._output_resampler: soxr.ResampleStream | None = None
        self._active_output_item_id: str | None = None
        self._active_output_content_index = 0
        self._active_output_start_frames = 0
        self._active_output_received_frames = 0
        self._handled_tool_calls: set[str] = set()
        self._dropped_input_chunks = 0
        self._last_error: str | None = None
        self._last_error_detail: dict[str, str] | None = None
        self._configured = asyncio.Event()
        self._greeting_pending = False
        self.idle_timeout_paused = lambda: False
        self._writer = PlaybackWriter(
            playback, max_chunks=getattr(settings, "realtime_output_queue_chunks", 64)
        )
        self._response_finished = asyncio.Event()
        self._response_finished.set()
        self._response_active = False
        self._active_response_id: str | None = None
        self._interrupted_responses: set[str] = set()
        self._interrupted_items = self._writer.interrupted
        self._output_transcript = ""
        self._authorized_door_claim = False
        self._context_codes: deque[str] = deque(maxlen=30)
        self._failures: deque[float] = deque()
        self._cloud_disabled_until = 0.0
        self._reconnect_attempted = False
        self._reconnecting = False
        self._input_suspended = False
        self._local_playback_active = False
        self._ephemeral_safety_key = secrets.token_bytes(32)
        self._arbiter = SpeechArbiter(
            wait_response=self.wait_response_finished,
            interrupt=self.cancel_response,
            speak=self._announce_out_of_band,
            response_active=lambda: (
                self._response_active or self._writer.busy or self.playback.output_active()
            ),
        )

    @property
    def running(self) -> bool:
        return self._running

    @property
    def registry(self) -> ToolRegistry:
        return self.tools

    async def start(self) -> None:
        if self._running:
            return
        if time.monotonic() < self._cloud_disabled_until:
            raise RuntimeError("Realtime circuit breaker is open")
        key = _secret_value(self.settings.openai_api_key)
        if not key:
            raise RuntimeError("OPENAI_API_KEY is required when Realtime voice is enabled")
        if not self.playback.output_sample_rate() > 0:
            raise RuntimeError("Reachy playback is not ready")

        url = (
            f"{self.settings.openai_realtime_url}?{urlencode({'model': self.settings.openai_realtime_model})}"
        )
        safety_key = _secret_value(getattr(self.settings, "realtime_safety_identifier_key", None))
        if not safety_key and self.settings.is_production:
            raise RuntimeError("A keyed Realtime safety identifier is required")
        safety_id = safety_identifier(self.settings.device_id, safety_key or self._ephemeral_safety_key)
        headers = {
            "Authorization": f"Bearer {key}",
            "OpenAI-Safety-Identifier": safety_id,
        }
        self._connection_context = self.connection_factory(
            url,
            additional_headers=headers,
            open_timeout=self.settings.realtime_open_timeout_seconds,
            max_queue=32,
        )
        try:
            self._configured.clear()
            self._last_error = None
            self._last_error_detail = None
            self._input_enabled = False
            self._input_suspended = False
            self._local_playback_active = False
            self._active_response_id = None
            self._interrupted_responses.clear()
            self._handled_tool_calls.clear()
            self._dropped_input_chunks = 0
            self.tools.reset()
            self._context_codes.clear()
            self._authorized_door_claim = False
            self._output_transcript = ""
            self._reconnect_attempted = False
            self._arbiter.reopen()
            self.connection = await self._connection_context.__aenter__()
            await self.playback.start_playing()
            self._output_resampler = soxr.ResampleStream(
                OPENAI_AUDIO_RATE,
                self.playback.output_sample_rate(),
                1,
                dtype="int16",
            )
            self._running = True
            self._started_at = self._last_activity = time.monotonic()
            self._spawn(self._receive_loop(), "realtime-events")
            await self._send_event(build_session_update(self.settings))
            await asyncio.wait_for(
                self._configured.wait(),
                timeout=self.settings.realtime_open_timeout_seconds,
            )
            if self._last_error is not None or not self._running:
                raise RuntimeError("OpenAI rejected the Realtime session configuration")
            self._spawn(self._send_audio_loop(), "realtime-audio-send")
            self._spawn(self._watchdog(), "realtime-watchdog")
            self.telemetry.publish(
                "realtime",
                {
                    "event": "connected",
                    "model": self.settings.openai_realtime_model,
                    "prompt_version": ROO_LOBBY_PROMPT_VERSION,
                },
            )
        except Exception:
            self._record_failure()
            await self.stop(reason="startup_failed")
            raise

    async def stop(self, *, reason: str = "requested") -> None:
        if not self._running and self.connection is None and self._connection_context is None:
            return
        self._running = False
        self._input_enabled = False
        self._arbiter.close()
        self._response_active = False
        self._response_finished.set()
        await self.tools.close()
        current = asyncio.current_task()
        tasks = tuple(task for task in self._tasks if task is not current)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

        await self._writer.stop()
        await self.playback.stop_playing()
        connection = self.connection
        self.connection = None
        if connection is not None:
            try:
                await connection.close()
            except Exception:
                logger.debug("Realtime connection close failed", exc_info=True)
        context = self._connection_context
        self._connection_context = None
        if context is not None:
            try:
                await context.__aexit__(None, None, None)
            except Exception:
                logger.debug("Realtime connection context close failed", exc_info=True)

        self._reset_audio_state()
        self.telemetry.publish("realtime", {"event": "disconnected", "reason": reason})
        if self.on_closed is not None:
            result = self.on_closed()
            if asyncio.iscoroutine(result):
                await result

    def feed_audio(self, pcm16_mono: bytes) -> bool:
        """Queue one local PCM16 chunk without blocking the capture loop."""

        if not self._running or not self._input_enabled or self._input_suspended or not pcm16_mono:
            return False
        source = np.frombuffer(pcm16_mono, dtype=np.int16)
        if source.size == 0:
            return False
        converted = self._input_resampler.resample_chunk(source, last=False)
        if converted.size == 0:
            return True
        payload = converted.astype(np.int16, copy=False).tobytes()
        if not np.any(source):
            # PCM resamplers may add one-bit dither even to exact silence.
            # A closed visitor gate must remain exact silence on the wire.
            payload = bytes(len(payload))
        if self._input_queue.full():
            try:
                self._input_queue.get_nowait()
                self._dropped_input_chunks += 1
            except asyncio.QueueEmpty:
                pass
        try:
            self._input_queue.put_nowait(payload)
            return True
        except asyncio.QueueFull:
            self._dropped_input_chunks += 1
            return False

    async def greet(self) -> None:
        """Ask Roo to greet the visitor after the wake-scoped session opens."""

        self._drain_input_queue()
        self._greeting_pending = True
        await self._send_event(
            {
                "type": "response.create",
                "response": {
                    "output_modalities": ["audio"],
                    "instructions": ("Say exactly: " + json.dumps(AI_DISCLOSURE + " How can I help?")),
                },
            }
        )
        # Keep cloud input gated through the one-sentence greeting. This gives
        # the acoustic echo canceller one utterance to converge without letting
        # startup echo cancel its own response. Subsequent turns are full duplex.

    async def announce(self, text: str) -> None:
        """Speak trusted local-controller status without altering visitor context."""

        await self._arbiter.announce(text)

    async def _announce_out_of_band(self, text: str) -> None:

        normalized = " ".join(text.split())[:500]
        if not normalized:
            return
        if self._running:
            self._response_active = True
            self._response_finished.clear()
        await self._send_event(
            {
                "type": "response.create",
                "response": {
                    "conversation": "none",
                    "input": [],
                    "output_modalities": ["audio"],
                    "tools": [],
                    "tool_choice": "none",
                    "metadata": {"source": "local_access_controller"},
                    "instructions": (
                        "Say exactly this trusted lobby status with no additions: " + json.dumps(normalized)
                    ),
                },
            }
        )
        if self._running:
            await asyncio.wait_for(self.wait_response_finished(), timeout=30.0)

    async def wait_response_finished(self) -> None:
        await self._response_finished.wait()
        await self._writer.wait_drained()

    async def cancel_response(self) -> None:
        if self._active_response_id is not None:
            self._interrupted_responses.add(self._active_response_id)
        if self._response_active:
            event = {"type": "response.cancel"}
            if self._active_response_id is not None:
                event["response_id"] = self._active_response_id
            await self._send_event(event)
        await self._handle_interruption()
        self._response_active = False
        self._response_finished.set()

    async def begin_local_playback(self) -> None:
        """A fixed notice owns the speaker; stale server events cannot truncate it."""

        self._local_playback_active = True
        self.suspend_input()
        await self.cancel_response()

    def end_local_playback(self) -> None:
        self._local_playback_active = False

    def suspend_input(self) -> None:
        self._input_suspended = True
        self._drain_input_queue()

    def resume_input(self) -> None:
        self._drain_input_queue()
        self._input_suspended = False

    def touch_activity(self) -> None:
        self._last_activity = time.monotonic()
        if self.on_activity is not None:
            self.on_activity()

    async def inject_system_context(self, outcome: str) -> None:
        """Inject enum codes only; no identity, score, arbitrary prose or follow-up response."""

        if not isinstance(outcome, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", outcome):
            raise ValueError("Controller context must be an outcome code")
        self._authorized_door_claim = outcome == "ACTUATED_CONFIRMED"
        self._context_codes.append(outcome)
        await self._send_event(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "system",
                    "content": [{"type": "input_text", "text": "[access_controller] outcome=" + outcome}],
                },
            }
        )

    async def send_text(self, text: str) -> None:
        await self._send_event(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )
        await self._send_event({"type": "response.create"})

    async def handle_server_event(self, event: dict[str, Any]) -> None:
        """Handle one decoded server event; public for protocol-level tests."""

        event_type = event.get("type")
        response = event.get("response")
        response_id = event.get("response_id")
        if isinstance(response, dict):
            response_id = response.get("id", response_id)
        if isinstance(response_id, str) and response_id in self._interrupted_responses:
            return
        if event_type == "session.updated":
            self._configured.set()
            return
        if event_type == "response.created":
            if self._local_playback_active:
                if isinstance(response_id, str):
                    self._interrupted_responses.add(response_id)
                return
            self._active_response_id = response_id if isinstance(response_id, str) else None
            self._response_active = True
            self._response_finished.clear()
            self._output_transcript = ""
            return
        if event_type == "input_audio_buffer.speech_started":
            if self._input_suspended:
                return
            self.touch_activity()
            await self._handle_interruption()
            self.telemetry.publish("realtime", {"event": "speech_started"})
            return
        if event_type == "input_audio_buffer.speech_stopped":
            if self._input_suspended:
                return
            self.touch_activity()
            self.telemetry.publish("realtime", {"event": "speech_stopped"})
            return
        if event_type == "response.output_item.added":
            if self._local_playback_active:
                return
            item = event.get("item") or {}
            if (
                item.get("type") == "message"
                and isinstance(item.get("id"), str)
                and item["id"] not in self._interrupted_items
            ):
                self._begin_output_item(item["id"], content_index=0)
            return
        if event_type == "response.output_audio.delta":
            if self._local_playback_active:
                return
            await self._handle_audio_delta(event)
            return
        if event_type == "response.done":
            if self._local_playback_active:
                return
            if isinstance(response_id, str) and self._active_response_id not in {None, response_id}:
                return
            await self._handle_response_done(event)
            return
        if event_type in {"response.cancelled", "response.failed"}:
            if self._local_playback_active:
                return
            await self._handle_interruption()
            self._response_active = False
            self._response_finished.set()
            self._greeting_pending = False
            self._input_enabled = True
            return
        if event_type == "rate_limits.updated":
            limits = event.get("rate_limits", [])
            exhausted = any(isinstance(limit, dict) and limit.get("remaining") == 0 for limit in limits)
            self.telemetry.publish("realtime", {"event": "rate_limit", "exhausted": exhausted})
            if exhausted and self._running:
                self._record_failure()
                self._spawn(self._fallback_and_stop("rate_limited"), "realtime-rate-limit")
            return
        if event_type == "response.output_audio_transcript.delta":
            if self._local_playback_active:
                return
            delta = event.get("delta")
            if isinstance(delta, str):
                self._output_transcript = (self._output_transcript + delta)[-1000:]
                if not self._authorized_door_claim and _has_door_claim(self._output_transcript):
                    await self.cancel_response()
                    await self.inject_system_context("UNCONFIRMED_DOOR_CLAIM_BLOCKED")
                    self.telemetry.publish("realtime", {"event": "output_guardrail"})
            return
        if event_type == "response.output_audio_transcript.done":
            transcript = event.get("transcript")
            if isinstance(transcript, str):
                self.telemetry.publish(
                    "transcript",
                    {"role": "assistant", "characters": len(transcript), "content_retained": False},
                )
            return
        if event_type == "conversation.item.input_audio_transcription.completed":
            transcript = event.get("transcript")
            if isinstance(transcript, str):
                self.telemetry.publish(
                    "transcript",
                    {"role": "user", "characters": len(transcript), "content_retained": False},
                )
            return
        if event_type == "error":
            error = event.get("error") or {}
            self._last_error = str(error.get("code") or "realtime_error")
            self._last_error_detail = {
                key: str(error[key])[:300] for key in ("type", "code", "param") if error.get(key) is not None
            }
            logger.error(
                "OpenAI Realtime error code=%s param=%s",
                self._last_error,
                self._last_error_detail.get("param"),
            )
            self.telemetry.publish("realtime", {"event": "error", "code": self._last_error})
            self._configured.set()
            if (
                self._last_error in {"session_expired", "rate_limit_exceeded", "server_error"}
                and self._running
            ):
                self._spawn(self._fallback_and_stop(self._last_error), "realtime-server-failure")

    def diagnostics(self) -> dict[str, Any]:
        return {
            "api": "realtime",
            "api_label": "Realtime",
            "running": self._running,
            "input_enabled": self._input_enabled,
            "model": self.settings.openai_realtime_model,
            "prompt_version": ROO_LOBBY_PROMPT_VERSION,
            "queued_input_chunks": self._input_queue.qsize(),
            "dropped_input_chunks": self._dropped_input_chunks,
            "active_output_item": self._active_output_item_id,
            "last_error": self._last_error,
            "last_error_detail": self._last_error_detail,
            "cloud_disabled": time.monotonic() < self._cloud_disabled_until,
            "queued_output_chunks": self._writer.queue.qsize(),
        }

    async def _send_audio_loop(self) -> None:
        while self._running:
            payload = await self._input_queue.get()
            await self._send_event(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(payload).decode("ascii"),
                }
            )

    async def _receive_loop(self) -> None:
        try:
            while self._running and self.connection is not None:
                raw = await self.connection.recv()
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                event = json.loads(raw)
                if not isinstance(event, dict):
                    raise ValueError("Realtime event must be an object")
                await self.handle_server_event(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._last_error = type(exc).__name__
            logger.warning("Realtime receive loop stopped: %s", type(exc).__name__)
            if self._running:
                await self._recover_connection()

    async def _watchdog(self) -> None:
        while self._running:
            await asyncio.sleep(1.0)
            now = time.monotonic()
            if self._reconnecting:
                continue
            if (
                not self.idle_timeout_paused()
                and now - self._last_activity >= self.settings.realtime_idle_timeout_seconds
            ):
                await self.stop(reason="idle_timeout")
                return
            if now - self._started_at >= self.settings.realtime_max_session_seconds:
                await self.stop(reason="max_session_age")
                return

    async def _handle_audio_delta(self, event: dict[str, Any]) -> None:
        encoded = event.get("delta")
        if not isinstance(encoded, str) or self._output_resampler is None:
            return
        item_id = event.get("item_id")
        if isinstance(item_id, str) and item_id in self._interrupted_items:
            return
        content_index = int(event.get("content_index", 0))
        if isinstance(item_id, str) and item_id != self._active_output_item_id:
            self._begin_output_item(item_id, content_index=content_index)
        try:
            pcm = np.frombuffer(base64.b64decode(encoded, validate=True), dtype=np.int16)
        except (ValueError, TypeError) as exc:
            raise ValueError("Malformed Realtime audio delta") from exc
        converted = self._output_resampler.resample_chunk(pcm, last=False)
        if converted.size:
            self._active_output_received_frames += int(converted.size)
            if not self._writer.enqueue(item_id, converted.astype(np.float32) / 32768.0):
                await self.cancel_response()
                self.telemetry.publish("realtime", {"event": "output_queue_overflow"})
        self.touch_activity()

    async def _handle_interruption(self) -> None:
        if self._active_response_id is not None:
            self._interrupted_responses.add(self._active_response_id)
        item_id = self._active_output_item_id
        if item_id is None:
            await self._writer.clear()
            return
        played = max(0, self.playback.output_frames_played() - self._active_output_start_frames)
        played = min(played, self._active_output_received_frames)
        await self._writer.clear(item_id=item_id)
        if self._output_resampler is not None:
            self._output_resampler.clear()
        rate = self.playback.output_sample_rate()
        audio_end_ms = int((played * 1000) / rate) if rate > 0 else 0
        await self._send_event(
            {
                "type": "conversation.item.truncate",
                "item_id": item_id,
                "content_index": self._active_output_content_index,
                "audio_end_ms": audio_end_ms,
            }
        )
        self.telemetry.publish(
            "realtime",
            {"event": "interrupted", "item_id": item_id, "audio_end_ms": audio_end_ms},
        )
        self._active_output_item_id = None
        self._active_output_received_frames = 0

    async def _handle_response_done(self, event: dict[str, Any]) -> None:
        response = event.get("response")
        if not isinstance(response, dict):
            return
        self._response_active = False
        self._response_finished.set()
        output_item_id = self._active_output_item_id
        enable_input = self._greeting_pending
        self._greeting_pending = False
        if output_item_id is not None or enable_input:
            self._spawn(
                self._finish_output_when_drained(output_item_id, enable_input=enable_input),
                "realtime-output-drain",
            )
        status = response.get("status")
        if status != "completed":
            self.telemetry.publish("realtime", {"event": "response_done", "status": status})
            if status in {"cancelled", "failed", "incomplete"}:
                await self._handle_interruption()
            return
        output = response.get("output", [])
        if not isinstance(output, list):
            return
        calls = []
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "function_call":
                continue
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or call_id in self._handled_tool_calls:
                continue
            if len(self._handled_tool_calls) >= self.tools.session_limit:
                self.telemetry.publish("realtime_tool", {"status": "rate_limited"})
                break
            self._handled_tool_calls.add(call_id)
            self.touch_activity()
            calls.append(item)
        if calls:
            self._spawn(self._execute_tool_batch(calls), "realtime-tools")

    async def _finish_output_when_drained(
        self,
        item_id: str | None,
        *,
        enable_input: bool,
    ) -> None:
        await self._writer.wait_drained()
        if item_id is not None and self._active_output_item_id == item_id:
            self._active_output_item_id = None
            self._active_output_received_frames = 0
        if self._running and enable_input:
            await asyncio.sleep(self.settings.realtime_initial_output_settle_ms / 1000.0)
            if not self._running:
                return
            self._drain_input_queue()
            self._input_enabled = True
            self.telemetry.publish("realtime", {"event": "input_enabled"})

    async def _execute_tool_batch(self, items: list[dict[str, Any]]) -> None:
        await asyncio.gather(*(self._execute_tool_call(item) for item in items))
        follow_up = any(
            item.get("name") not in {"wait_for_user", "end_conversation", "request_access_check"}
            for item in items
        )
        if follow_up and self.connection is not None and not self._input_suspended:
            await self._response_finished.wait()
            await self._send_event({"type": "response.create"})

    async def _execute_tool_call(self, item: dict[str, Any]) -> None:
        call_id = str(item["call_id"])
        name = item.get("name")
        result = await self.tools.execute(str(name), str(item.get("arguments") or "{}"), call_id=call_id)
        if self.connection is None:
            return
        await self._send_event(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(result, separators=(",", ":"), default=str),
                },
            }
        )
        self.telemetry.publish(
            "realtime_tool",
            {"name": str(name), "call_id": call_id, "status": result.get("status")},
        )

    async def _search_knowledge(self, arguments: dict[str, Any], call_id: str) -> dict[str, Any]:
        try:
            answer = await self.knowledge.answer(arguments["query"], request_id=call_id)
            return answer.as_tool_output()
        except PublicKnowledgeError:
            return {
                "status": "unavailable",
                "answer": "Approved MLAI public knowledge is unavailable right now.",
                "citations": [],
            }

    def _record_failure(self) -> None:
        now = time.monotonic()
        self._failures.append(now)
        while self._failures and self._failures[0] < now - 600:
            self._failures.popleft()
        if len(self._failures) >= 3:
            self._cloud_disabled_until = now + 300

    async def _fallback_and_stop(self, reason: str) -> None:
        await self._writer.clear(item_id=self._active_output_item_id)
        if self.on_fallback is not None:
            try:
                result = self.on_fallback()
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                self.telemetry.publish("realtime", {"event": "fallback_failed", "error": type(exc).__name__})
        await self.stop(reason=reason)

    async def _recover_connection(self) -> None:
        """Reconnect once; replay only enum outcomes, never audio or tool calls."""

        self._record_failure()
        if self._reconnect_attempted or self._reconnecting or time.monotonic() < self._cloud_disabled_until:
            await self._fallback_and_stop("connection_error")
            return
        self._reconnect_attempted = True
        self._reconnecting = True
        await self.tools.cancel_pending()
        self.suspend_input()
        await self._writer.clear(item_id=self._active_output_item_id)
        self._active_output_item_id = None
        self._response_active = False
        self._response_finished.set()
        current = asyncio.current_task()
        tasks = tuple(task for task in self._tasks if task is not current)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            connection, context = self.connection, self._connection_context
            self.connection = None
            self._connection_context = None
            if connection is not None:
                await connection.close()
            if context is not None:
                await context.__aexit__(None, None, None)
            await asyncio.sleep(random.uniform(0.5, 1.5))
            if not self._running:
                return
            key = _secret_value(self.settings.openai_api_key)
            safety_key = _secret_value(getattr(self.settings, "realtime_safety_identifier_key", None))
            query = urlencode({"model": self.settings.openai_realtime_model})
            url = f"{self.settings.openai_realtime_url}?{query}"
            self._connection_context = self.connection_factory(
                url,
                additional_headers={
                    "Authorization": f"Bearer {key}",
                    "OpenAI-Safety-Identifier": safety_identifier(
                        self.settings.device_id, safety_key or self._ephemeral_safety_key
                    ),
                },
                open_timeout=self.settings.realtime_open_timeout_seconds,
                max_queue=32,
            )
            self._configured.clear()
            self._last_error = None
            self.connection = await self._connection_context.__aenter__()
            self._spawn(self._receive_loop(), "realtime-events")
            await self._send_event(build_session_update(self.settings))
            await asyncio.wait_for(self._configured.wait(), self.settings.realtime_open_timeout_seconds)
            if self._last_error:
                raise RuntimeError("Realtime reconnect configuration rejected")
            if self._context_codes:
                # 30 enum codes at most, additionally capped below a 300-token budget.
                summary = ";".join(self._context_codes)[-900:]
                await self._send_event(
                    {
                        "type": "conversation.item.create",
                        "item": {
                            "type": "message",
                            "role": "system",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": "[access_controller] prior_outcomes=" + summary,
                                }
                            ],
                        },
                    }
                )
            self._input_resampler.clear()
            if self._output_resampler is not None:
                self._output_resampler.clear()
            self._greeting_pending = False
            self._input_enabled = True
            self.resume_input()
            self._reconnecting = False
            self.touch_activity()
            self._spawn(self._send_audio_loop(), "realtime-audio-send")
            self._spawn(self._watchdog(), "realtime-watchdog")
            self.telemetry.publish("realtime", {"event": "reconnected"})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record_failure()
            self._last_error = type(exc).__name__
            await self._fallback_and_stop("reconnect_failed")
        finally:
            self._reconnecting = False

    async def _send_event(self, event: dict[str, Any]) -> None:
        connection = self.connection
        if connection is None:
            raise RuntimeError("Realtime session is not connected")
        async with self._send_lock:
            await connection.send(json.dumps(event, separators=(",", ":")))

    def _begin_output_item(self, item_id: str, *, content_index: int) -> None:
        self._active_output_item_id = item_id
        self._active_output_content_index = content_index
        self._active_output_start_frames = self.playback.output_frames_played()
        self._active_output_received_frames = 0

    def _spawn(self, awaitable: Any, name: str) -> None:
        task = asyncio.create_task(awaitable, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self.telemetry.publish(
                "realtime", {"event": "task_failed", "error": type(task.exception()).__name__}
            )
            if self._running and not self._reconnecting:
                self._spawn(self._recover_connection(), "realtime-reconnect")

    def _reset_audio_state(self) -> None:
        self._input_resampler.clear()
        if self._output_resampler is not None:
            self._output_resampler.clear()
        self._output_resampler = None
        self._active_output_item_id = None
        self._active_output_received_frames = 0
        self._greeting_pending = False
        self._active_response_id = None
        self._local_playback_active = False
        self._interrupted_responses.clear()
        self._output_transcript = ""
        self._response_finished.set()
        self._drain_input_queue()

    def _drain_input_queue(self) -> None:
        while not self._input_queue.empty():
            try:
                self._input_queue.get_nowait()
            except asyncio.QueueEmpty:
                break


def _default_connection_factory(*args: Any, **kwargs: Any) -> AbstractAsyncContextManager[Any]:
    from websockets.asyncio.client import connect

    return connect(*args, **kwargs)


def _secret_value(value: Any) -> str:
    if value is None:
        return ""
    getter = getattr(value, "get_secret_value", None)
    return str(getter() if getter is not None else value).strip()


def safety_identifier(device_id: str, key: str | bytes) -> str:
    """A device label cannot be recovered using an unkeyed dictionary attack."""

    if not key:
        raise ValueError("Safety identifier needs a secret key")
    return hmac.new(
        key.encode("utf-8") if isinstance(key, str) else key, device_id.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _has_door_claim(transcript: str) -> bool:
    for match in DOOR_CLAIM.finditer(transcript):
        prefix = transcript[max(0, match.start() - 25) : match.start()].lower()
        if not re.search(r"(?:not |never |isn't |hasn't |cannot |can't |don't )$", prefix):
            return True
    return False
