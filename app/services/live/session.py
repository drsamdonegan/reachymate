"""GPT-Live's continuous audio protocol with locally validated Responses tools.

Reuse the media queues, tool registry and bounded fallback from the previous
transport. Live has no Realtime turn commits, response audio-done or truncation.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from uuid import uuid4

import numpy as np
import soxr

from app.config import Settings
from app.services.live.prompt import (
    BACKEND_INSTRUCTIONS,
    GREETING_INSTRUCTION,
    LIVE_INSTRUCTIONS,
    LIVE_PROMPT_VERSION,
)
from app.services.realtime.session import (
    OpenAIRealtimeSession,
    _has_door_claim,
    _secret_value,
    safety_identifier,
)
from app.services.realtime.tools import tool_schemas

logger = logging.getLogger(__name__)
LIVE_AUDIO_RATE = 16_000


def build_session_start(settings: Settings) -> dict:
    return {
        "type": "session.start",
        "session": {
            "model": settings.openai_live_model,
            "instructions": LIVE_INSTRUCTIONS,
            "input": [
                {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": GREETING_INSTRUCTION}],
                }
            ],
            "audio": {
                "format": {"type": "audio/pcm", "rate": LIVE_AUDIO_RATE},
                "output": {"voice": settings.openai_realtime_voice},
            },
            "delegation": {
                "type": "responses",
                "responses": {
                    "model": settings.openai_live_backend_model,
                    "instructions": BACKEND_INSTRUCTIONS,
                    "tools": [dict(tool, strict=False) for tool in tool_schemas()],
                    "tool_choice": "auto",
                    "parallel_tool_calls": False,
                    "reasoning": {"effort": "low"},
                    "max_output_tokens": 1024,
                },
            },
            "store": False,
        },
    }


class OpenAIGptLiveSession(OpenAIRealtimeSession):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._input_resampler = soxr.ResampleStream(
            self.settings.sample_rate_hz, LIVE_AUDIO_RATE, 1, dtype="int16"
        )
        self._input_queue = asyncio.Queue(maxsize=min(self.settings.realtime_input_queue_chunks, 8))
        self._live_closed = asyncio.Event()
        self._stop_lock = asyncio.Lock()
        self._live_closing = False
        self._generation = 0
        self._session_id = None
        self._model_audio_blocked = False
        self._controller_announcement = False
        self._server_input_muted = False
        self._command_waiters: dict[str, asyncio.Future] = {}
        self._calls_by_response: dict[str, list[dict]] = {}
        self._response_for_delegation: dict[str, str] = {}
        self._backend_pending: set[str] = set()
        self._last_output_speech_at = 0.0
        self._output_started = asyncio.Event()
        self._input_frames_sent = self._nonzero_input_frames_sent = 0
        self._output_frames_received = self._duplex_input_frames = 0
        self._dropped_output_frames = 0
        self._usage_seconds = 0.0
        self._finalization_confirmed = False
        self._output_guardrail_active = False

    def _emit(self, event: str, **fields) -> None:
        self.telemetry.publish("live", {"event": event, **fields})

    async def _send_event(self, event: dict) -> None:
        if self.connection is None:
            return
        event.setdefault("event_id", str(uuid4()))
        async with self._send_lock:
            await self.connection.send(json.dumps(event, separators=(",", ":")))

    async def start(self) -> None:
        if self._running:
            return
        if time.monotonic() < self._cloud_disabled_until:
            raise RuntimeError("GPT-Live circuit breaker is open")
        key = _secret_value(self.settings.openai_api_key)
        if not key or self.playback.output_sample_rate() <= 0:
            raise RuntimeError("GPT-Live requires an API credential and ready playback")
        safety_key = _secret_value(self.settings.realtime_safety_identifier_key)
        if not safety_key and self.settings.is_production:
            raise RuntimeError("A keyed voice safety identifier is required")
        self._connection_context = self.connection_factory(
            self.settings.openai_live_url,
            additional_headers={
                "Authorization": "Bearer " + key,
                "OpenAI-Safety-Identifier": safety_identifier(
                    self.settings.device_id, safety_key or self._ephemeral_safety_key
                ),
            },
            open_timeout=self.settings.realtime_open_timeout_seconds,
            max_queue=32,
        )
        self._generation += 1
        self._configured.clear()
        self._live_closed.clear()
        self._live_closing = False
        self._last_error = self._last_error_detail = None
        self._input_enabled = self._input_suspended = self._local_playback_active = False
        self._model_audio_blocked = self._output_guardrail_active = False
        self._server_input_muted = False
        self._handled_tool_calls.clear()
        self._backend_pending.clear()
        self._calls_by_response.clear()
        self._response_for_delegation.clear()
        self._context_codes.clear()
        self._authorized_door_claim = False
        self._output_transcript = ""
        self._drain_input_queue()
        self._input_resampler.clear()
        self._input_frames_sent = self._nonzero_input_frames_sent = 0
        self._output_frames_received = self._duplex_input_frames = 0
        self._dropped_output_frames = self._dropped_input_chunks = 0
        self._last_output_speech_at = 0
        self._usage_seconds = 0
        self._finalization_confirmed = False
        self.tools.reset()
        self._arbiter.reopen()
        try:
            self.connection = await self._connection_context.__aenter__()
            await self.playback.start_playing()
            self._output_resampler = soxr.ResampleStream(
                LIVE_AUDIO_RATE, self.playback.output_sample_rate(), 1, dtype="int16"
            )
            self._running = True
            self._started_at = self._last_activity = time.monotonic()
            self._spawn(self._receive_loop(), "gpt-live-events")
            await self._send_event(build_session_start(self.settings))
            await asyncio.wait_for(self._configured.wait(), self.settings.realtime_open_timeout_seconds)
            if self._last_error or self._live_closed.is_set():
                raise RuntimeError("GPT-Live rejected the session configuration")
            self._input_enabled = True
            self._spawn(self._send_audio_loop(), "gpt-live-audio-send")
            self._spawn(self._watchdog(), "gpt-live-watchdog")
            self._emit(
                "connected",
                model=self.settings.openai_live_model,
                backend_model=self.settings.openai_live_backend_model,
                full_duplex=True,
            )
        except Exception:
            self._record_failure()
            await self.stop(reason="startup_failed")
            raise

    async def greet(self) -> None:
        # The startup developer message requests the greeting. Audio continues
        # throughout it; GPT-Live itself chooses when to listen and speak.
        self._emit("greeting_requested", full_duplex=True)

    async def stop(self, *, reason: str = "requested") -> None:
        async with self._stop_lock:
            if not self._running and self.connection is None and self._connection_context is None:
                return
            self._live_closing = True
            self._input_enabled = False
            self._generation += 1  # Late tool results belong to the old session.
            await self.tools.cancel_pending()
            if self.connection is not None and not self._live_closed.is_set():
                try:
                    await self._send_event({"type": "session.close"})
                    await asyncio.wait_for(self._live_closed.wait(), self.settings.live_close_timeout_seconds)
                except Exception:
                    self._emit("finalization_unconfirmed", reason=reason)
            for waiter in self._command_waiters.values():
                if not waiter.done():
                    waiter.cancel()
            self._command_waiters.clear()
            await super().stop(reason=reason)
            self._emit(
                "disconnected",
                reason=reason,
                seconds=self._usage_seconds,
                finalization_confirmed=self._finalization_confirmed,
            )

    async def _receive_loop(self) -> None:
        try:
            while self.connection is not None and not self._live_closed.is_set():
                raw = await self.connection.recv()
                event = json.loads(raw)
                if not isinstance(event, dict):
                    raise ValueError("Invalid GPT-Live event")
                await self.handle_server_event(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._last_error = type(exc).__name__
            self._configured.set()
            if self._running and not self._live_closing:
                self._spawn(self._recover_connection(), "gpt-live-disconnect")

    async def _recover_connection(self) -> None:
        # A fresh wake starts a new Live session. Never replay speech or effects,
        # or silently switch to the old Realtime API after a Live failure.
        self._record_failure()
        await self._fallback_and_stop("connection_error")

    async def _send_audio_loop(self) -> None:
        deadline = time.monotonic()
        silence = bytes(int(LIVE_AUDIO_RATE * self.settings.wakeword_chunk_ms / 1000) * 2)
        while self._running and not self._live_closing:
            try:
                payload = self._input_queue.get_nowait()
            except asyncio.QueueEmpty:
                payload = silence
            if self._input_suspended:
                payload = bytes(len(payload))
            # Silence keeps the Live timeline moving while the local gate is
            # closed, including startup. Unadmitted microphone audio is absent.
            await self._send_event(
                {
                    "type": "session.input_audio.append",
                    "audio": base64.b64encode(payload).decode("ascii"),
                }
            )
            frames = len(payload) // 2
            self._input_frames_sent += frames
            if any(payload):
                self._nonzero_input_frames_sent += frames
                if self.playback.output_active():
                    self._duplex_input_frames += frames
            deadline += frames / LIVE_AUDIO_RATE
            if deadline < time.monotonic() - 0.2:
                deadline = time.monotonic()
            await asyncio.sleep(max(0, deadline - time.monotonic()))

    async def handle_server_event(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "session.started":
            self._session_id = (event.get("session") or {}).get("id")
            self._configured.set()
        elif kind == "session.closed":
            self._usage_seconds = float((event.get("usage") or {}).get("seconds", self._usage_seconds))
            self._finalization_confirmed = True
            self._live_closed.set()
            if not self._live_closing:
                self._spawn(self.stop(reason="server_closed"), "gpt-live-server-close")
        elif kind == "session.usage.updated":
            self._usage_seconds = float((event.get("usage") or {}).get("seconds", self._usage_seconds))
        elif kind == "session.output_audio.delta":
            await self._handle_live_audio(event)
        elif kind in {"session.input_transcript.delta", "session.output_transcript.delta"}:
            delta = event.get("delta")
            if not isinstance(delta, str):
                return
            role = "user" if kind == "session.input_transcript.delta" else "assistant"
            self.telemetry.publish(
                "transcript",
                {
                    "role": role,
                    "characters": len(delta),
                    "content_retained": False,
                    "start_ms": event.get("start_ms"),
                    "end_ms": event.get("end_ms"),
                    "api": "gpt_live",
                },
            )
            if role == "user" and not self._input_suspended:
                self.touch_activity()
                self._emit("input_transcript", characters=len(delta))
            if role == "assistant":
                self._output_transcript = (self._output_transcript + delta)[-1000:]
                if not self._authorized_door_claim and _has_door_claim(self._output_transcript):
                    self._output_guardrail_active = True
                    await self._writer.clear()
                    self._spawn(self._guardrail_stop(), "gpt-live-output-guardrail")
        elif kind == "session.delegation.created":
            delegation = event.get("delegation") or {}
            ref = delegation.get("id")
            if isinstance(ref, str):
                self._backend_pending.add(ref)
                response_id = delegation.get("response_id")
                if isinstance(response_id, str):
                    self._response_for_delegation[ref] = response_id
            self.touch_activity()
            self._emit("delegation_started", target=delegation.get("target"))
        elif kind == "response.event":
            await self._handle_backend_event(event)
        elif kind == "error":
            error = event.get("error") or {}
            if self._live_closing and error.get("code") == "context_injection_incomplete":
                # Closing stops the timeline, so an in-flight context append
                # may be discarded. The session.closed acknowledgment still
                # determines finalization; this is not a new connection fault.
                waiter = self._command_waiters.pop(error.get("client_event_id"), None)
                if waiter is not None and not waiter.done():
                    waiter.cancel()
                self._emit("closing_context_discarded")
                return
            self._last_error = str(error.get("code") or "live_error")
            self._last_error_detail = {
                key: str(error[key])[:300] for key in ("type", "code", "param") if error.get(key) is not None
            }
            waiter = self._command_waiters.pop(error.get("client_event_id"), None)
            if waiter is not None and not waiter.done():
                waiter.set_exception(RuntimeError(self._last_error))
            self._configured.set()
            self._emit("error", code=self._last_error, param=error.get("param"))
            if self._running and not self._live_closing:
                self._spawn(self._fallback_and_stop(self._last_error), "gpt-live-error-close")
        else:
            waiter = self._command_waiters.pop(event.get("client_event_id"), None)
            if waiter is not None and not waiter.done():
                waiter.set_result(event)
            if kind == "session.input_audio.muted":
                self._server_input_muted = True
            elif kind == "session.input_audio.unmuted":
                self._server_input_muted = False

    async def _handle_live_audio(self, event: dict) -> None:
        try:
            raw = base64.b64decode(event["delta"], validate=True)
            pcm = np.frombuffer(raw, dtype=np.int16)
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError("Malformed GPT-Live audio") from exc
        self._output_frames_received += len(pcm)
        if (
            self._local_playback_active
            or self._output_guardrail_active
            or self._model_audio_blocked
            and not self._controller_announcement
        ):
            self._dropped_output_frames += len(pcm)
            return
        if self._output_resampler is None or not len(pcm):
            return
        samples = (
            pcm
            if self.playback.output_sample_rate() == LIVE_AUDIO_RATE
            else self._output_resampler.resample_chunk(pcm, last=False)
        )
        if not np.any(samples):
            # Continuous zero output is timeline silence, not speech/activity.
            # The hardware callback already supplies silence when no audio is queued.
            return
        rate = self.playback.output_sample_rate()
        buffered = self._writer.queued_frames + getattr(self.playback, "output_buffered_frames", lambda: 0)()
        if buffered / rate > self.settings.live_max_output_buffer_seconds:
            self._dropped_output_frames += buffered
            await self._writer.clear()
            self._emit("playback_backlog_discarded", frames=buffered)
        self._last_output_speech_at = time.monotonic()
        self._output_started.set()
        self.touch_activity()
        if not self._writer.enqueue(None, samples.astype(np.float32) / 32768):
            self._dropped_output_frames += len(samples)
            await self._writer.clear()
            self._emit("output_queue_overflow")

    async def _handle_backend_event(self, envelope: dict) -> None:
        event = envelope.get("event") or {}
        kind = event.get("type")
        response = event.get("response") or {}
        delegation_id = envelope.get("delegation_id")
        ref = (
            event.get("response_id") or response.get("id") or self._response_for_delegation.get(delegation_id)
        )
        if not isinstance(ref, str) or not isinstance(delegation_id, str):
            return
        if kind == "response.created":
            self._response_for_delegation[delegation_id] = ref
            self._calls_by_response.setdefault(ref, [])
        elif kind == "response.output_item.done":
            item = event.get("item") or {}
            if item.get("type") == "function_call" and isinstance(item.get("call_id"), str):
                call_id = item["call_id"]
                if call_id not in self._handled_tool_calls:
                    self._handled_tool_calls.add(call_id)
                    self._calls_by_response.setdefault(ref, []).append(item)
        elif kind in {"response.completed", "response.failed", "response.incomplete"}:
            calls = self._calls_by_response.pop(ref, [])
            if calls and kind == "response.completed":
                self._spawn(self._execute_live_tool_batch(calls, self._generation), "gpt-live-tools")
            else:
                self._backend_pending.discard(envelope.get("delegation_id"))
            self._response_for_delegation.pop(delegation_id, None)
            self._emit("backend_response", status=kind.removeprefix("response."), tool_calls=len(calls))
        if not self._live_closing:
            self.touch_activity()

    async def _execute_live_tool_batch(self, calls: list[dict], generation: int) -> None:
        for item in calls:
            if generation != self._generation or self._live_closing:
                return
            call_id, name = item["call_id"], str(item.get("name"))
            result = await self.tools.execute(name, str(item.get("arguments") or "{}"), call_id=call_id)
            if generation != self._generation or self._live_closing or self.connection is None:
                return
            await self._send_event(
                {
                    "type": "response.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps(result, separators=(",", ":"), allow_nan=False),
                    },
                }
            )
            self.telemetry.publish(
                "realtime_tool",
                {
                    "name": name,
                    "call_id": call_id,
                    "status": result.get("status"),
                    "api": "gpt_live",
                },
            )
        if generation == self._generation and not self._live_closing and self.connection is not None:
            await self._send_event({"type": "response.create"})

    async def _command(self, kind: str, **fields) -> dict:
        ref = str(uuid4())
        future = asyncio.get_running_loop().create_future()
        self._command_waiters[ref] = future
        try:
            await self._send_event({"type": kind, "event_id": ref, **fields})
            return await asyncio.wait_for(future, 5)
        finally:
            self._command_waiters.pop(ref, None)

    async def begin_local_playback(self) -> None:
        self._local_playback_active = self._input_suspended = self._model_audio_blocked = True
        self._drain_input_queue()
        await self._writer.clear()
        await self._command("session.input_audio.mute")
        await self._command(
            "session.instructions.append",
            delegation_id=None,
            content=(
                "The application is playing a fixed local notice. Remain silent until it provides a result."
            ),
        )

    def end_local_playback(self) -> None:
        self._local_playback_active = False
        # Keep input/output suspended through the local consent window.

    def suspend_input(self) -> None:
        self._input_suspended = self._model_audio_blocked = True
        self._drain_input_queue()

    def resume_input(self) -> None:
        self._drain_input_queue()
        if self._server_input_muted and self._running and not self._live_closing:
            self._spawn(self._resume_input(), "gpt-live-unmute")
        else:
            self._input_suspended = self._model_audio_blocked = False

    async def _resume_input(self) -> None:
        await self._command("session.input_audio.unmute")
        await self._command(
            "session.instructions.append",
            delegation_id=None,
            content="The local notice or access attempt has finished. Resume normal conversation.",
        )
        self._input_suspended = self._model_audio_blocked = False

    async def cancel_response(self) -> None:
        # Native Live interruptions are handled by the voice model. This is
        # reserved for explicit application/arbiter interventions, not every turn.
        await self._writer.clear()
        await self._command(
            "session.instructions.append",
            delegation_id=None,
            content="Yield now. Stop the current spoken reply and listen to the caller or controller update.",
        )

    async def wait_response_finished(self) -> None:
        while self._running and (
            self._writer.busy
            or self.playback.output_active()
            or time.monotonic() - self._last_output_speech_at < 0.5
        ):
            await asyncio.sleep(0.02)

    async def _announce_out_of_band(self, text: str) -> None:
        normalized = " ".join(text.split())[:500]
        if not normalized or not self._running:
            return
        self._output_started.clear()
        self._controller_announcement = True
        try:
            await self._command("session.commentary.append", delegation_id=None, content=normalized)
            await asyncio.wait_for(self._output_started.wait(), 5)
            await asyncio.wait_for(self.wait_response_finished(), 15)
        finally:
            self._controller_announcement = False

    async def inject_system_context(self, outcome: str) -> None:
        if not isinstance(outcome, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", outcome):
            raise ValueError("Controller context must be an outcome code")
        self._authorized_door_claim = outcome == "ACTUATED_CONFIRMED"
        self._context_codes.append(outcome)
        await self._command(
            "session.instructions.append",
            delegation_id=None,
            content="[access_controller] outcome=" + outcome + ". Shadow never means a door opened.",
        )

    async def send_text(self, text: str) -> None:
        await self._send_event(
            {
                "type": "response.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )
        await self._send_event({"type": "response.create"})

    async def _guardrail_stop(self) -> None:
        await self.inject_system_context("UNCONFIRMED_DOOR_CLAIM_BLOCKED")
        self._emit("output_guardrail")
        await self._fallback_and_stop("output_guardrail")

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self._emit("task_failed", error=type(task.exception()).__name__)
            if self._running and not self._live_closing:
                self._spawn(self._recover_connection(), "gpt-live-task-recovery")

    def diagnostics(self) -> dict:
        return {
            "api": "gpt_live",
            "api_label": "GPT-Live",
            "full_duplex": True,
            "running": self._running and not self._live_closing,
            "input_enabled": self._input_enabled and not self._input_suspended,
            "model": self.settings.openai_live_model,
            "backend_model": self.settings.openai_live_backend_model,
            "prompt_version": LIVE_PROMPT_VERSION,
            "queued_input_chunks": self._input_queue.qsize(),
            "dropped_input_chunks": self._dropped_input_chunks,
            "queued_output_chunks": self._writer.queue.qsize(),
            "input_frames_sent": self._input_frames_sent,
            "nonzero_input_frames_sent": self._nonzero_input_frames_sent,
            "output_frames_received": self._output_frames_received,
            "simultaneous_input_output_frames": self._duplex_input_frames,
            "dropped_output_frames": self._dropped_output_frames,
            "last_error": self._last_error,
            "last_error_detail": self._last_error_detail,
            "cloud_disabled": time.monotonic() < self._cloud_disabled_until,
            "server_input_muted": self._server_input_muted,
            "usage_seconds": self._usage_seconds,
            "finalization_confirmed": self._finalization_confirmed,
            "backend_pending": len(self._backend_pending),
        }
