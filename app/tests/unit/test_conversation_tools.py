from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import AbstractAsyncContextManager

import numpy as np
import pytest

from app.services.conversation.arbiter import SpeechArbiter
from app.services.conversation.fallback_audio import CLIP_HASHES, load_clip
from app.services.realtime.session import OpenAIRealtimeSession, build_session_update, safety_identifier
from app.services.realtime.tools import ToolRegistry, tool_schemas
from app.services.system.telemetry import TelemetryBroker


class Playback:
    def __init__(self):
        self.audio = []
        self.clears = 0
        self.block = None

    async def start_playing(self):
        return None

    async def stop_playing(self):
        return None

    async def clear_output(self):
        self.clears += 1

    async def push_audio(self, samples):
        if self.block is not None:
            await self.block.wait()
        self.audio.append(samples)

    def output_sample_rate(self):
        return 24000

    def output_frames_played(self):
        return 0

    def output_active(self):
        return False


class Connection:
    def __init__(self):
        self.sent = []
        self.events = asyncio.Queue()

    async def send(self, data):
        event = json.loads(data)
        self.sent.append(event)
        if event["type"] == "session.update":
            self.events.put_nowait(json.dumps({"type": "session.updated"}))

    async def recv(self):
        return await self.events.get()

    async def close(self):
        return None


class Context(AbstractAsyncContextManager):
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *args):
        return None


def session_for(settings):
    playback = Playback()
    connection = Connection()
    session = OpenAIRealtimeSession(
        settings=settings, playback=playback, knowledge=None, telemetry=TelemetryBroker()
    )
    session.connection = connection
    return session, playback, connection


def test_concierge_and_robot_motion_tools_keep_door_boundary(settings):
    schemas = tool_schemas()
    assert len(schemas) == 8
    assert schemas[-1]["name"] == "robot_motion"
    assert all(tool["parameters"]["additionalProperties"] is False for tool in schemas)
    assert not any("door" in tool["name"] for tool in schemas)
    configured = build_session_update(settings)["session"]
    assert "tracing" not in configured
    assert "transcription" not in configured["audio"]["input"]
    assert configured["audio"]["input"]["noise_reduction"] == {"type": "far_field"}


def test_device_identifier_is_keyed():
    first = safety_identifier("reachy-lobby", "secret-one")
    assert first != safety_identifier("reachy-lobby", "secret-two")
    assert first != hashlib.sha256(b"reachy-lobby").hexdigest()


@pytest.mark.asyncio
async def test_registry_rejects_identity_injection_and_deduplicates():
    calls = []

    async def handler(arguments, call_id):
        calls.append((arguments, call_id))
        return {"status": "notified"}

    registry = ToolRegistry(handlers={"route_visitor": handler})
    arguments = {"intent": "delivery", "visitor_first_name": "Sam"}
    assert (await registry.execute("route_visitor", {**arguments, "member_id": "admin"}, call_id="bad"))[
        "status"
    ] == "invalid_arguments"
    assert (await registry.execute("route_visitor", {**arguments, "intent": "unlock"}, call_id="enum"))[
        "status"
    ] == "invalid_arguments"
    results = await asyncio.gather(
        *(registry.execute("route_visitor", arguments, call_id="same") for _ in range(3))
    )
    assert all(result["status"] == "notified" for result in results)
    assert calls == [(arguments, "same")]
    assert (await registry.execute("unlock_door", "{}", call_id="unsafe"))["status"] == "unavailable"


@pytest.mark.asyncio
async def test_registry_deadline_and_session_caps():
    async def slow(arguments, call_id):
        await asyncio.Event().wait()

    registry = ToolRegistry(handlers={"notify_staff": slow}, timeout_seconds=0.01, session_limit=1)
    assert (await registry.execute("notify_staff", {"reason": "visitor_needs_help"}, call_id="1"))[
        "status"
    ] == "unavailable"
    assert (await registry.execute("wait_for_user", {}, call_id="2"))["status"] == "rate_limited"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["cancelled", "failed", "incomplete", None])
async def test_noncompleted_tools_never_run(settings, status):
    session, _, connection = session_for(settings)
    calls = []

    async def handler(arguments, call_id):
        calls.append(call_id)
        return {"status": "consent_prompted"}

    session.registry.set_handler("request_access_check", handler)
    await session.handle_server_event(
        {
            "type": "response.done",
            "response": {
                "status": status,
                "output": [
                    {
                        "type": "function_call",
                        "name": "request_access_check",
                        "call_id": "bad",
                        "arguments": "{}",
                    }
                ],
            },
        }
    )
    await asyncio.sleep(0)
    assert calls == []
    assert not any(e["type"] == "conversation.item.create" for e in connection.sent)


