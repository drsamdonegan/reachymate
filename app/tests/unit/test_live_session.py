from __future__ import annotations

import asyncio
import base64
import json
import time

import numpy as np
import pytest

from app.services.live.session import OpenAIGptLiveSession, build_session_start
from app.services.system.telemetry import TelemetryBroker
from app.tests.unit.test_realtime_session import FakeKnowledge, FakePlayback


class Wire:
    def __init__(self):
        self.sent = []
        self.events = asyncio.Queue()
        self.socket_closed = False

    async def send(self, payload):
        event = json.loads(payload)
        self.sent.append(event)
        kind = event["type"]
        reply = None
        if kind == "session.start":
            reply = {"type": "session.started", "session": {"id": "live-test", "model": "gpt-live-1"}}
        elif kind == "session.close":
            reply = {"type": "session.closed", "usage": {"seconds": 3.0}, "reason": "close_requested"}
        elif kind in {"session.input_audio.mute", "session.input_audio.unmute"}:
            reply = {"type": kind + "d", "client_event_id": event["event_id"]}
        elif kind.endswith(".append") and kind != "session.input_audio.append":
            reply = {"type": kind + "ed", "client_event_id": event["event_id"]}
        if reply:
            await self.events.put(reply)

    async def recv(self):
        return json.dumps(await self.events.get())

    async def close(self):
        self.socket_closed = True


class Scope:
    def __init__(self, wire):
        self.wire = wire

    async def __aenter__(self):
        return self.wire

    async def __aexit__(self, *args):
        return None


def session_fixture(settings):
    settings.openai_api_key = "test-key"
    settings.live_close_timeout_seconds = 0.2
    wire, playback = Wire(), FakePlayback()
    urls = []

    def factory(url, **kwargs):
        urls.append(url)
        return Scope(wire)

    session = OpenAIGptLiveSession(
        settings=settings,
        playback=playback,
        knowledge=FakeKnowledge(),
        telemetry=TelemetryBroker(),
        connection_factory=factory,
    )
    return session, wire, playback, urls


async def until(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0.005)


def audio(samples):
    return {"type": "session.output_audio.delta", "delta": base64.b64encode(samples).decode("ascii")}


async def backend(session, kind, *, delegation="d1", **fields):
    await session.handle_server_event(
        {
            "type": "response.event",
            "delegation_id": delegation,
            "event": {"type": kind, **fields},
        }
    )


def test_live_start_uses_native_continuous_audio_and_only_existing_tools(settings):
    event = build_session_start(settings)
    assert event["type"] == "session.start"
    config = event["session"]
    assert config["model"] == "gpt-live-1"
    assert config["audio"]["format"] == {"type": "audio/pcm", "rate": 16000}
    assert config["store"] is False
    assert len(config["delegation"]["responses"]["tools"]) == 8
    assert config["delegation"]["responses"]["parallel_tool_calls"] is False
    assert "turn_detection" not in config["audio"]
    assert "output_modalities" not in config and "tools" not in config


@pytest.mark.asyncio
async def test_failed_connect_releases_context_and_can_retry(settings):
    session, wire, _, _ = session_fixture(settings)
    exits = []

    class FailedScope:
        async def __aenter__(self):
            raise ConnectionError("test connection failure")

        async def __aexit__(self, *args):
            exits.append(True)

    session.connection_factory = lambda *args, **kwargs: FailedScope()
    with pytest.raises(ConnectionError):
        await session.start()
    assert exits == [True]
    assert session._connection_context is None and not session.running
    session.connection_factory = lambda *args, **kwargs: Scope(wire)
    await session.start()
    await session.stop()


@pytest.mark.asyncio
async def test_close_discards_pending_context_without_poisoning_voice_health(settings):
    session, _, _, _ = session_fixture(settings)
    await session.start()
    session._live_closing = True
    await session.handle_server_event(
        {"type": "error", "error": {"code": "context_injection_incomplete", "type": "server_error"}}
    )
    assert session.diagnostics()["last_error"] is None
    await session.stop()
    assert session.diagnostics()["finalization_confirmed"] is True


