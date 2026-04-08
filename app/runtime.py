"""Application runtime context."""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import Settings
from app.models.domain import BookingResult, FaceMatchResult, FaceQuality
from app.models.enums import BookingStatus, FaceQualityFlag, HealthStatus
from app.services.audio.asr import CommandRecognizer
from app.services.audio.audio_router import AudioRouter
from app.services.audio.barge_in_guard import BargeInGuard
from app.services.audio.microphone import NullMicrophoneAdapter, SoundDeviceMicrophoneAdapter
from app.services.audio.vad import VoiceActivityDetector
from app.services.audio.wakeword import WakeWordDetector
from app.services.policy.decision_engine import DecisionEngine
from app.services.reachy.robot import ReachyRobotAdapter, SimulatedRobotAdapter
from app.services.salto.auth import SaltoOAuthManager
from app.services.salto.locks import HttpSaltoClient, MockSaltoClient
from app.services.salto.unlock_service import UnlockService
from app.services.slack.booking_service import MlaiBookingClient
from app.services.system.telemetry import TelemetryBroker
from app.services.vision.camera import ReachyCameraAdapter, SimulationCameraAdapter
from app.services.vision.enrollment import EnrollmentService
from app.services.vision.face_detect import YuNetFaceDetector
from app.services.vision.face_embed import DummyFaceEmbedder, SFaceEmbedder
from app.services.vision.face_match import FaceMatcher, IdentificationService
from app.state_machine import OfficeBouncerStateMachine
from app.storage.db import create_session_factory, init_db, session_scope
from app.storage.repositories import (
    AccessEventRepository,
    SaltoTokenRepository,
    SqlFaceStore,
    SystemHealthRepository,
    UserRepository,
)


logger = logging.getLogger(__name__)


class ScopedUserRepository:
    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    def upsert(self, **kwargs):
        with session_scope(self.session_factory) as session:
            return UserRepository(session).upsert(**kwargs)

    def get(self, user_id: str):
        with session_scope(self.session_factory) as session:
            return UserRepository(session).get(user_id)

    def get_by_slack_id(self, slack_user_id: str):
        with session_scope(self.session_factory) as session:
            return UserRepository(session).get_by_slack_id(slack_user_id)

    def list(self):
        with session_scope(self.session_factory) as session:
            return UserRepository(session).list()


class ScopedFaceStore:
    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    def replace_embeddings(self, user_id: str, embeddings):
        with session_scope(self.session_factory) as session:
            SqlFaceStore(session).replace_embeddings(user_id, embeddings)

    def list_embeddings(self):
        with session_scope(self.session_factory) as session:
            return SqlFaceStore(session).list_embeddings()


class ScopedAccessEvents:
    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    def create(self, **kwargs):
        with session_scope(self.session_factory) as session:
            return AccessEventRepository(session).create(**kwargs)

    def list_recent(self, limit: int = 50):
        with session_scope(self.session_factory) as session:
            return AccessEventRepository(session).list_recent(limit=limit)


class ScopedSystemHealth:
    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    def set_status(self, component: str, status: HealthStatus, details: dict[str, Any] | None = None) -> None:
        with session_scope(self.session_factory) as session:
            SystemHealthRepository(session).set_status(component, status, details)

    def snapshot(self) -> dict[str, HealthStatus]:
        with session_scope(self.session_factory) as session:
            return SystemHealthRepository(session).snapshot()

    def list(self):
        with session_scope(self.session_factory) as session:
            return SystemHealthRepository(session).list()


class ScopedSaltoTokens:
    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    def get(self, provider: str = "salto_ks"):
        with session_scope(self.session_factory) as session:
            return SaltoTokenRepository(session).get(provider=provider)

    def save(self, **kwargs):
        with session_scope(self.session_factory) as session:
            return SaltoTokenRepository(session).save(**kwargs)


@dataclass
class SimulationOutcome:
    face_result: FaceMatchResult


class FakeIdentificationService:
    """Test and simulation helper."""

    def __init__(self, result: FaceMatchResult) -> None:
        self.result = result

    async def identify(self) -> FaceMatchResult:
        return self.result

    def has_recent_face(self) -> bool:
        return self.result.quality.face_count > 0

    def probe_face_presence(self) -> bool:
        return self.result.quality.face_count > 0