@pytest.mark.asyncio
async def test_late_audio_is_discarded_and_receive_never_waits_on_playback(settings):
    import base64

    import soxr

    session, playback, _ = session_for(settings)
    session._output_resampler = soxr.ResampleStream(24000, 24000, 1, dtype="int16")
    playback.block = asyncio.Event()
    event = {
        "type": "response.output_audio.delta",
        "item_id": "old",
        "delta": base64.b64encode(np.ones(2400, dtype=np.int16).tobytes()).decode(),
    }
    await asyncio.wait_for(session.handle_server_event(event), 0.05)
    await session.handle_server_event({"type": "input_audio_buffer.speech_started"})
    playback.block.set()
    await session.handle_server_event(event)
    await asyncio.sleep(0)
    assert playback.audio == []
    assert session._active_output_item_id is None
    await session._writer.stop()


@pytest.mark.asyncio
async def test_tool_deadline_does_not_block_receive_loop(settings):
    session, _, _ = session_for(settings)
    started = asyncio.Event()
    finish = asyncio.Event()

    async def handler(arguments, call_id):
        started.set()
        await finish.wait()
        return {"status": "notified"}

    session.registry.set_handler("notify_staff", handler)
    await asyncio.wait_for(
        session.handle_server_event(
            {
                "type": "response.done",
                "response": {
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "name": "notify_staff",
                            "call_id": "1",
                            "arguments": '{"reason":"visitor_needs_help"}',
                        }
                    ],
                },
            }
        ),
        0.05,
    )
    await started.wait()
    await asyncio.wait_for(session.handle_server_event({"type": "session.updated"}), 0.05)
    assert session._configured.is_set()
    finish.set()
    await asyncio.gather(*session._tasks)


@pytest.mark.asyncio
async def test_arbiter_interrupts_long_response_and_preserves_announcement_order():
    actions = []

    async def wait():
        await asyncio.Event().wait()

    async def interrupt():
        actions.append("interrupt")

    async def speak(text):
        actions.append(text)

    arbiter = SpeechArbiter(
        wait_response=wait, interrupt=interrupt, speak=speak, response_active=lambda: True, wait_seconds=0.01
    )
    await asyncio.gather(arbiter.announce("first"), arbiter.announce("second"))
    assert actions == ["interrupt", "first", "interrupt", "second"]
    arbiter.close()
    await arbiter.announce("stale")
    assert "stale" not in actions


@pytest.mark.asyncio
async def test_guardrail_cancels_unconfirmed_claim_and_context_is_enum_only(settings):
    session, _, connection = session_for(settings)
    await session.handle_server_event({"type": "response.created"})
    await session.handle_server_event(
        {"type": "response.output_audio_transcript.delta", "delta": "I've unlocked the door."}
    )
    assert any(e["type"] == "response.cancel" for e in connection.sent)
    item = connection.sent[-1]["item"]
    assert item["role"] == "system"
    assert item["content"][0]["text"].endswith("UNCONFIRMED_DOOR_CLAIM_BLOCKED")
    with pytest.raises(ValueError):
        await session.inject_system_context("Sam is a member")
    await session.inject_system_context("ACTUATED_CONFIRMED")
    before = len(connection.sent)
    await session.handle_server_event(
        {"type": "response.output_audio_transcript.delta", "delta": "The door is open."}
    )
    assert len(connection.sent) == before


@pytest.mark.asyncio
async def test_reconnect_replays_only_outcomes_and_resets_visitor_state(settings, monkeypatch):
    settings.openai_api_key = "fake-key"
    connections = []

    def factory(*args, **kwargs):
        connection = Connection()
        connections.append(connection)
        return Context(connection)

    playback = Playback()
    session = OpenAIRealtimeSession(
        settings=settings,
        playback=playback,
        knowledge=None,
        telemetry=TelemetryBroker(),
        connection_factory=factory,
    )
    monkeypatch.setattr("app.services.realtime.session.random.uniform", lambda *_: 0)
    await session.start()
    await session.inject_system_context("SHADOW")
    session._handled_tool_calls.add("old-call")
    await session._recover_connection()
    assert session.running
    assert len(connections) == 2
    assert "[access_controller] prior_outcomes=SHADOW" in str(connections[1].sent)
    assert not any(e["type"] == "response.create" for e in connections[1].sent)
    await session.stop()
    await session.start()
    assert not session._handled_tool_calls
    assert session._dropped_input_chunks == 0
    assert not session._context_codes
    await session.stop()


