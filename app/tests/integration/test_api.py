from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models.domain import FaceMatchResult, FaceQuality, UnlockResult
from app.models.enums import Decision, FaceQualityFlag, RuntimeMode
from app.runtime import AppContext, FakeIdentificationService
from app.services.slack.booking_service import MockBookingClient


class FakeEnrollmentService:
    async def enroll(self, payload):  # type: ignore[no-untyped-def]
        return {"user_id": "fake-user", "embedding_count": len(payload["samples"])}


def build_context(
    settings,
    face_result,
    booking_client=None,
    door_action=None,
    *,
    active: bool = True,
    biometric_consent: bool = True,
    access_revoked: bool = False,
) -> AppContext:
    context = AppContext(
        settings=settings,
        identification_service=FakeIdentificationService(face_result),
        booking_client=booking_client or MockBookingClient(),
        door_action=door_action,
    )
    context.enrollment_service = FakeEnrollmentService()
    context.users.upsert(
        user_id="user-1",
        full_name="Sam Donegan",
        slack_user_id="U123",
        slack_display_name="sam",
        company="MLAI",
        active=active,
        biometric_consent=biometric_consent,
        access_revoked=access_revoked,
    )
    return context


class FailingDoorAction:
    async def execute_for_event(self, access_event_id: str) -> UnlockResult:
        return UnlockResult(success=False, detail="placeholder_failed")


class NullDetailDoorAction:
    async def execute_for_event(self, access_event_id: str) -> UnlockResult:
        return UnlockResult(success=False, detail=None)


class RaisingDoorAction:
    async def execute_for_event(self, access_event_id: str) -> UnlockResult:
        raise RuntimeError("provider exploded")


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


def test_readiness_returns_503_when_access_safeguards_are_missing(settings) -> None:
    app = create_app(settings, context_factory=lambda current: AppContext(settings=current))

    with TestClient(app) as client:
        ready = client.get("/healthz/ready")

    assert ready.status_code == 503
    assert ready.json()["access_ready"] is False
    assert ready.json()["components"]["face_liveness"] == "ERROR"


def test_voice_test_endpoint_reports_disabled_voice(settings) -> None:
    app = create_app(settings, context_factory=lambda current: AppContext(settings=current))

    with TestClient(app) as client:
        response = client.post("/api/admin/test-conversation")

    assert response.status_code == 409
    assert response.json()["detail"] == "OpenAI voice is not enabled"


def test_dashboard_disconnect_releases_telemetry_subscription(settings) -> None:
    app = create_app(settings, context_factory=lambda current: AppContext(settings=current))
    with TestClient(app) as client:
        broker = app.state.context.telemetry
        for index in range(250):
            broker.publish("test", {"index": index})
        with client.websocket_connect("/ws/telemetry") as websocket:
            assert websocket.receive_json()["payload"]["index"] == 150

        async def wait_unsubscribed():
            async with asyncio.timeout(1):
                while broker._subscribers:
                    await asyncio.sleep(0.001)

        client.portal.call(wait_unsubscribed)
        assert not broker._subscribers


def test_access_attempt_allows_booked_user(settings, known_face_result: FaceMatchResult) -> None:
    booking_client = MockBookingClient(booked_today={"U123"})
    app = create_app(
        settings,
        context_factory=lambda current: build_context(
            current, known_face_result, booking_client=booking_client
        ),
    )

    with TestClient(app) as client:
        response = client.post("/api/admin/simulate/attempt")
        events = client.get("/api/admin/events").json()

    assert response.status_code == 200
    assert events[0]["reason_code"] == "ALLOW_ACTIVE_BOOKING"
    assert events[0]["door_action_result"] == "placeholder_would_open"
    assert events[0]["metadata"]["unlock_result"] == {
        "placeholder": True,
        "actuated": False,
    }


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


def test_access_attempt_denies_when_door_placeholder_fails(
    settings, known_face_result: FaceMatchResult
) -> None:
    booking_client = MockBookingClient(booked_today={"U123"})
    app = create_app(
        settings,
        context_factory=lambda current: build_context(
            current,
            known_face_result,
            booking_client=booking_client,
            door_action=FailingDoorAction(),
        ),
    )

    with TestClient(app) as client:
        client.post("/api/admin/simulate/attempt")
        events = client.get("/api/admin/events").json()

    assert events[0]["reason_code"] == "DENY_DOOR_ACTION_ERROR"
    assert events[0]["decision"] == Decision.DENY.value
    assert "opening" not in str(events[0]["metadata"]).lower()
    assert app.state.context.state_machine.state.value == "LISTENING_LOW_POWER"


def test_access_attempt_denies_when_door_placeholder_detail_is_none(
    settings, known_face_result: FaceMatchResult
) -> None:
    app = create_app(
        settings,
        context_factory=lambda current: build_context(
            current,
            known_face_result,
            booking_client=MockBookingClient(booked_today={"U123"}),
            door_action=NullDetailDoorAction(),
        ),
    )

    with TestClient(app) as client:
        client.post("/api/admin/simulate/attempt")
        event = client.get("/api/admin/events").json()[0]

    assert event["decision"] == Decision.DENY.value
    assert event["reason_code"] == "DENY_DOOR_ACTION_ERROR"
    assert "opening" not in str(event["metadata"]).lower()


def test_access_attempt_denies_when_door_placeholder_throws(
    settings, known_face_result: FaceMatchResult
) -> None:
    app = create_app(
        settings,
        context_factory=lambda current: build_context(
            current,
            known_face_result,
            booking_client=MockBookingClient(booked_today={"U123"}),
            door_action=RaisingDoorAction(),
        ),
    )

    with TestClient(app) as client:
        client.post("/api/admin/simulate/attempt")
        event = client.get("/api/admin/events").json()[0]

    assert event["decision"] == Decision.ERROR.value
    assert event["reason_code"] == "ERROR_INTERNAL"
    assert "opening" not in str(event["metadata"]).lower()


@pytest.mark.parametrize(
    ("member_state", "expected_reason"),
    [
        ({"active": False}, "DENY_USER_INACTIVE"),
        ({"biometric_consent": False}, "DENY_CONSENT_REQUIRED"),
        ({"access_revoked": True}, "DENY_ACCESS_REVOKED"),
    ],
)
def test_access_attempt_denies_ineligible_member(
    settings,
    known_face_result: FaceMatchResult,
    member_state: dict,
    expected_reason: str,
) -> None:
    app = create_app(
        settings,
        context_factory=lambda current: build_context(
            current,
            known_face_result,
            booking_client=MockBookingClient(booked_today={"U123"}),
            **member_state,
        ),
    )

    with TestClient(app) as client:
        client.post("/api/admin/simulate/attempt")
        event = client.get("/api/admin/events").json()[0]

    assert event["decision"] == Decision.DENY.value
    assert event["reason_code"] == expected_reason


def test_production_does_not_register_developer_routes(settings) -> None:
    production = settings.model_copy(update={"app_env": RuntimeMode.PRODUCTION, "allow_dev_endpoints": True})
    app = create_app(production)
    paths = set(app.openapi()["paths"])

    assert "/healthz/live" in paths
    assert "/api/admin/events" not in paths
    assert "/api/admin/enroll" not in paths
    assert "/camera/snapshot" not in paths
    assert "/ws/telemetry" not in paths
