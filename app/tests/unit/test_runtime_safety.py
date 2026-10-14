from __future__ import annotations

import base64

import pytest

from app.config import Settings
from app.models.domain import UnlockResult
from app.models.enums import CommandIntent, RuntimeMode
from app.runtime import AppContext, FakeIdentificationService
from app.services.door import PlaceholderDoorAction
from app.services.slack.booking_service import MockBookingClient


class TrackingDoorAction:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute_for_event(self, access_event_id: str) -> UnlockResult:
        self.calls.append(access_event_id)
        return UnlockResult(success=True, metadata={"placeholder": True, "actuated": False})


def test_unsafe_production_configuration_is_rejected(tmp_path) -> None:
    settings = Settings(
        app_env=RuntimeMode.PRODUCTION,
        db_url=f"sqlite:///{tmp_path / 'production.db'}",
        reachy_enabled=False,
        reachy_simulation=True,
        physical_unlock_enabled=False,
    )

    with pytest.raises(RuntimeError, match="Unsafe production configuration"):
        AppContext(settings=settings)


@pytest.mark.asyncio
async def test_production_freeze_uses_non_actuating_placeholder(tmp_path) -> None:
    detector = tmp_path / "detector.onnx"
    embedder = tmp_path / "embedder.onnx"
    vad = tmp_path / "vad.onnx"
    vosk = tmp_path / "vosk"
    sherpa = tmp_path / "sherpa"
    keywords = tmp_path / "keywords.txt"
    detector.write_bytes(b"test-placeholder")
    embedder.write_bytes(b"test-placeholder")
    vad.write_bytes(b"test-placeholder")
    vosk.mkdir()
    sherpa.mkdir()
    keywords.write_text("test", encoding="utf-8")
    settings = Settings(
        app_env=RuntimeMode.PRODUCTION,
        db_url=f"sqlite:///{tmp_path / 'production.db'}",
        reachy_enabled=True,
        reachy_simulation=False,
        physical_unlock_enabled=False,
        tts_engine="realtime",
        realtime_enabled=True,
        openai_api_key="test-openai-key",
        realtime_safety_identifier_key="test-safety-identifier-key",
        data_encryption_key=base64.urlsafe_b64encode(b"k" * 32).decode("ascii"),
        openai_realtime_model="gpt-realtime-2.1",
        face_detector_model_path=str(detector),
        face_embedder_model_path=str(embedder),
        vad_model_path=str(vad),
        vosk_model_path=str(vosk),
        sherpa_kws_model_dir=str(sherpa),
        sherpa_kws_keywords_path=str(keywords),
        mlai_backend_url="https://mlai.example.test",
        mlai_backend_api_key="scoped-production-test-key",
        mlai_public_knowledge_token="scoped-public-test-key",
        allow_dev_endpoints=False,
    )
    context = AppContext(settings=settings)

    assert isinstance(context.door_action, PlaceholderDoorAction)
    assert context.door_action.is_placeholder is True
    assert context.door_action.actuates_hardware is False

    await context.stop()


@pytest.mark.asyncio
async def test_spoken_open_door_command_does_not_execute_placeholder(
    settings, known_face_result
) -> None:
    door_action = TrackingDoorAction()
    context = AppContext(
        settings=settings,
        identification_service=FakeIdentificationService(known_face_result),
        booking_client=MockBookingClient(booked_today={"U123"}),
        door_action=door_action,
    )
    await context.start()
    try:
        await context.state_machine.handle_command(CommandIntent.OPEN_DOOR)
    finally:
        await context.stop()

    assert door_action.calls == []


@pytest.mark.asyncio
async def test_disabled_cloud_conversation_is_never_reported_ready(settings) -> None:
    context = AppContext(settings=settings, booking_client=MockBookingClient())
    await context.start()
    try:
        readiness = context.readiness()
    finally:
        await context.stop()

    assert readiness["conversation_ready"] is False
    assert readiness["components"]["realtime"] == "ERROR"
    assert readiness["components"]["public_knowledge"] == "ERROR"


@pytest.mark.asyncio
async def test_real_face_pipeline_is_not_misreported_as_liveness(settings) -> None:
    context = AppContext(settings=settings, booking_client=MockBookingClient())
    await context.start()
    try:
        readiness = context.readiness()
    finally:
        await context.stop()

    assert readiness["access_ready"] is False
    assert readiness["components"]["face_liveness"] == "ERROR"
