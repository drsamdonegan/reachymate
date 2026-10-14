"""A standalone vision/database stack; no robot, microphone or cloud startup."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.services.system.key_provider import BackendKeyProvider
from app.services.system.telemetry import TelemetryBroker
from app.services.vision.camera import CameraAdapter, PinnedLocalCameraAdapter
from app.services.vision.enrollment import EnrollmentService
from app.services.vision.face_detect import YuNetFaceDetector
from app.services.vision.face_embed import SFaceEmbedder
from app.services.vision.face_match import FaceMatcher, IdentificationService
from app.services.vision.pose import FacePoseEstimator
from app.storage.db import create_session_factory, init_db
from app.storage.encryption import SensitiveDataCipher
from app.storage.repositories import SqlFaceStore, UserRepository


@dataclass
class VisionStack:
    settings: Settings
    session: Any
    face_store: SqlFaceStore
    user_repo: UserRepository
    detector: YuNetFaceDetector
    embedder: SFaceEmbedder
    matcher: FaceMatcher
    enrollment: EnrollmentService
    camera: CameraAdapter | None

    async def start(self) -> None:
        # Loading weights is local. Inference is performed by the caller in a worker.
        import asyncio

        await asyncio.to_thread(self.detector.load)
        await asyncio.to_thread(self.embedder.load)
        if self.camera is not None:
            await self.camera.start()

    async def stop(self) -> None:
        try:
            if self.camera is not None:
                await self.camera.stop()
        finally:
            self.close()

    def close(self) -> None:
        self.session.close()
        self.face_store.cipher = SensitiveDataCipher(None)

    def identifier(self) -> IdentificationService:
        if self.camera is None:
            raise RuntimeError("No camera was requested for this vision stack")
        return IdentificationService(
            settings=self.settings,
            camera=self.camera,
            detector=self.detector,
            embedder=self.embedder,
            matcher=self.matcher,
            face_store=self.face_store,
        )


def build_vision_stack(
    settings: Settings,
    *,
    for_enrollment: bool = False,
    from_reachy_camera: bool = False,
    camera: CameraAdapter | None = None,
) -> VisionStack:
    cipher = SensitiveDataCipher(settings.data_encryption_key)
    if for_enrollment and not cipher.enabled:
        raise RuntimeError("DATA_ENCRYPTION_KEY is required before enrollment")
    factory = create_session_factory(settings)
    init_db(factory)
    session = factory()
    store = SqlFaceStore(
        session,
        cipher=cipher,
        allow_plaintext=False,
    )
    users = UserRepository(session)
    detector = YuNetFaceDetector(settings.face_detector_model_path, strict=True)
    embedder = SFaceEmbedder(settings.face_embedder_model_path, allow_fallback=False)
    if camera is None and from_reachy_camera:
        camera = PinnedLocalCameraAdapter(settings=settings, telemetry=TelemetryBroker())
    enrollment = EnrollmentService(
        settings=settings, detector=detector, embedder=embedder, user_repo=users, face_store=store
    )
    if settings.camera_calibration_path:
        from app.services.vision.attention import CameraCalibration

        enrollment.pose_estimator = FacePoseEstimator(
            CameraCalibration.load(settings.camera_calibration_path)
        )
    return VisionStack(settings, session, store, users, detector, embedder, FaceMatcher(), enrollment, camera)


@asynccontextmanager
async def authorized_vision_settings(settings: Settings, *, require_key: bool = True):
    """Fetch-only CLI authorization; no app/robot startup or key writes.

    Withdrawal/deletion can set require_key=False: unavailable encryption keys
    must not stop a member's templates being deleted.
    """
    from pydantic import SecretStr

    scoped_settings = settings.model_copy()
    provider = BackendKeyProvider(settings) if settings.kek_endpoint else None
    try:
        if provider:
            key = await provider.fetch()
            scoped_settings.data_encryption_key = SecretStr(key) if key else None
        if require_key and not SensitiveDataCipher(scoped_settings.data_encryption_key).enabled:
            raise RuntimeError("A currently authorized data encryption key is required")
        yield scoped_settings
    finally:
        scoped_settings.data_encryption_key = None
        if provider:
            await provider.close()