@pytest.mark.asyncio
async def test_microphone_stays_enabled_during_playback_and_greeting(settings):
    session, wire, playback, urls = session_fixture(settings)
    await session.start()
    try:
        await session.greet()
        playback.active = True
        payload = np.full(1280, 1000, dtype=np.int16).tobytes()
        assert session.feed_audio(payload)
        await until(lambda: session.diagnostics()["simultaneous_input_output_frames"] > 0)
        assert session.diagnostics()["input_enabled"] is True
        assert urls == ["wss://api.openai.com/v1/live/sessions"]
        assert wire.sent[0]["type"] == "session.start"
        kinds = {item["type"] for item in wire.sent}
        assert "session.input_audio.append" in kinds
        assert not kinds & {"session.update", "input_audio_buffer.append", "response.cancel"}
    finally:
        playback.active = False
        await session.stop()


@pytest.mark.asyncio
async def test_continuous_silence_does_not_renew_activity_or_keep_playback_busy(settings):
    session, wire, playback, _ = session_fixture(settings)
    await session.start()
    try:
        before = session._last_activity
        assert session.feed_audio(bytes(2560))
        await session.handle_server_event(audio(bytes(640)))
        assert session._last_activity == before
        assert not session._writer.busy and not playback.pushed
        await until(lambda: any(e["type"] == "session.input_audio.append" for e in wire.sent))
        appends = [e for e in wire.sent if e["type"] == "session.input_audio.append"]
        assert all(not any(base64.b64decode(e["audio"])) for e in appends)
    finally:
        await session.stop()


@pytest.mark.asyncio
async def test_forwarded_empty_completion_uses_output_items_and_deduplicates_calls(settings):
    session, wire, _, _ = session_fixture(settings)
    await session.start()
    called = []
    session.tools.set_handler(
        "notify_staff", lambda arguments, call_id: called.append(call_id) or {"status": "ok"}
    )
    try:
        await backend(session, "response.created", response={"id": "r1"})
        item = {
            "type": "function_call",
            "call_id": "c1",
            "name": "notify_staff",
            "arguments": '{"reason":"visitor_needs_help"}',
        }
        await backend(session, "response.output_item.done", item=item)
        await backend(session, "response.output_item.done", item=item)
        await backend(session, "response.completed", response={"id": "r1", "output": []})
        await until(lambda: any(e["type"] == "response.create" for e in wire.sent))
        assert called == ["c1"]
        results = [e for e in wire.sent if e["type"] == "response.item.create"]
        assert len(results) == 1
        assert results[0]["item"]["call_id"] == "c1"
        assert not any(e["type"] == "conversation.item.create" for e in wire.sent)
    finally:
        await session.stop()


@pytest.mark.asyncio
async def test_concurrent_delegations_keep_calls_with_the_correct_response(settings):
    session, _, _, _ = session_fixture(settings)
    await session.start()
    try:
        await backend(session, "response.created", delegation="a", response={"id": "ra"})
        await backend(session, "response.created", delegation="b", response={"id": "rb"})
        await backend(
            session,
            "response.output_item.done",
            delegation="a",
            item={
                "type": "function_call",
                "call_id": "ca",
                "name": "wait_for_user",
                "arguments": "{}",
            },
        )
        assert [item["call_id"] for item in session._calls_by_response["ra"]] == ["ca"]
        assert not session._calls_by_response["rb"]
    finally:
        await session.stop()


@pytest.mark.asyncio
async def test_slow_tool_does_not_block_audio_receive_and_late_result_is_discarded(settings):
    session, wire, playback, _ = session_fixture(settings)
    gate = asyncio.Event()

    async def slow(arguments, call_id):
        await gate.wait()
        return {"status": "ok"}

    session.tools.set_handler("notify_staff", slow)
    await session.start()
    await backend(session, "response.created", response={"id": "r1"})
    await backend(
        session,
        "response.output_item.done",
        item={
            "type": "function_call",
            "call_id": "c1",
            "name": "notify_staff",
            "arguments": '{"reason":"visitor_needs_help"}',
        },
    )
    await backend(session, "response.completed", response={"id": "r1"})
    await session.handle_server_event(audio(np.full(320, 500, dtype=np.int16).tobytes()))
    await until(lambda: bool(playback.pushed))
    await session.stop()
    gate.set()
    assert not any(e["type"] == "response.item.create" for e in wire.sent)


