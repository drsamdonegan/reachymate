"""Application runtime context."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.config import Settings
from app.models.domain import FaceMatchResult
from app.models.enums import AppState, CommandIntent, HealthStatus, RuntimeMode
from app.services.audio.asr import CommandRecognizer
from app.services.audio.audio_router import AudioRouter
from app.services.audio.barge_in_guard import BargeInGuard
from app.services.audio.doa import DoaMonitor
from app.services.audio.microphone import NullMicrophoneAdapter, SoundDeviceMicrophoneAdapter
from app.services.audio.vad import VoiceActivityDetector
from app.services.audio.wakeword import WakeWordDetector
from app.services.audio.xvf import XvfController
from app.services.conversation import ConversationCoordinator
from app.services.door import PlaceholderDoorAction
from app.services.events.checkin import EventCheckin
from app.services.events.express import LumaExpressHandoff
from app.services.events.luma import LumaClient
from app.services.knowledge import PublicKnowledgeClient
from app.services.live import OpenAIGptLiveSession
from app.services.policy.decision_engine import DecisionEngine
from app.services.reachy.daemon_supervisor import DaemonSupervisor
from app.services.reachy.media import ReachyMediaAdapter
from app.services.reachy.robot import ReachyRobotAdapter, SimulatedRobotAdapter
from app.services.realtime import OpenAIRealtimeSession
from app.services.routing.models import RouteRequest, RoutingContext, StaffReason
from app.services.routing.router import VisitorRouter
from app.services.routing.skills import build_frontdesk_client
from app.services.slack.booking_service import MlaiBookingClient
from app.services.system.health import (
    ACCESS_REQUIRED_COMPONENTS,
    CONVERSATION_REQUIRED_COMPONENTS,
    components_ready,
)
from app.services.system.key_provider import BackendKeyProvider
from app.services.system.telemetry import TelemetryBroker
from app.services.vision.attention import AttentionService, CameraCalibration
from app.services.vision.av_gate import AVGate, MouthActivity
from app.services.vision.camera import ReachyCameraAdapter, SimulationCameraAdapter
from app.services.vision.enrollment import EnrollmentService
from app.services.vision.face_detect import YuNetFaceDetector
from app.services.vision.face_embed import SFaceEmbedder
from app.services.vision.face_match import FaceMatcher, IdentificationService
from app.services.vision.model_view import ModelCameraView, capture_model_view
from app.services.vision.pose import FacePoseEstimator
from app.state_machine import OfficeBouncerStateMachine
from app.storage.db import create_session_factory, init_db, session_scope
from app.storage.encryption import SensitiveDataCipher
from app.storage.repositories import (
    AccessEventRepository,
    SqlFaceStore,
    SystemHealthRepository,
    UnlockCommandRepository,
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
    def __init__(self, session_factory, *, cipher, allow_plaintext: bool) -> None:
        self.session_factory = session_factory
        self.cipher = cipher
        self.allow_plaintext = allow_plaintext

    def replace_embeddings(self, user_id: str, embeddings):
        with session_scope(self.session_factory) as session:
            SqlFaceStore(
                session,
                cipher=self.cipher,
                allow_plaintext=self.allow_plaintext,
            ).replace_embeddings(user_id, embeddings)

    def list_embeddings(self, *, model_id=None):
        with session_scope(self.session_factory) as session:
            return SqlFaceStore(
                session,
                cipher=self.cipher,
                allow_plaintext=self.allow_plaintext,
            ).list_embeddings(model_id=model_id)

    def enroll_atomic(self, **kwargs):
        with session_scope(self.session_factory) as session:
            return SqlFaceStore(
                session, cipher=self.cipher, allow_plaintext=self.allow_plaintext
            ).enroll_atomic(**kwargs)


class ScopedAccessEvents:
    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    def create(self, **kwargs):
        with session_scope(self.session_factory) as session:
            return AccessEventRepository(session).create(**kwargs)

    def list_recent(self, limit: int = 50):
        with session_scope(self.session_factory) as session:
            return AccessEventRepository(session).list_recent(limit=limit)

    def update(self, **kwargs):
        with session_scope(self.session_factory) as session:
            return AccessEventRepository(session).update(**kwargs)


class ScopedUnlockCommands:
    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    def reserve(self, access_event_id: str):
        with session_scope(self.session_factory) as session:
            return UnlockCommandRepository(session).reserve(access_event_id)

    def update(self, access_event_id: str, **kwargs):
        with session_scope(self.session_factory) as session:
            return UnlockCommandRepository(session).update(access_event_id, **kwargs)

    def get_for_event(self, access_event_id: str):
        with session_scope(self.session_factory) as session:
            return UnlockCommandRepository(session).get_for_event(access_event_id)


class ScopedSystemHealth:
    def __init__(self, session_factory, *, max_age_seconds: int) -> None:
        self.session_factory = session_factory
        self.max_age_seconds = max_age_seconds
        self.on_persistence_failure = None
        self._cache = {}

    def set_status(self, component: str, status: HealthStatus, details: dict[str, Any] | None = None) -> None:
        self._cache[component] = (status, time.monotonic())
        try:
            with session_scope(self.session_factory) as session:
                SystemHealthRepository(session).set_status(component, status, details)
        except Exception as exc:
            if self.on_persistence_failure:
                self.on_persistence_failure()
            logger.warning("Health persistence failed: %s", type(exc).__name__)

    def snapshot(self) -> dict[str, HealthStatus]:
        try:
            with session_scope(self.session_factory) as session:
                return SystemHealthRepository(session).snapshot(max_age_seconds=self.max_age_seconds)
        except Exception:
            if self.on_persistence_failure:
                self.on_persistence_failure()
            now = time.monotonic()
            snapshot = {
                name: status if now - updated < self.max_age_seconds else HealthStatus.ERROR
                for name, (status, updated) in self._cache.items()
            }
            snapshot["database"] = HealthStatus.ERROR
            return snapshot

    def list(self):
        with session_scope(self.session_factory) as session:
            return SystemHealthRepository(session).list()


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
        door_action=None,
        camera=None,
        microphone=None,
        robot=None,
        telemetry: TelemetryBroker | None = None,
        booking_transport=None,
    ) -> None:
        self.settings = settings
        self._validate_production_settings()
        self.telemetry = telemetry or TelemetryBroker()
        self.session_factory = create_session_factory(settings)
        init_db(self.session_factory)
        self.sensitive_cipher = SensitiveDataCipher(settings.data_encryption_key)
        self.key_provider = BackendKeyProvider(settings) if settings.kek_endpoint else None

        self.users = ScopedUserRepository(self.session_factory)
        self.face_store = ScopedFaceStore(
            self.session_factory,
            cipher=self.sensitive_cipher,
            allow_plaintext=not settings.is_production and settings.reachy_simulation,
        )
        self.access_events = ScopedAccessEvents(self.session_factory)
        self.unlock_commands = ScopedUnlockCommands(self.session_factory)
        self.system_health = ScopedSystemHealth(
            self.session_factory,
            max_age_seconds=settings.health_freshness_seconds,
        )
        self.reachy_media = None
        if settings.reachy_enabled and not settings.reachy_simulation:
            self.reachy_media = ReachyMediaAdapter(
                settings=settings,
                telemetry=self.telemetry,
            )

        self.microphone = microphone or self.reachy_media or self._build_microphone()
        self.robot = robot or self._build_robot()
        self.camera = camera or self._build_camera()
        self.luma_express = LumaExpressHandoff(
            settings.data_dir / "luma-browser-handoff.json" if settings.app_env != RuntimeMode.TEST else None
        )
        self._event_mode_lock = asyncio.Lock()
        if self.luma_express.active:
            self.camera.set_external_owner(True)
        self.event_checkin = EventCheckin(
            camera=self.camera,
            camera_name=(
                "Reachy Mini Camera" if isinstance(self.camera, ReachyCameraAdapter) else "Simulation camera"
            ),
            client=LumaClient(settings.luma_api_key),
            telemetry=self.telemetry,
            camera_allowed=self._event_camera_allowed,
            on_confirmed=self._welcome_event_guest,
            on_scanner_stop=self._release_event_camera,
            external_mode=self.luma_express.snapshot,
        )
        if settings.luma_event_id:
            from app.services.events.qr import EVENT_ID

            if EVENT_ID.fullmatch(settings.luma_event_id):
                self.event_checkin.event = {"id": settings.luma_event_id, "name": "Selected Luma event"}
        if self.luma_express.active and self.luma_express.event_id:
            self.event_checkin.event = {"id": self.luma_express.event_id, "name": "Luma scanner event"}
        self.access_disabled = False
        self._overlay_recovered_at = None
        self._local_models_ready = False
        self._routing_task = None
        self._idle_task = None
        self._safe_hold_task = None
        self._hardware_fault_lock = asyncio.Lock()
        self._track_ref = str(uuid4())

        self.detector = YuNetFaceDetector(
            settings.face_detector_model_path,
            strict=settings.is_production or not settings.reachy_simulation,
        )
        self.embedder = SFaceEmbedder(
            settings.face_embedder_model_path,
            allow_fallback=not settings.is_production and settings.reachy_simulation,
        )
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
        calibration = (
            CameraCalibration.load(settings.camera_calibration_path)
            if settings.camera_calibration_path
            else None
        )
        self.attention = AttentionService(
            camera=self.camera,
            detector=self.detector,
            calibration=calibration,
            on_face=self.robot.look_at_face,
            uncalibrated_demo=settings.uncalibrated_conversation_demo,
        )
        self.attention.worker.on_fault = self._hardware_fault
        if settings.mouth_landmarker_path and settings.mouth_landmarker_sha256:
            import hashlib

            if (
                hashlib.sha256(Path(settings.mouth_landmarker_path).read_bytes()).hexdigest()
                != settings.mouth_landmarker_sha256
            ):
                raise RuntimeError("Mouth landmarker asset failed integrity verification")
            self.attention.mouth = MouthActivity(settings.mouth_landmarker_path)
        if isinstance(self.identification_service, IdentificationService):
            self.identification_service.expected_track_supplier = self.attention.latest
            self.identification_service.pose_estimator = (
                FacePoseEstimator(calibration) if calibration else None
            )
        self.enrollment_service.pose_estimator = FacePoseEstimator(calibration) if calibration else None
        self.doa = None
        self.daemon_supervisor = None
        if self.reachy_media is not None:
            self.doa = DoaMonitor(
                XvfController(profile=settings.xvf_profile),
                motion_time=lambda: getattr(getattr(self.robot, "motion", None), "last_motion_at", 0.0),
            )
            self.reachy_media.xvf = self.doa.controller
        self.av_gate = AVGate(
            mode=settings.av_gate_mode,
            doa_validated=settings.doa_bearing_validated,
            sample_rate=settings.sample_rate_hz,
            zero_tail_seconds=0.7 if settings.av_zero_tail_enabled else None,
            dither_validated=settings.dither_validated,
        )
        self.frontdesk_client = build_frontdesk_client(settings)
        self.visitor_router = VisitorRouter(
            self.frontdesk_client,
            record=self._record_route,
            poll_seconds=settings.frontdesk_poll_seconds,
            wait_seconds=settings.frontdesk_wait_seconds,
        )

        self.booking_client = booking_client or MlaiBookingClient(settings, transport=booking_transport)
        self.door_action = door_action or PlaceholderDoorAction(commands=self.unlock_commands)
        self._validate_production_composition()
        self.guard = BargeInGuard(settings.cooldown_seconds)
        self.decision_engine = DecisionEngine(face_match_threshold=settings.face_match_threshold)

        self.state_machine = OfficeBouncerStateMachine(
            settings=settings,
            decision_engine=self.decision_engine,
            identification_service=self.identification_service,
            booking_client=self.booking_client,
            door_action=self.door_action,
            access_events=self.access_events,
            system_health=self.system_health,
            user_repo=self.users,
            robot=self.robot,
            telemetry=self.telemetry,
            guard=self.guard,
        )
        self.system_health.on_persistence_failure = self._disable_access_for_database

        self.public_knowledge = PublicKnowledgeClient(settings)
        self.realtime = None
        if settings.realtime_enabled:
            if not isinstance(self.microphone, ReachyMediaAdapter):
                raise RuntimeError("OpenAI voice requires the shared Reachy media adapter")
            session_type = OpenAIGptLiveSession if settings.voice_api == "gpt_live" else OpenAIRealtimeSession
            self.realtime = session_type(
                settings=settings,
                playback=self.microphone,
                knowledge=self.public_knowledge,
                telemetry=self.telemetry,
            )
            if isinstance(self.robot, ReachyRobotAdapter):
                self.robot.set_realtime_speech(self.realtime.announce)
        self.conversation = ConversationCoordinator(
            settings=settings,
            guard=self.guard,
            state_machine=self.state_machine,
            realtime=self.realtime,
            system_health=self.system_health,
            play_notice=self._play_consent_notice,
            person_in_view=self._face_present,
            robot_speaking=self.robot.is_speaking,
            prepare_engagement=self._prepare_engagement,
            on_idle=self._end_engagement,
        )
        if self.realtime is not None:
            self.realtime.on_closed = self.conversation.on_realtime_closed
            self.realtime.idle_timeout_paused = lambda: self.conversation.attended_chat_active
            self.realtime.registry.set_handler("route_visitor", self._route_visitor)
            self.realtime.registry.set_handler("notify_staff", self._notify_staff)
            self.realtime.registry.set_handler("get_visitor_request_status", self._visitor_status)
            self.realtime.registry.set_handler("robot_motion", self._robot_motion)
            if isinstance(self.realtime, OpenAIGptLiveSession) and settings.camera_vision_enabled:
                self.realtime.registry.set_handler("inspect_camera", self._inspect_camera)
                self.realtime.vision_snapshot_provider = self._capture_model_view
                self.realtime.vision_allowed = self._model_vision_allowed

        self.conversation.suspended = self.luma_express.active
        self.vad = VoiceActivityDetector(settings.vad_model_path, threshold=settings.vad_threshold)
        self.wakeword = WakeWordDetector(
            settings.wakeword_model_path,
            engine=settings.wakeword_engine,
            vosk_model_path=settings.vosk_model_path,
            phrase=settings.wake_phrase,
            sherpa_model_dir=settings.sherpa_kws_model_dir,
            sherpa_keywords_path=settings.sherpa_kws_keywords_path,
            sherpa_threshold=settings.sherpa_kws_threshold,
            sherpa_score=settings.sherpa_kws_score,
            vosk_fallback_enabled=settings.wakeword_vosk_fallback_enabled,
        )
        self.command_recognizer = CommandRecognizer(settings.vosk_model_path)
        self.conversation.reset_local_grammar = self.command_recognizer.reset
        if self.realtime:
            self.realtime.on_fallback = self._play_cloud_fallback
        local_audio_enabled = not isinstance(self.microphone, NullMicrophoneAdapter)

        self.audio_router = AudioRouter(
            settings=settings,
            microphone=self.microphone,
            vad=self.vad,
            wakeword=self.wakeword,
            asr=self.command_recognizer,
            guard=self.guard,
            telemetry=self.telemetry,
            robot_is_speaking=self.robot.is_speaking,
            face_present_supplier=self._face_present,
            on_wake=self.conversation.handle_wake,
            on_command=self.conversation.handle_command,
            on_engaged_audio=self.conversation.feed_audio,
            enabled=local_audio_enabled,
            av_gate=self.av_gate,
            attention_supplier=self.attention.latest,
            doa_supplier=self.doa.latest if self.doa else lambda: None,
            on_activity=self.conversation.gated_activity,
            on_track_changed=self.conversation.update_engaged_track,
            attended_chat_supplier=lambda: self.conversation.attended_chat_active,
            on_fault=self._hardware_fault,
            on_barge_in=self.realtime.cancel_response
            if self.realtime and settings.local_barge_in_enabled and settings.voice_api == "realtime"
            else None,
            barge_in_allowed=lambda: bool(
                self.realtime and self.realtime.running and not self.realtime._input_suspended
            ),
        )
        self._health_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        try:
            if self.key_provider:
                await self._refresh_key()
            self.system_health.set_status("database", HealthStatus.HEALTHY)
            await self.robot.start()
            if isinstance(self.robot, ReachyRobotAdapter) and self.robot.motion and self.robot.daemon:
                self.daemon_supervisor = DaemonSupervisor(
                    self.robot.daemon, self.robot.motion, on_fault=self._hardware_fault
                )
                await self.daemon_supervisor.start()
            robot_ready = not isinstance(self.robot, ReachyRobotAdapter) or self.robot._reachy is not None
            self.system_health.set_status(
                "robot", HealthStatus.HEALTHY if robot_ready else HealthStatus.ERROR
            )
            await self.camera.start()
            camera_ready = self.camera.get_frame() is not None if self.reachy_media is None else False
            self.system_health.set_status(
                "camera",
                HealthStatus.DEGRADED
                if self.luma_express.active
                else HealthStatus.HEALTHY
                if camera_ready
                else HealthStatus.ERROR,
            )
            if self.reachy_media is not None:
                self.reachy_media.release_idle_camera()
            await self._probe_external_health()
            self.system_health.set_status(
                "door_action",
                HealthStatus.HEALTHY,
                {"mode": "placeholder", "actuates_hardware": False},
            )
            audio_ready = not isinstance(self.microphone, NullMicrophoneAdapter)
            local_wake_ready = False
            local_wake_failed = False
            if audio_ready:
                try:
                    self.vad.load()
                    self.wakeword.load()
                    self.command_recognizer.load()
                    local_wake_ready = True
                    self._local_models_ready = True
                except Exception as exc:
                    local_wake_failed = True
                    self.audio_router.set_enabled(False)
                    self.system_health.set_status(
                        "local_wake",
                        HealthStatus.ERROR,
                        {"error": type(exc).__name__},
                    )
                    if self.settings.is_production or not self.settings.reachy_simulation:
                        raise
            if not local_wake_failed:
                self.system_health.set_status(
                    "local_wake",
                    HealthStatus.HEALTHY if local_wake_ready else HealthStatus.DEGRADED,
                )
            identity_ready = isinstance(self.identification_service, FakeIdentificationService)
            if not identity_ready and (self.settings.is_production or not self.settings.reachy_simulation):
                try:
                    self.detector.load()
                    self.embedder.load()
                    identity_ready = self.detector.available and self.embedder.available
                except Exception as exc:
                    self.system_health.set_status(
                        "face_identity",
                        HealthStatus.ERROR,
                        {"error": type(exc).__name__},
                    )
                    raise
            elif not identity_ready:
                identity_ready = (
                    Path(self.settings.face_detector_model_path).is_file()
                    and Path(self.settings.face_embedder_model_path).is_file()
                )
            self.system_health.set_status(
                "face_identity", HealthStatus.HEALTHY if identity_ready else HealthStatus.ERROR
            )
            liveness_is_simulated = not self.settings.is_production and isinstance(
                self.identification_service, FakeIdentificationService
            )
            self.system_health.set_status(
                "face_liveness",
                HealthStatus.HEALTHY if liveness_is_simulated else HealthStatus.ERROR,
                {
                    "mode": "simulated" if liveness_is_simulated else "unavailable",
                    "quality_checks_are_not_pad": True,
                },
            )
            realtime_ready = (
                self.settings.realtime_enabled
                and self.realtime is not None
                and self.settings.openai_api_key is not None
            )
            self.system_health.set_status(
                "realtime", HealthStatus.HEALTHY if realtime_ready else HealthStatus.ERROR
            )

            await self.audio_router.start()
            await self.attention.start()
            if self.doa:
                try:
                    await self.doa.start()
                except Exception as exc:
                    self.system_health.set_status("xvf", HealthStatus.DEGRADED, {"error": type(exc).__name__})
            audio_detail: dict[str, Any] = {}
            if self.reachy_media is not None:
                media_audio = self.reachy_media.diagnostics()
                processing_ready = (
                    not self.settings.audio_processing_enabled or media_audio["audio_processing_enabled"]
                )
                audio_ready = audio_ready and processing_ready
                audio_detail = {
                    "echo_cancellation": media_audio["audio_processing_enabled"],
                    "processing_error": media_audio["audio_processing_error"],
                }
            self.system_health.set_status(
                "audio",
                HealthStatus.HEALTHY if audio_ready else HealthStatus.ERROR,
                audio_detail,
            )
            await self.state_machine.boot()
            self._health_task = asyncio.create_task(
                self._health_monitor_loop(),
                name="component-health-monitor",
            )
            self.telemetry.publish("startup", {"ready": True})
        except Exception:
            await self.stop()
            raise

    async def stop(self) -> None:
        self._stopping = True
        tasks = (self._routing_task, self._idle_task, self._safe_hold_task, self._health_task)
        for task in tasks:
            if task and task is not asyncio.current_task():
                task.cancel()
        self._health_task = None
        try:
            async with asyncio.timeout(12):
                await asyncio.gather(
                    *(task for task in tasks if task and task is not asyncio.current_task()),
                    return_exceptions=True,
                )
                components = [
                    self.event_checkin,
                    self.daemon_supervisor,
                    self.attention,
                    self.doa,
                    self.audio_router,
                    self.conversation,
                    self.camera,
                    self.booking_client,
                    self.public_knowledge,
                    self.frontdesk_client,
                    self.key_provider,
                ]
                for component in components:
                    if component is None:
                        continue
                    close = getattr(component, "stop", None) or getattr(component, "close", None)
                    if close:
                        try:
                            await asyncio.wait_for(close(), timeout=2)
                        except Exception as exc:
                            self.telemetry.publish(
                                "shutdown_failed",
                                {"component": type(component).__name__, "error": type(exc).__name__},
                            )
        except TimeoutError:
            logger.error("Runtime shutdown cleanup exceeded budget")
        finally:
            if self.attention.mouth:
                try:
                    self.attention.mouth.close()
                except Exception:
                    logger.warning("Mouth model cleanup failed")
            try:
                await asyncio.wait_for(self.robot.stop(), timeout=15)
            except Exception as exc:
                self.telemetry.publish("shutdown_failed", {"component": "robot", "error": type(exc).__name__})

    def live_health(self) -> dict[str, str]:
        return {"status": "ok"}

    def readiness(self) -> dict[str, Any]:
        snapshot = self.system_health.snapshot()
        access_ready = components_ready(snapshot, ACCESS_REQUIRED_COMPONENTS)
        conversation_ready = components_ready(snapshot, CONVERSATION_REQUIRED_COMPONENTS)
        return {
            "ready": access_ready,
            "access_ready": access_ready,
            "conversation_ready": conversation_ready,
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
                # The database column keeps its legacy name until a later schema cleanup.
                "door_action_result": row.salto_result,
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
                "biometric_consent": user.biometric_consent,
                "access_revoked": user.access_revoked,
            }
            for user in self.users.list()
        ]

    async def booking_check(self, user_id: str) -> dict[str, Any]:
        user = self.users.get(user_id)
        if user is None:
            raise KeyError(user_id)
        result = await self.booking_client.get_active_booking(
            slack_user_id=user.slack_user_id, now=datetime.now(timezone.utc)
        )
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
        if self.luma_express.active:
            raise ValueError("Close Luma's scanner and return the camera before enrollment")
        if not self.sensitive_cipher.enabled:
            raise ValueError("Encrypted storage is required before enrollment")
        import hashlib

        from app.services.conversation.fallback_audio import CONSENT_NOTICE

        if (
            payload.get("notice_version") != "biometric-notice-v1"
            or payload.get("notice_sha256") != hashlib.sha256(CONSENT_NOTICE.encode()).hexdigest()
        ):
            raise ValueError("The displayed biometric notice must match the current version")
        if self.settings.reachy_enabled and not self.settings.reachy_simulation:
            from types import SimpleNamespace

            from app.scripts.enroll_user import verify_member

            await verify_member(
                self.settings,
                SimpleNamespace(
                    member_lookup_url=None,
                    slack_user_id=payload["slack_user_id"],
                    full_name=payload["full_name"],
                ),
            )
        result = self.enrollment_service.enroll(
            full_name=payload["full_name"],
            slack_user_id=payload["slack_user_id"],
            slack_display_name=payload["slack_display_name"],
            company=payload.get("company"),
            biometric_consent=payload["biometric_consent"],
            samples=payload["samples"],
            operator_id=payload.get("operator_id"),
            notice_version=payload.get("notice_version"),
            notice_sha256=payload.get("notice_sha256"),
            greet_by_name=payload.get("greet_by_name", False),
            reinstate=payload.get("reinstate", False),
        )
        if inspect.isawaitable(result):
            return await result
        return result

    async def speak(self, text: str) -> None:
        if self.luma_express.active:
            raise RuntimeError("Voice is paused for Luma Express")
        if self.realtime is None or not self.realtime.running:
            raise RuntimeError("No active voice session")
        await self.robot.speak(text)

    def _event_camera_allowed(self) -> bool:
        return (
            not getattr(self, "_stopping", False)
            and not self.luma_express.active
            and self.state_machine.state != AppState.SAFE_HOLD
            and not (self.realtime and self.realtime._input_suspended)
        )

    async def _release_event_camera(self) -> None:
        if self.reachy_media and not self.attention.enabled:
            await asyncio.to_thread(self.reachy_media.release_idle_camera)
        if not getattr(self, "_stopping", False) and self.state_machine.state == AppState.IDLE:
            if self._idle_task and self._idle_task is not asyncio.current_task():
                self._idle_task.cancel()
            self._idle_task = asyncio.create_task(self._park_after_idle())

    async def _welcome_event_guest(self) -> None:
        if self.realtime and self.realtime.running and not self.realtime._input_suspended:
            await self.robot.speak("Your event check-in is confirmed. Welcome!")

    async def start_luma_express(self) -> dict:
        async with self._event_mode_lock:
            event = self.event_checkin.event
            if not event:
                raise ValueError("Select a Luma event first")
            if self.state_machine.state == AppState.SAFE_HOLD or getattr(self, "_stopping", False):
                raise ValueError("Resolve the hardware fault before handing off the camera")
            if self.reachy_media and self.settings.reachy_media_backend != "lite_direct":
                raise ValueError("Luma Express requires the direct USB camera backend")
            if not callable(getattr(self.camera, "set_external_owner", None)):
                raise ValueError("This camera does not support a browser handoff")
            self.luma_express.begin(event["id"])
            self.conversation.suspended = True
            try:
                await self.event_checkin.stop_scanning()
                await self.conversation.suspend()
                speech = getattr(self.robot, "speech", None)
                if speech:
                    speech.drop_pending()
                self.attention.set_enabled(False)
                # Finishes any frame currently being read, then prevents every app camera consumer.
                await asyncio.to_thread(self.camera.set_external_owner, True)
                await self.event_checkin.next_guest(clear_for_handoff=True)
                await self._cancel_idle_for_wake()
                motion = getattr(self.robot, "motion", None)
                if motion:
                    motion.stop_expression()
                wake = getattr(self.robot, "wake", None)
                if wake:
                    await wake(0.0)
                if self.state_machine.state == AppState.SAFE_HOLD or getattr(self, "_stopping", False):
                    raise RuntimeError("Hardware became unavailable")
                await self.state_machine.transition(AppState.IDLE)
                self.luma_express.ready = True
                self.system_health.set_status("camera", HealthStatus.DEGRADED, {"mode": "luma_handoff"})
                self.telemetry.publish("event_scanner_mode", {"mode": "luma_browser_handoff"})
            except Exception:
                # Leave the persisted guard active on partial failure. The browser may already
                # own the camera from an earlier run; never reconnect voice automatically.
                raise ValueError(
                    "Luma handoff did not finish. Retry, or close Luma and return the camera"
                ) from None
        return self.event_checkin.snapshot()

    async def finish_luma_express(self, scanner_closed: bool) -> dict:
        if not scanner_closed:
            raise ValueError("Close Luma's scanner tab before returning the camera")
        async with self._event_mode_lock:
            if not self.luma_express.active:
                return self.event_checkin.snapshot()
            self.luma_express.finish()
            await asyncio.to_thread(self.camera.set_external_owner, False)
            self.conversation.suspended = False
            await self.camera.start()
            await self._release_event_camera()
            self.telemetry.publish("event_scanner_mode", {"mode": "reachy"})
        return self.event_checkin.snapshot()

    async def start_voice_test(self) -> dict[str, Any]:
        """Open the selected wake-scoped voice path from the loopback diagnostics UI."""

        if self.luma_express.active:
            raise RuntimeError("Close Luma's scanner and return the camera before starting chat")
        if self.realtime is None:
            raise RuntimeError("OpenAI voice is not enabled")
        await self.conversation.handle_wake(attended=self.settings.attended_chat_enabled)
        return self.realtime.diagnostics()

    async def stop_voice_test(self) -> dict[str, str]:
        await self.conversation.handle_command(CommandIntent.CANCEL)
        return {"status": "stopped"}

    async def send_voice_request(self, text: str) -> dict[str, str]:
        if self.luma_express.active:
            raise RuntimeError("Voice is paused for Luma Express")
        if self.realtime is None or not self.realtime.running or self.realtime._input_suspended:
            raise RuntimeError("Start a voice test and finish any local consent step first")
        sender = getattr(self.realtime, "send_text", None)
        if sender is None:
            raise RuntimeError("Text requests require GPT-Live")
        await sender(text)
        return {"status": "sent"}

    def get_camera_snapshot(self) -> bytes | None:
        # Dashboard polling must not wake the dormant camera or repeatedly
        # tear down the capture that the engagement tracker is using.
        if self.luma_express.active:
            return None
        if self.reachy_media is not None and not self.attention.enabled and not self.event_checkin.scanning:
            return None
        return self.camera.get_snapshot_bytes()

    def audio_diagnostics(self) -> dict[str, Any]:
        diagnostics = self.audio_router.diagnostics()
        diagnostics["conversation"] = self.conversation.diagnostics()
        if self.reachy_media is not None:
            diagnostics["reachy_media"] = self.reachy_media.diagnostics()
        if self.realtime is not None:
            diagnostics["realtime"] = self.realtime.diagnostics()
            diagnostics["voice"] = diagnostics["realtime"]
        observation = self.attention.latest()
        diagnostics["attention"] = {
            "enabled": self.attention.enabled,
            "degraded_reason": self.attention.degraded_reason,
            "face_present": observation is not None and observation.in_zone,
        }
        power = getattr(self.robot, "power", None)
        if power:
            diagnostics["power"] = {
                "parked": power.parked,
                "parking": power.parking,
                "joint_sag_limit_radians": power.joint_sag_limit,
                "bench_tolerance": self.settings.uncalibrated_conversation_demo,
            }
        motion = getattr(self.robot, "motion", None)
        if motion:
            diagnostics["motion"] = motion.expression_diagnostics()
        return diagnostics

    def robot_motion_capabilities(self) -> dict:
        capabilities = getattr(self.robot, "motion_capabilities", None)
        return capabilities() if capabilities else {"ready": False, "axes": [], "dances": [], "emotions": []}

    def _model_vision_allowed(self) -> bool:
        return bool(
            self.settings.camera_vision_enabled
            and not self.luma_express.active
            and self.conversation.attended_chat_active
            and self.state_machine.state == AppState.ENGAGED
            and self.attention.enabled
            and not self.event_checkin.scanning
            and self.guard.is_engaged()
            and self.realtime is not None
            and self.realtime.running
            and not self.realtime._input_suspended
            and isinstance(self.camera, ReachyCameraAdapter)
        )

    async def _inspect_camera(self, arguments: dict, _call_id: str) -> dict:
        if self.event_checkin.scanning:
            return {"status": "unavailable", "reason": "finish_event_qr_scanning_first"}
        if not self._model_vision_allowed():
            return {"status": "unavailable", "reason": "start_attended_chat_or_finish_local_check"}
        return {"status": "ready", "question": arguments["question"]}

    async def _capture_model_view(self) -> ModelCameraView | None:
        if not self._model_vision_allowed():
            return None
        motion = getattr(self.robot, "motion", None)
        if motion is not None:
            async with asyncio.timeout(4.5):
                while motion.expression_diagnostics()["active"]:
                    if not self._model_vision_allowed():
                        return None
                    await asyncio.sleep(0.05)
        view = await asyncio.to_thread(capture_model_view, self.camera)
        return view if self._model_vision_allowed() else None

    async def _robot_motion(self, arguments: dict, _call_id: str = "dashboard") -> dict:
        from app.services.reachy.motion.commands import RobotMotionRequest

        request = RobotMotionRequest.model_validate(arguments)
        if self.luma_express.active and request.action not in {"capabilities", "stop"}:
            return {"status": "blocked", "reason": "camera_handed_to_luma"}
        if request.action == "capabilities":
            return {"status": "available", **self.robot_motion_capabilities()}
        perform = getattr(self.robot, "perform_motion", None)
        if perform is None:
            return {"status": "unavailable"}
        if request.action != "stop":
            if (
                not self.guard.is_engaged()
                or self.state_machine.state != AppState.ENGAGED
                or self.realtime is not None
                and self.realtime._input_suspended
            ):
                return {"status": "blocked", "reason": "start_voice_test_or_finish_local_check_first"}
        result = await perform(request)
        if result.get("status") == "started":
            self.conversation.gated_activity()
        return result

    def telemetry_snapshot(self) -> list[dict[str, Any]]:
        return self.telemetry.snapshot()

    def camera_diagnostics(self) -> dict[str, Any]:
        if self.luma_express.active:
            return {"available": False, "reason": "camera_handed_to_luma"}
        try:
            frame = self.camera.get_frame()
            if frame is None:
                return {"available": False}
            detections = self.detector.detect(frame)
            return {
                "available": True,
                "shape": list(frame.shape),
                "faces": [{"bbox": list(face.bbox), "confidence": face.confidence} for face in detections],
            }
        finally:
            if (
                self.reachy_media is not None
                and not self.attention.enabled
                and not self.event_checkin.scanning
            ):
                self.reachy_media.release_idle_camera()

    async def simulate_attempt(self) -> None:
        if not self.settings.reachy_simulation or isinstance(self.robot, ReachyRobotAdapter):
            raise RuntimeError("Simulation attempts are unavailable on real hardware; use local consent")
        await self.state_machine.handle_wake_word()

    def _build_microphone(self):
        if self.settings.simulation_use_microphone:
            try:
                return SoundDeviceMicrophoneAdapter(self.settings)
            except Exception:
                logger.debug("Falling back to null microphone", exc_info=True)
        return NullMicrophoneAdapter(self.settings)

    def _build_robot(self):
        if self.settings.reachy_enabled and not self.settings.reachy_simulation:
            return ReachyRobotAdapter(
                settings=self.settings,
                microphone=self.microphone,
                telemetry=self.telemetry,
                media=self.reachy_media,
            )
        return SimulatedRobotAdapter(
            settings=self.settings, microphone=self.microphone, telemetry=self.telemetry
        )

    def _build_camera(self):
        if self.settings.reachy_enabled and not self.settings.reachy_simulation:
            return ReachyCameraAdapter(
                settings=self.settings,
                telemetry=self.telemetry,
                media=self.reachy_media,
            )
        return SimulationCameraAdapter(settings=self.settings, telemetry=self.telemetry)

    def _validate_production_settings(self) -> None:
        if self.settings.host not in {"127.0.0.1", "localhost", "::1"}:
            raise RuntimeError("Concierge may only bind to loopback")
        if self.settings.access_mode not in {"shadow", "enforce"}:
            raise RuntimeError("access_mode must be shadow or enforce")
        if self.settings.identification_mode not in {"request_scoped", "claim_then_verify"}:
            raise RuntimeError("Passive face identification is forbidden")
        if self.settings.reachy_enabled and not self.settings.reachy_simulation:
            if not self.settings.data_encryption_key and not self.settings.kek_endpoint:
                raise RuntimeError("A real-robot run requires DATA_ENCRYPTION_KEY")
            if self.settings.access_mode == "enforce":
                raise RuntimeError("Enforce mode requires a validated PAD sentinel; unavailable")
        if self.settings.physical_unlock_enabled or self.settings.door_stage != 0:
            raise RuntimeError("Physical door composition is gated by owner approval and hardware evidence")
        if (
            self.settings.dev_endpoints_enabled
            and self.settings.app_env.value != "test"
            and self.settings.host not in {"127.0.0.1", "localhost", "::1"}
        ):
            raise RuntimeError("Developer/admin endpoints may only bind to loopback")
        if not self.settings.is_production:
            return

        errors: list[str] = []
        if self.settings.physical_unlock_enabled:
            errors.append(
                "physical unlock is unreleased until PAD, door confirmation, and owner approval exist"
            )
        if not self.settings.reachy_enabled or self.settings.reachy_simulation:
            errors.append("production requires a real Reachy adapter")
        if self.settings.tts_engine not in {"live", "realtime", "fixed_clips"}:
            errors.append("production speech requires GPT-Live, Realtime or pinned fixed clips")
        if not self.settings.realtime_enabled:
            errors.append("production requires wake-scoped OpenAI voice")
        if self.settings.openai_api_key is None:
            errors.append("production OpenAI credential is missing")
        if not self.settings.realtime_safety_identifier_key:
            errors.append("production requires a keyed voice safety identifier")
        if self.settings.data_encryption_key is None and not self.settings.kek_endpoint:
            errors.append("production sensitive-data encryption key is missing")
        approved = "gpt-live-1" if self.settings.voice_api == "gpt_live" else "gpt-realtime-2.1"
        if self.settings.voice_model != approved:
            errors.append("production must use the approved model for the selected voice API")
        if not self.settings.mlai_public_knowledge_token:
            errors.append("production requires an organization-scoped public Roo service principal")
        if self.settings.wakeword_engine != "sherpa_onnx":
            errors.append("production requires the approved local sherpa-onnx wake engine")
        required_local_models = (
            Path(self.settings.vad_model_path),
            Path(self.settings.vosk_model_path),
            Path(self.settings.sherpa_kws_model_dir),
            Path(self.settings.sherpa_kws_keywords_path),
        )
        if not all(path.exists() for path in required_local_models):
            errors.append("one or more pinned local audio models are missing")
        if not Path(self.settings.face_detector_model_path).is_file():
            errors.append("face detector model is missing")
        if not Path(self.settings.face_embedder_model_path).is_file():
            errors.append("face embedder model is missing")
        if not self.settings.mlai_backend_url.startswith("https://"):
            errors.append("production MLAI backend must use HTTPS")
        if not self.settings.mlai_backend_api_key or self.settings.mlai_backend_api_key == "local-dev-key":
            errors.append("production MLAI credential is missing or unsafe")
        if errors:
            raise RuntimeError("Unsafe production configuration: " + "; ".join(errors))

    def _validate_production_composition(self) -> None:
        if not self.settings.is_production:
            return

        errors: list[str] = []
        if not isinstance(self.microphone, ReachyMediaAdapter):
            errors.append("non-Reachy microphone is forbidden")
        if not isinstance(self.robot, ReachyRobotAdapter):
            errors.append("simulated/non-Reachy robot is forbidden")
        if not isinstance(self.camera, ReachyCameraAdapter):
            errors.append("simulated/non-Reachy camera is forbidden")
        if isinstance(self.identification_service, FakeIdentificationService):
            errors.append("fake identity service is forbidden")
        if not isinstance(self.door_action, PlaceholderDoorAction):
            errors.append("only the non-actuating placeholder door action is currently permitted")
        if errors:
            raise RuntimeError("Unsafe production composition: " + "; ".join(errors))

    def _face_present(self) -> bool:
        if isinstance(self.identification_service, FakeIdentificationService):
            return self.identification_service.has_recent_face()
        observed = self.attention.latest()
        return observed is not None and observed.in_zone and time.monotonic() - observed.timestamp <= 2

    async def _probe_external_health(self) -> None:
        booking_ready = await self._safe_healthcheck(self.booking_client)
        self.system_health.set_status(
            "booking",
            HealthStatus.HEALTHY if booking_ready else HealthStatus.ERROR,
        )
        knowledge_ready = await self.public_knowledge.healthcheck()
        self.system_health.set_status(
            "public_knowledge",
            HealthStatus.HEALTHY if knowledge_ready else HealthStatus.ERROR,
        )

    async def _health_monitor_loop(self) -> None:
        interval = min(
            max(1.0, self.settings.health_probe_interval_seconds),
            max(1.0, self.settings.health_freshness_seconds / 2),
        )
        while True:
            await asyncio.sleep(interval)
            try:
                await self._probe_external_health()
                await self._measure_runtime_health()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Component health refresh failed")

    async def _measure_runtime_health(self) -> None:
        from sqlalchemy import text

        free = shutil.disk_usage(
            self.settings.data_dir if self.settings.data_dir.exists() else Path.cwd()
        ).free
        try:
            with session_scope(self.session_factory) as session:
                session.execute(
                    text("UPDATE system_health SET updated_at=updated_at WHERE component='database'")
                )
            self.system_health.set_status("database", HealthStatus.HEALTHY)
            database_ok = True
        except Exception:
            database_ok = False
            self._disable_access_for_database()
        audio_ok = (
            self.audio_router._running and self.audio_router.enabled and self.audio_router.heartbeat.healthy()
        )
        if self.reachy_media:
            media = self.reachy_media.diagnostics()
            audio_ok = audio_ok and (media.get("last_sample_age_seconds") or 999) < 2
        self.system_health.set_status("audio", HealthStatus.HEALTHY if audio_ok else HealthStatus.ERROR)
        self.system_health.set_status(
            "local_wake", HealthStatus.HEALTHY if self._local_models_ready else HealthStatus.DEGRADED
        )
        robot_ok = not isinstance(self.robot, ReachyRobotAdapter) or self.robot._reachy is not None
        self.system_health.set_status("robot", HealthStatus.HEALTHY if robot_ok else HealthStatus.ERROR)
        self.system_health.set_status(
            "door_action", HealthStatus.HEALTHY, {"mode": "shadow", "actuated": False}
        )
        identity_ok = (
            isinstance(self.identification_service, FakeIdentificationService) or self.embedder.available
        )
        self.system_health.set_status(
            "face_identity", HealthStatus.HEALTHY if identity_ok else HealthStatus.ERROR
        )
        pad_simulated = (
            isinstance(self.identification_service, FakeIdentificationService)
            and self.settings.reachy_simulation
        )
        self.system_health.set_status(
            "face_liveness",
            HealthStatus.HEALTHY if pad_simulated else HealthStatus.ERROR,
            {"mode": "simulated" if pad_simulated else "unavailable"},
        )
        self.system_health.set_status(
            "attention",
            HealthStatus.HEALTHY
            if self.attention.tracker
            and self.attention.heartbeat.healthy()
            and not self.attention.degraded_reason
            else HealthStatus.DEGRADED,
            {"reason": self.attention.degraded_reason},
        )
        if self.daemon_supervisor:
            self.system_health.set_status(
                "daemon",
                HealthStatus.HEALTHY if self.daemon_supervisor.heartbeat.healthy() else HealthStatus.ERROR,
            )
        if (
            isinstance(self.robot, ReachyRobotAdapter)
            and self.robot.motion
            and self.robot.motion.error
            and not getattr(self.robot.power, "parking", False)
        ):
            await self._hardware_fault("motion_fault")
        if self.attention.enabled:
            camera_ok = self.attention.heartbeat.healthy()
            self.system_health.set_status("camera", HealthStatus.HEALTHY if camera_ok else HealthStatus.ERROR)
        if self.doa:
            latest = self.doa.latest()
            self.system_health.set_status(
                "xvf",
                HealthStatus.HEALTHY
                if latest and time.monotonic() - latest.timestamp < 1
                else HealthStatus.DEGRADED,
            )
        if self.realtime:
            self.system_health.set_status(
                "realtime",
                HealthStatus.HEALTHY if self.realtime.running else HealthStatus.DEGRADED,
                {"state": "engaged" if self.realtime.running else "dormant"},
            )
        snapshot = self.system_health.snapshot()
        if self.key_provider:
            await self._refresh_key()
        fault = (
            free < 2_000_000_000
            or not database_ok
            or snapshot.get("booking") != HealthStatus.HEALTHY
            or (self.key_provider is not None and not self.key_provider.available)
        )
        if fault:
            self.access_disabled = True
            self._overlay_recovered_at = None
        elif self.access_disabled and free >= 3_000_000_000:
            self._overlay_recovered_at = self._overlay_recovered_at or time.monotonic()
            if time.monotonic() - self._overlay_recovered_at >= 60:
                self.access_disabled = False
                if self.state_machine.state == AppState.IDLE and not self.guard.is_engaged():
                    if self._idle_task:
                        self._idle_task.cancel()
                    self._idle_task = asyncio.create_task(self._park_after_idle(delay=0))
        self.state_machine.access_disabled = self.access_disabled
        self.telemetry.publish(
            "health", {"access_disabled": self.access_disabled, "disk_free_gib": round(free / 2**30, 2)}
        )
        heartbeat = Path(self.settings.kiosk_heartbeat_path)
        heartbeat.parent.mkdir(parents=True, exist_ok=True)
        temporary = heartbeat.with_suffix(".tmp")
        temporary.write_text(json.dumps({"timestamp": time.time(), "pid": __import__("os").getpid()}))
        temporary.replace(heartbeat)

    async def _cancel_idle_for_wake(self) -> None:
        if self._idle_task:
            if getattr(getattr(self.robot, "power", None), "parking", False):
                await asyncio.shield(self._idle_task)
            else:
                self._idle_task.cancel()

    async def _prepare_engagement(self) -> None:
        if self.luma_express.active:
            raise RuntimeError("Camera handed to Luma")
        await self._cancel_idle_for_wake()
        self._track_ref = str(uuid4())
        wake = getattr(self.robot, "wake", None)
        if wake:
            await wake(self.doa.wake_bearing() if self.doa else 0.0)
        await self.state_machine.transition(AppState.ENGAGED)
        if self.reachy_media:
            frame = await asyncio.to_thread(self.camera.get_frame)
            self.system_health.set_status(
                "camera", HealthStatus.HEALTHY if frame is not None else HealthStatus.ERROR
            )
        self.attention.set_enabled(True)

    async def _play_consent_notice(self) -> None:
        motion = getattr(self.robot, "motion", None)
        if motion:
            motion.stop_expression()
        # Simulation models the duration; hardware uses the pinned local clip.
        if self.reachy_media is None:
            from app.services.conversation.fallback_audio import CONSENT_NOTICE

            await self.robot.speak(CONSENT_NOTICE)
            while self.robot.is_speaking():
                await asyncio.sleep(0.02)
            return
        from app.services.conversation.fallback_audio import FallbackAudio

        await FallbackAudio(playback=self.reachy_media, telemetry=self.telemetry).play("consent_notice")

    async def _play_cloud_fallback(self) -> None:
        if self.reachy_media:
            from app.services.conversation.fallback_audio import FallbackAudio

            await FallbackAudio(playback=self.reachy_media, telemetry=self.telemetry).play(
                "cloud_unavailable"
            )

    async def _end_engagement(self) -> None:
        motion = getattr(self.robot, "motion", None)
        if motion:
            motion.stop_expression()
            from app.services.reachy.motion.manager import MotionMode

            motion.set_mode(MotionMode.IDLE_NEUTRAL)
        self.attention.set_enabled(False)
        self.av_gate.reset()
        if self.reachy_media and not self.event_checkin.scanning:
            self.reachy_media.release_idle_camera()
        if self._routing_task and self._routing_task is not asyncio.current_task():
            self._routing_task.cancel()
        self.visitor_router.forget_session(self.conversation.session_ref)
        if getattr(self, "_stopping", False):
            return
        await self.state_machine.transition(AppState.IDLE)
        if self._idle_task and self._idle_task is not asyncio.current_task():
            self._idle_task.cancel()
        self._idle_task = asyncio.create_task(self._park_after_idle())

    async def _park_after_idle(self, delay=60) -> None:
        await asyncio.sleep(delay)
        if (
            self.state_machine.state == AppState.IDLE
            and not self.guard.is_engaged()
            and not self.access_disabled
            and not self.event_checkin.scanning
            and not self.luma_express.active
        ):
            park = getattr(self.robot, "park", None)
            if park:
                try:
                    await park()
                except Exception:
                    await self._hardware_fault("park_failed")
                    return
            await self.state_machine.transition(AppState.DORMANT)

    async def _hardware_fault(self, reason: str) -> None:
        async with self._hardware_fault_lock:
            if self.state_machine.state == AppState.SAFE_HOLD:
                return
            await self._enter_safe_hold(reason)

    async def _enter_safe_hold(self, reason: str) -> None:
        if getattr(self, "_stopping", False):
            return
        self.telemetry.publish("hardware_fault", {"reason": reason})
        await self.event_checkin.stop_scanning()
        self.attention.set_enabled(False)
        try:
            await asyncio.wait_for(self.conversation.stop(), timeout=3)
        except Exception:
            logger.error("Conversation shutdown during hardware fault failed")
        if self.reachy_media:
            self.reachy_media.release_idle_camera()
        park = getattr(self.robot, "park", None)
        if park and reason != "robot_app_lock_busy":
            try:
                await asyncio.wait_for(park(), 15)
            except Exception:
                logger.error("Fault parking failed")
        await self.state_machine.transition(AppState.SAFE_HOLD)
        if self._safe_hold_task is None or self._safe_hold_task.done():
            self._safe_hold_task = asyncio.create_task(self._recover_safe_hold())

    async def _refresh_key(self) -> None:
        if self.key_provider is None:
            return
        key = await self.key_provider.fetch()
        cipher = SensitiveDataCipher(key)
        self.sensitive_cipher = cipher
        self.face_store.cipher = cipher
        self.system_health.set_status("key", HealthStatus.HEALTHY if key else HealthStatus.ERROR)
        if not key:
            self.access_disabled = True
            self.state_machine.access_disabled = True

    async def _recover_safe_hold(self) -> None:
        from app.services.system.tcc import permission_status

        while self.state_machine.state == AppState.SAFE_HOLD:
            await asyncio.sleep(60)
            permissions = await asyncio.to_thread(permission_status)
            if any(permissions.get(name) in {"denied", "restricted"} for name in ("camera", "microphone")):
                continue
            if self.reachy_media:
                try:
                    await self.reachy_media.restart_streams()
                    lock = await asyncio.to_thread(self.robot.daemon.app_lock)
                    if lock.get("state") != "free":
                        continue
                    if not self.audio_router.heartbeat.healthy():
                        continue
                    await asyncio.wait_for(self.robot.park(), timeout=15)
                    motors = await asyncio.to_thread(self.robot.daemon.motors)
                    if motors.get("mode") != "disabled" or not self.robot.power.parked:
                        continue
                except Exception:
                    continue
            await self.state_machine.boot()

    def _disable_access_for_database(self) -> None:
        self.access_disabled = True
        self._overlay_recovered_at = None
        self.state_machine.access_disabled = True

    def _routing_context(self) -> RoutingContext:
        return RoutingContext(self.settings.device_id, self.conversation.session_ref, self._track_ref)

    def _record_route(self, **row):
        from sqlalchemy import text

        with session_scope(self.session_factory) as session:
            found = session.execute(
                text("SELECT status FROM visitor_routes WHERE id=:id"), {"id": row["id"]}
            ).scalar()
            if found and row["status"] == "RESERVED":
                return False
            if found:
                session.execute(
                    text("UPDATE visitor_routes SET status=:status, latency_ms=:latency_ms WHERE id=:id"), row
                )
            else:
                session.execute(
                    text(
                        "INSERT INTO visitor_routes (id,session_ref,intent,skill_tool,status,latency_ms) "
                        "VALUES (:id,:session_ref,:intent,:skill_tool,:status,:latency_ms)"
                    ),
                    row,
                )
        return True

    async def _route_visitor(self, arguments, call_id):
        if not self.settings.routing_enabled:
            return {"status": "unavailable", "say_hint": "Please see the front desk."}
        request = RouteRequest.model_validate(arguments)
        await self.conversation.mark_routed()
        result = await self.visitor_router.route(request, self._routing_context(), call_id)
        if result["status"] == "notified":
            await self.state_machine.transition(AppState.AWAITING_HOST)
            if self._routing_task:
                self._routing_task.cancel()
            self._routing_task = asyncio.create_task(
                self.visitor_router.poll(
                    self.conversation.session_ref,
                    self._on_route_status,
                    on_poll=self.conversation.gated_activity,
                )
            )
        return result

    async def _notify_staff(self, arguments, call_id):
        await self.conversation.mark_routed()
        return await self.visitor_router.notify_staff(
            StaffReason(arguments["reason"]), self._routing_context(), call_id
        )

    async def _visitor_status(self, arguments, call_id):
        return await self.visitor_router.status(self.conversation.session_ref)

    async def _on_route_status(self, result):
        if self.realtime and self.realtime.running:
            await self.realtime.inject_system_context("ROUTING_" + result["status"].upper())
            await self.realtime.announce(result["say_hint"])

    @staticmethod
    async def _safe_healthcheck(component: Any) -> bool:
        healthcheck = getattr(component, "healthcheck", None)
        if healthcheck is None:
            return False
        try:
            result = healthcheck()
            if inspect.isawaitable(result):
                result = await result
            return result is True
        except Exception:
            return False
