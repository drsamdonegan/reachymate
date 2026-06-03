from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app
from app.models.domain import BookingResult, FaceMatchResult, FaceQuality
from app.models.enums import BookingStatus, FaceQualityFlag
from app.runtime import AppContext, FakeIdentificationService
from app.services.salto.locks import MockSaltoClient
from app.services.slack.booking_service import MockBookingClient


class FakeEnrollmentService:
    async def enroll(self, payload):  # type: ignore[no-untyped-def]
        return {"user_id": "fake-user", "embedding_count": len(payload["samples"])}


def build_context(settings, face_result, booking_client=None, salto_client=None) -> AppContext:
    context = AppContext(
        settings=settings,
        identification_service=FakeIdentificationService(face_result),
        booking_client=booking_client or MockBookingClient(),
        salto_client=salto_client or MockSaltoClient(),
    )
    context.enrollment_service = FakeEnrollmentService()
    context.users.upsert(
        user_id="user-1",
        full_name="Sam Donegan",
        slack_user_id="U123",
        slack_display_name="sam",
        company="MLAI",
        active=True,
    )
    return context


def test_health_and_events_endpoints(settings, known_face_result: FaceMatchResult) -> None:
    app = create_app(settings, context_factory=lambda current: build_context(current, known_face_result))

    with TestClient(app) as client:
        live = client.get("/healthz/live")
        ready = client.get("/healthz/ready")
        events = client.get("/api/admin/events")

    assert live.status_code == 200
    assert ready.status_code == 200
    assert ready.json()["ready"] is True
    assert events.status_code == 200


def test_access_attempt_allows_booked_user(settings, known_face_result: FaceMatchResult) -> None:
    booking_client = MockBookingClient(booked_today={"U123"})
    app = create_app(
        settings,
        context_factory=lambda current: build_context(current, known_face_result, booking_client=booking_client),
    )

    with TestClient(app) as client:
        response = client.post("/api/admin/simulate/attempt")
        events = client.get("/api/admin/events").json()

    assert response.status_code == 200
    assert events[0]["reason_code"] == "ALLOW_ACTIVE_BOOKING"


def test_access_attempt_denies_unknown_face(settings) -> None:
    face_result = FaceMatchResult(
        matched_user_id=None,
        similarity=None,
        quality=FaceQuality(
            usable=True,
            flags=[FaceQualityFlag.OK],
            face_count=1,
            face_size=180,
            blur_score=140,
            brightness=110,
        ),
        bbox=(40, 40, 180, 180),
        stable_frames=3,
    )
    app = create_app(settings, context_factory=lambda current: build_context(current, face_result))

    with TestClient(app) as client:
        client.post("/api/admin/simulate/attempt")
        events = client.get("/api/admin/events").json()

    assert events[0]["reason_code"] == "DENY_UNKNOWN_FACE"


def test_access_attempt_denies_multiple_faces(settings) -> None:
    face_result = FaceMatchResult(
        matched_user_id=None,
        similarity=None,
        quality=FaceQuality(
            usable=False,
            flags=[FaceQualityFlag.MULTIPLE_FACES],
            face_count=2,
            face_size=120,
            blur_score=120,
            brightness=100,
        ),
    )
    app = create_app(settings, context_factory=lambda current: build_context(current, face_result))

    with TestClient(app) as client:
        client.post("/api/admin/simulate/attempt")
        events = client.get("/api/admin/events").json()

    assert events[0]["reason_code"] == "DENY_MULTIPLE_FACES"


def test_access_attempt_denies_when_salto_fails(settings, known_face_result: FaceMatchResult) -> None:
    booking_client = MockBookingClient(booked_today={"U123"})
    salto_client = MockSaltoClient(fail_unlock=True)
    app = create_app(
        settings,
        context_factory=lambda current: build_context(
            current,
            known_face_result,
            booking_client=booking_client,
            salto_client=salto_client,
        ),
    )

    with TestClient(app) as client:
        client.post("/api/admin/simulate/attempt")
        events = client.get("/api/admin/events").json()

    assert events[0]["reason_code"] == "DENY_SALTO_ERROR"