@pytest.mark.asyncio
async def test_local_notice_mutes_cloud_and_stale_output_cannot_clear_local_playback(settings):
    session, wire, playback, _ = session_fixture(settings)
    await session.start()
    try:
        await session.begin_local_playback()
        before = list(playback.operations)
        assert not session.feed_audio(np.full(320, 400, dtype=np.int16).tobytes())
        await session.handle_server_event(audio(np.full(320, 500, dtype=np.int16).tobytes()))
        assert not playback.pushed
        assert playback.operations == before
        assert session._server_input_muted
        session.end_local_playback()
        assert session._input_suspended  # Remains local during the confirmation window.
        session.resume_input()
        await until(lambda: not session._input_suspended)
        assert not session._server_input_muted
        assert any(e["type"] == "session.input_audio.unmute" for e in wire.sent)
    finally:
        await session.stop()


@pytest.mark.asyncio
async def test_close_waits_for_final_usage_and_uses_cumulative_snapshots(settings):
    session, wire, _, _ = session_fixture(settings)
    await session.start()
    await session.handle_server_event({"type": "session.usage.updated", "usage": {"seconds": 1}})
    await session.handle_server_event({"type": "session.usage.updated", "usage": {"seconds": 2}})
    assert session.diagnostics()["usage_seconds"] == 2
    await session.stop()
    assert wire.socket_closed
    assert session.diagnostics()["finalization_confirmed"]
    assert session.diagnostics()["usage_seconds"] == 3


@pytest.mark.asyncio
async def test_user_transcript_does_not_send_realtime_cancel_or_truncate(settings):
    session, wire, _, _ = session_fixture(settings)
    await session.start()
    try:
        await session.handle_server_event(
            {
                "type": "session.input_transcript.delta",
                "delta": "Actually, wait",
                "start_ms": 1,
                "end_ms": 500,
            }
        )
        assert not any(e["type"] in {"response.cancel", "conversation.item.truncate"} for e in wire.sent)
        assert all("Actually" not in str(event) for event in session.telemetry.snapshot())
    finally:
        await session.stop()


@pytest.mark.asyncio
async def test_network_burst_and_timeline_pause_are_preserved_during_speech(settings):
    session, _, playback, _ = session_fixture(settings)
    await session.start()
    try:
        speech = np.full(3200, 500, dtype=np.int16).tobytes()
        pause = bytes(6400)
        # A 600 ms burst previously exceeded the 350 ms limit and cut speech.
        await session.handle_server_event(audio(speech))
        await session.handle_server_event(audio(pause))
        await session.handle_server_event(audio(speech))
        assert not playback.pushed  # The receiver never waits for the reserve.
        await session._writer.queue.join()
        assert [len(chunk) for chunk in playback.pushed] == [3200, 3200, 3200]
        assert not np.any(playback.pushed[1])
        assert session.diagnostics()["dropped_output_frames"] == 0
        assert not any(
            e["payload"].get("event") == "playback_backlog_discarded" for e in session.telemetry.snapshot()
        )
        # Even if the sink still reports queued silence as active, silence
        # must eventually stop feeding itself and blocking local grammar.
        session._last_output_speech_at = time.monotonic() - 1
        playback.active = True
        await session.handle_server_event(audio(pause))
        assert session._writer.queue.empty() and not session._writer.busy
        assert len(playback.pushed) == 3
    finally:
        await session.stop()


@pytest.mark.asyncio
async def test_attended_chat_pauses_idle_watchdog_but_not_max_session_age(settings):
    session, _, _, _ = session_fixture(settings)
    session.idle_timeout_paused = lambda: True
    await session.start()
    try:
        session._last_activity = time.monotonic() - 100
        await asyncio.sleep(1.1)
        assert session.running
        session._started_at = time.monotonic() - settings.realtime_max_session_seconds - 1
        async with asyncio.timeout(3):
            while session.running:
                await asyncio.sleep(0.01)
        assert session.diagnostics()["last_stop_reason"] == "max_session_age"
    finally:
        await session.stop()
