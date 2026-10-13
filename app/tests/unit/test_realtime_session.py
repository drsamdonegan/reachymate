from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest

from app.services.knowledge import PublicKnowledgeAnswer, PublicKnowledgeCitation
from app.services.realtime.session import OpenAIRealtimeSession, build_session_update
from app.services.system.telemetry import TelemetryBroker


class FakeConnection:
    def __init__(self, operations: list[str] | None = None) -> None:
        self.sent: list[dict] = []
        self.operations = operations if operations is not None else []

    async def send(self, message: str) -> None:
        event = json.loads(message)
        self.sent.append(event)
        self.operations.append(f"send:{event['type']}")

    async def recv(self) -> str:
        raise RuntimeError("not used")

    async def close(self) -> None:
        return None


class FakePlayback:
    def __init__(self, operations: list[str] | None = None) -> None:
        self.played = 0
        self.pushed: list[np.ndarray] = []
        self.operations = operations if operations is not None else []
        self.active = False

    async def start_playing(self) -> None:
        return None

    async def stop_playing(self) -> None:
        return None

    async def push_audio(self, samples: np.ndarray) -> None:
        self.pushed.append(samples)

    async def clear_output(self) -> None:
        self.operations.append("clear")

    def output_sample_rate(self) -> int:
        return 16_000

    def output_frames_played(self) -> int:
        return self.played

    def output_active(self) -> bool:
        return self.active


class FakeKnowledge:
    def __init__(self) -> None:
        self.queries: list[tuple[str, str | None]] = []

    async def answer(self, query: str, *, request_id: str | None = None) -> PublicKnowledgeAnswer:
        self.queries.append((query, request_id))
        return PublicKnowledgeAnswer(
            status="answered",
            answer="MLAI runs a monthly public meetup.",
            citations=(
                PublicKnowledgeCitation(
                    item_id="item-1",
                    public_key="meetup",
                    revision=1,
                    title="MLAI meetup",
                    published_at="2026-08-01T00:00:00Z",
                ),
            ),
            request_id=request_id,
        )


def make_session(settings, *, operations: list[str] | None = None):
    playback = FakePlayback(operations)
    knowledge = FakeKnowledge()
    session = OpenAIRealtimeSession(
        settings=settings,
        playback=playback,
        knowledge=knowledge,  # type: ignore[arg-type]
        telemetry=TelemetryBroker(),
    )
    connection = FakeConnection(operations)
    session.connection = connection
    return session, playback, knowledge, connection


def test_session_update_uses_current_realtime_21_shape(settings) -> None:
    event = build_session_update(settings)
    session = event["session"]

    assert event["type"] == "session.update"
    assert session["type"] == "realtime"
    assert session["model"] == "gpt-realtime-2.1"
    assert session["output_modalities"] == ["audio"]
    assert session["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert session["audio"]["input"]["turn_detection"] == {
        "type": "server_vad",
        "threshold": 0.8,
        "prefix_padding_ms": 300,
        "silence_duration_ms": 600,
        "interrupt_response": True,
        "create_response": True,
    }
    assert session["audio"]["output"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert session["reasoning"] == {"effort": "low"}
    assert len(session["tools"]) == 7
    assert session["tools"][0]["name"] == "search_public_knowledge"
    assert "tracing" not in session


@pytest.mark.asyncio
async def test_interruption_clears_playback_then_truncates_at_played_audio(settings) -> None:
    operations: list[str] = []
    session, playback, _knowledge, connection = make_session(settings, operations=operations)
    session._begin_output_item("item-123", content_index=0)
    session._active_output_received_frames = 48_000
    playback.played = 24_000  # 1.5 seconds at the target's 16 kHz output rate.

    await session.handle_server_event({"type": "input_audio_buffer.speech_started"})

    assert operations == ["clear", "send:conversation.item.truncate"]
    assert connection.sent[-1] == {
        "type": "conversation.item.truncate",
        "item_id": "item-123",
        "content_index": 0,
        "audio_end_ms": 1500,
    }


@pytest.mark.asyncio
async def test_interruption_never_truncates_beyond_received_audio(settings) -> None:
    session, playback, _knowledge, connection = make_session(settings)
    session._begin_output_item("item-short", content_index=0)
    session._active_output_received_frames = 8_000
    playback.played = 24_000

    await session.handle_server_event({"type": "input_audio_buffer.speech_started"})

    assert connection.sent[-1]["audio_end_ms"] == 500


@pytest.mark.asyncio
async def test_response_done_executes_only_public_knowledge_tool(settings) -> None:
    session, _playback, knowledge, connection = make_session(settings)
    event = {
        "type": "response.done",
        "response": {
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": "search_public_knowledge",
                    "call_id": "call-1",
                    "arguments": '{"query":"When is the MLAI meetup?"}',
                }
            ],
        },
    }

    await session.handle_server_event(event)
    await session.handle_server_event(event)  # Duplicate response delivery is idempotent.
    await asyncio.gather(*session._tasks)

    assert knowledge.queries == [("When is the MLAI meetup?", "call-1")]
    assert [sent["type"] for sent in connection.sent] == [
        "conversation.item.create",
        "response.create",
    ]
    tool_output = json.loads(connection.sent[0]["item"]["output"])
    assert tool_output["status"] == "answered"
    assert tool_output["citations"][0]["public_key"] == "meetup"
    assert "untrusted public data" in tool_output["content_policy"]