def test_fixed_clips_have_pinned_integrity_and_tampering_fails(tmp_path):
    for name in CLIP_HASHES:
        samples, rate = load_clip(name)
        assert samples.size > rate
    (tmp_path / "consent_notice.wav").write_bytes(b"changed")
    with pytest.raises(ValueError, match="integrity"):
        load_clip("consent_notice", directory=tmp_path)


def test_schema_validates_with_installed_openai_types(settings):
    from openai.types.realtime import SessionUpdateEvent

    parsed = SessionUpdateEvent.model_validate(build_session_update(settings))
    assert parsed.session.type == "realtime"
    assert len(parsed.session.tools) == 8


@pytest.mark.asyncio
async def test_tool_task_flood_is_bounded(settings):
    session, _, _ = session_for(settings)
    calls = [
        {
            "type": "function_call",
            "name": "notify_staff",
            "call_id": str(i),
            "arguments": '{"reason":"visitor_needs_help"}',
        }
        for i in range(200)
    ]
    await session.handle_server_event(
        {"type": "response.done", "response": {"status": "completed", "output": calls}}
    )
    assert len(session._handled_tool_calls) <= session.registry.session_limit
    assert len(session._tasks) <= session.registry.session_limit
    await asyncio.gather(*session._tasks)


@pytest.mark.asyncio
async def test_circuit_breaker_opens_after_three_failures(settings):
    session, _, _ = session_for(settings)
    for _ in range(3):
        session._record_failure()
    assert session.diagnostics()["cloud_disabled"]
    with pytest.raises(RuntimeError, match="circuit breaker"):
        await session.start()


@pytest.mark.asyncio
async def test_late_cancelled_response_cannot_clear_newer_announcement(settings):
    session, playback, _ = session_for(settings)
    await session.handle_server_event({"type": "response.created", "response": {"id": "old"}})
    await session.cancel_response()
    await session.handle_server_event({"type": "response.created", "response": {"id": "new"}})
    session._begin_output_item("new-item", content_index=0)
    clears = playback.clears
    await session.handle_server_event(
        {"type": "response.done", "response": {"id": "old", "status": "cancelled", "output": []}}
    )
    assert session._response_active
    assert session._active_output_item_id == "new-item"
    assert playback.clears == clears
    await session.handle_server_event(
        {"type": "response.done", "response": {"id": "new", "status": "completed", "output": []}}
    )
    assert "old" in session._interrupted_responses
    await asyncio.gather(*session._tasks)


@pytest.mark.asyncio
async def test_stale_server_speech_cannot_truncate_fixed_consent_clip(settings):
    session, playback, _ = session_for(settings)
    await session.handle_server_event({"type": "response.created", "response": {"id": "old"}})
    await session.begin_local_playback()
    clears = playback.clears
    await session.handle_server_event({"type": "input_audio_buffer.speech_started"})
    await session.handle_server_event({"type": "response.created", "response": {"id": "late"}})
    await session.handle_server_event(
        {"type": "response.done", "response": {"id": "late", "status": "cancelled", "output": []}}
    )
    assert playback.clears == clears
    assert session._local_playback_active
    session.end_local_playback()
    assert session._input_suspended


@pytest.mark.asyncio
async def test_notice_truncated_by_external_clear_cannot_count_as_played():
    from app.services.conversation.fallback_audio import FallbackAudio

    # This sink accepts buffers but a fault clears them before any frames play.
    playback = Playback()
    with pytest.raises(RuntimeError, match="interrupted"):
        await FallbackAudio(playback=playback).play("consent_notice")


@pytest.mark.asyncio
async def test_parallel_tools_produce_one_followup_response(settings):
    session, _, connection = session_for(settings)

    async def handler(arguments, call_id):
        return {"status": "notified"}

    session.registry.set_handler("notify_staff", handler)
    session.registry.set_handler("get_visitor_request_status", handler)
    await session.handle_server_event(
        {
            "type": "response.done",
            "response": {
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "name": "notify_staff",
                        "call_id": "staff",
                        "arguments": '{"reason":"visitor_needs_help"}',
                    },
                    {
                        "type": "function_call",
                        "name": "get_visitor_request_status",
                        "call_id": "status",
                        "arguments": "{}",
                    },
                ],
            },
        }
    )
    await asyncio.gather(*session._tasks)
    assert len([event for event in connection.sent if event["type"] == "conversation.item.create"]) == 2
    assert len([event for event in connection.sent if event["type"] == "response.create"]) == 1