class AppContext:
    """Container for runtime services."""

    def __init__(
        self,
        *,
        settings: Settings,
        booking_client=None,
        identification_service=None,
        salto_client=None,
        camera=None,
        microphone=None,
        robot=None,
        telemetry: TelemetryBroker | None = None,
        salto_transport=None,
        booking_transport=None,
    ) -> None:
        self.settings = settings
        self.telemetry = telemetry or TelemetryBroker()
        self.session_factory = create_session_factory(settings)
        init_db(self.session_factory)

        self.users = ScopedUserRepository(self.session_factory)
        self.face_store = ScopedFaceStore(self.session_factory)
        self.access_events = ScopedAccessEvents(self.session_factory)
        self.system_health = ScopedSystemHealth(self.session_factory)
        self.salto_tokens = ScopedSaltoTokens(self.session_factory)

        self.microphone = microphone or self._build_microphone()
        self.robot = robot or self._build_robot()
        self.camera = camera or self._build_camera()

        self.detector = YuNetFaceDetector()
        self.embedder = SFaceEmbedder()
        self.matcher = FaceMatcher()
        self.identification_service = identification_service or IdentificationService(
            settings=settings,
            camera=self.camera,
            detector=self.detector,
            embedder=self.embedder,
            matcher=self.matcher,
            face_store=self.face_store,
        )
        self.enrollment_service = EnrollmentService(
            settings=settings,
            detector=self.detector,
            embedder=self.embedder,
            user_repo=self.users,
            face_store=self.face_store,
        )

        self.booking_client = booking_client or MlaiBookingClient(settings, transport=booking_transport)
        self.salto_auth = SaltoOAuthManager(settings=settings, token_repo=self.salto_tokens, transport=salto_transport)
        self.salto_client = salto_client or self._build_salto_client(salto_transport)
        self.unlock_service = UnlockService(settings=settings, auth=self.salto_auth, client=self.salto_client)
        self.guard = BargeInGuard(settings.cooldown_seconds)
        self.decision_engine = DecisionEngine(face_match_threshold=settings.face_match_threshold)

        self.state_machine = OfficeBouncerStateMachine(
            settings=settings,
            decision_engine=self.decision_engine,
            identification_service=self.identification_service,
            booking_client=self.booking_client,
            unlock_service=self.unlock_service,
            access_events=self.access_events,
            system_health=self.system_health,
            user_repo=self.users,
            robot=self.robot,
            telemetry=self.telemetry,
            guard=self.guard,
        )

        self.audio_router = AudioRouter(
            settings=settings,
            microphone=self.microphone,
            vad=VoiceActivityDetector(),
            wakeword=WakeWordDetector(settings.wakeword_model_path),
            asr=CommandRecognizer(settings.vosk_model_path),
            guard=self.guard,
            telemetry=self.telemetry,
            robot_is_speaking=self.robot.is_speaking,
            face_present_supplier=self._face_present,
            on_wake=self.state_machine.handle_wake_word,
            on_command=self.state_machine.handle_command,
        )

    async def start(self) -> None:
        self.system_health.set_status("database", HealthStatus.HEALTHY)
        await self.robot.start()
        self.system_health.set_status("robot", HealthStatus.HEALTHY)
        await self.camera.start()
        self.system_health.set_status("camera", HealthStatus.HEALTHY)
        self.system_health.set_status("booking", HealthStatus.HEALTHY)
        self.system_health.set_status("salto", HealthStatus.HEALTHY)
        self.system_health.set_status("audio", HealthStatus.HEALTHY)

        if self.settings.reachy_simulation and self.salto_tokens.get() is None:
            self.salto_tokens.save(
                access_token="mock-access-token",
                refresh_token="mock-refresh-token",
                expires_at=datetime.now(timezone.utc) + timedelta(hours=12),
            )

        await self.audio_router.start()
        await self.state_machine.boot()
        self.telemetry.publish("startup", {"ready": True})

    async def stop(self) -> None:
        await self.audio_router.stop()
        await self.camera.stop()
        await self.robot.stop()
        if hasattr(self.booking_client, "close"):
            await self.booking_client.close()
        if hasattr(self.salto_auth, "close"):
            await self.salto_auth.close()
        if hasattr(self.salto_client, "close"):
            await self.salto_client.close()

    def live_health(self) -> dict[str, str]:
        return {"status": "ok"}

    def readiness(self) -> dict[str, Any]:
        snapshot = self.system_health.snapshot()
        ready = all(status != HealthStatus.ERROR for status in snapshot.values())
        return {
            "ready": ready,
            "components": {component: status.value for component, status in snapshot.items()},
        }

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.access_events.list_recent(limit=limit)
        return [
            {
                "id": row.id,
                "timestamp": row.timestamp.isoformat(),
                "detected_user_id": row.detected_user_id,
                "matched_confidence": row.matched_confidence,
                "booking_status": row.booking_status,
                "decision": row.decision,
                "reason_code": row.reason_code,
                "salto_result": row.salto_result,
                "latency_ms": row.latency_ms,
                "metadata": row.metadata_json,
            }
            for row in rows
        ]

    def list_users(self) -> list[dict[str, Any]]:
        return [
            {
                "id": user.id,
                "full_name": user.full_name,
                "slack_user_id": user.slack_user_id,
                "slack_display_name": user.slack_display_name,
                "company": user.company,
                "active": user.active,
            }
            for user in self.users.list()
        ]

    async def booking_check(self, user_id: str) -> dict[str, Any]:
        user = self.users.get(user_id)
        if user is None:
            raise KeyError(user_id)
        result = await self.booking_client.get_active_booking(slack_user_id=user.slack_user_id, now=datetime.now(timezone.utc))
        return {
            "user_id": user.id,
            "slack_user_id": user.slack_user_id,
            "active": result.active,
            "status": result.status.value,
            "booking_date": result.booking_date.isoformat() if result.booking_date else None,
            "detail": result.detail,
            "metadata": result.metadata,
        }

    async def enroll(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = self.enrollment_service.enroll(
            full_name=payload["full_name"],
            slack_user_id=payload["slack_user_id"],
            slack_display_name=payload["slack_display_name"],
            company=payload.get("company"),
            samples=payload["samples"],
        )
        if inspect.isawaitable(result):
            return await result
        return result

    async def speak(self, text: str) -> None:
        await self.robot.speak(text)

    def get_camera_snapshot(self) -> bytes | None:
        return self.camera.get_snapshot_bytes()

    def audio_diagnostics(self) -> dict[str, Any]:
        return self.audio_router.diagnostics()

    def camera_diagnostics(self) -> dict[str, Any]:
        frame = self.camera.get_frame()
        if frame is None:
            return {"available": False}
        detections = self.detector.detect(frame)
        return {
            "available": True,
            "shape": list(frame.shape),
            "faces": [{"bbox": list(face.bbox), "confidence": face.confidence} for face in detections],
        }

    async def simulate_attempt(self) -> None:
        await self.state_machine.handle_wake_word()

    def salto_authorize_url(self) -> str:
        url, _ = self.salto_auth.build_authorize_url()
        return url

    async def handle_salto_callback(self, code: str, state: str) -> dict[str, Any]:
        return await self.salto_auth.handle_callback(code=code, state=state)

    def _build_microphone(self):
        if self.settings.simulation_use_microphone:
            try:
                return SoundDeviceMicrophoneAdapter(self.settings)
            except Exception:
                logger.debug("Falling back to null microphone", exc_info=True)
        return NullMicrophoneAdapter(self.settings)

    def _build_robot(self):
        if self.settings.reachy_enabled and not self.settings.reachy_simulation:
            return ReachyRobotAdapter(settings=self.settings, microphone=self.microphone, telemetry=self.telemetry)
        return SimulatedRobotAdapter(settings=self.settings, microphone=self.microphone, telemetry=self.telemetry)

    def _build_camera(self):
        if self.settings.reachy_enabled and not self.settings.reachy_simulation:
            return ReachyCameraAdapter(
                settings=self.settings,
                telemetry=self.telemetry,
                robot_provider=lambda: getattr(self.robot, "_reachy", None),
            )
        return SimulationCameraAdapter(settings=self.settings, telemetry=self.telemetry)

    def _build_salto_client(self, transport=None):
        if self.settings.reachy_simulation or not self.settings.salto_client_id:
            return MockSaltoClient()
        return HttpSaltoClient(self.settings, transport=transport)

    def _face_present(self) -> bool:
        if hasattr(self.identification_service, "probe_face_presence"):
            return bool(self.identification_service.probe_face_presence())
        if hasattr(self.identification_service, "has_recent_face"):
            return bool(self.identification_service.has_recent_face())
        return True