@pytest.mark.asyncio
async def test_unknown_tool_cannot_reach_door_controller(settings) -> None:
    session, _playback, knowledge, connection = make_session(settings)
    await session.handle_server_event(
        {
            "type": "response.done",
            "response": {
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "name": "unlock_door",
                        "call_id": "call-bad",
                        "arguments": "{}",
                    }
                ]
            },
        }
    )
    await asyncio.gather(*session._tasks)

    assert knowledge.queries == []
    output = json.loads(connection.sent[0]["item"]["output"])
    assert output["status"] == "unavailable"


@pytest.mark.asyncio
async def test_session_updated_marks_current_schema_as_accepted(settings) -> None:
    session, _playback, _knowledge, _connection = make_session(settings)

    assert session._configured.is_set() is False
    await session.handle_server_event({"type": "session.updated", "session": {}})

    assert session._configured.is_set() is True


@pytest.mark.asyncio
async def test_access_status_is_out_of_band_and_has_no_tools(settings) -> None:
    session, _playback, _knowledge, connection = make_session(settings)

    await session.announce("The door remains locked.")

    response = connection.sent[0]["response"]
    assert response["conversation"] == "none"
    assert response["input"] == []
    assert response["tools"] == []
    assert response["tool_choice"] == "none"
    assert response["metadata"]["source"] == "local_access_controller"
    assert "The door remains locked." in response["instructions"]


@pytest.mark.asyncio
async def test_transcript_content_is_not_retained_in_telemetry(settings) -> None:
    session, _playback, _knowledge, _connection = make_session(settings)

    await session.handle_server_event(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": "My private visitor utterance",
        }
    )

    event = session.telemetry.snapshot()[-1]
    assert event["payload"]["content_retained"] is False
    assert event["payload"]["characters"] == len("My private visitor utterance")
    assert "private visitor" not in str(event)


def test_feed_audio_is_bounded_and_resamples_locally(settings) -> None:
    settings.realtime_input_queue_chunks = 2
    session, _playback, _knowledge, _connection = make_session(settings)
    session._running = True
    session._input_enabled = True
    chunk = np.full(1280, 1000, dtype=np.int16).tobytes()

    assert session.feed_audio(chunk)
    assert session.feed_audio(chunk)
    assert session.feed_audio(chunk)
    diagnostics = session.diagnostics()
    assert diagnostics["queued_input_chunks"] == 2
    assert diagnostics["dropped_input_chunks"] == 1


@pytest.mark.asyncio
async def test_greeting_discards_noise_and_enables_input_after_output_drains(settings) -> None:
    settings.realtime_initial_output_settle_ms = 10
    session, playback, _knowledge, connection = make_session(settings)
    session._running = True
    chunk = np.full(1280, 1000, dtype=np.int16).tobytes()

    assert session.feed_audio(chunk) is False
    await session.greet()

    assert connection.sent[-1]["type"] == "response.create"
    assert session.diagnostics()["input_enabled"] is False
    playback.active = True
    await session.handle_server_event(
        {"type": "response.done", "response": {"status": "completed", "output": []}}
    )
    await asyncio.sleep(0)
    assert session.diagnostics()["input_enabled"] is False

    playback.active = False
    await asyncio.sleep(0.08)
    assert session.diagnostics()["input_enabled"] is True
    assert session.feed_audio(chunk) is True
