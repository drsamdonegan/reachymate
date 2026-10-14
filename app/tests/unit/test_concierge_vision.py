from __future__ import annotations

import base64
import io

import numpy as np
import pytest
from PIL import Image

from app.services.vision.embedding_guard import AccessAttemptScope, EmbeddingGuard
from app.services.vision.enrollment import decode_image
from app.services.vision.face_detect import DetectedFace
from app.services.vision.face_embed import DummyFaceEmbedder
from app.services.vision.face_match import FaceMatcher, aggregate_identity_frames
from app.storage.db import create_session_factory, init_db, session_scope
from app.storage.encryption import SensitiveDataCipher
from app.storage.repositories import SqlFaceStore, UserRepository
from app.storage.tables import BiometricConsentRecord, FaceEmbeddingRecord


def test_embedding_requires_a_local_authorized_scope_and_scope_expires():
    embedder = DummyFaceEmbedder()
    frame = np.zeros((10, 10, 3), dtype=np.uint8)
    face = DetectedFace((0, 0, 10, 10), 0.99)
    with pytest.raises(RuntimeError, match="scope"):
        embedder.embed(frame, face)
    with AccessAttemptScope(attempt_id="enroll-test", purpose="enrollment"):
        assert len(embedder.embed(frame, face)) == 128
    assert EmbeddingGuard.active() is False


def test_identity_uses_top_three_mean_and_requires_consensus_and_margin():
    scores = FaceMatcher().score_identities(
        [1.0, 0.0], {"a": [[1.0, 0.0], [0.0, 1.0], [0.0, 1.0]], "b": [[0.8, 0.6]]}
    )
    assert scores["a"] == pytest.approx(1 / 3)
    assert scores["b"] == pytest.approx(0.8)
    stable = aggregate_identity_frames([({"a": 0.7, "b": 0.5}, 10)] * 3)
    assert stable.status == "MATCH" and stable.user_id == "a"
    assert aggregate_identity_frames([({"a": 0.7, "b": 0.69}, 10)] * 3).status == "UNCERTAIN"
    assert aggregate_identity_frames([({"a": 0.2, "b": 0.1}, 10)] * 3).status == "UNKNOWN"
    assert (
        aggregate_identity_frames([({"a": 0.7}, 10), ({"b": 0.8}, 10), ({"a": 0.7}, 10)]).status
        == "UNCERTAIN"
    )


def test_enrollment_decode_is_bgr_and_rejects_oversized_data():
    out = io.BytesIO()
    Image.new("RGB", (2, 2), (255, 0, 0)).save(out, "PNG")
    encoded = base64.b64encode(out.getvalue()).decode()
    image = decode_image(encoded)
    assert image[0, 0].tolist() == [0, 0, 255]
    with pytest.raises(ValueError, match="size"):
        decode_image(encoded, max_bytes=1)


def test_atomic_enrollment_preserves_revocation_and_cipher_binds_row(settings):
    factory = create_session_factory(settings)
    init_db(factory)
    cipher = SensitiveDataCipher(base64.urlsafe_b64encode(b"x" * 32).decode())
    with session_scope(factory) as session:
        user = UserRepository(session).upsert(
            full_name="Member",
            slack_user_id="U1",
            slack_display_name="m",
            biometric_consent=True,
            access_revoked=True,
        )
        store = SqlFaceStore(session, cipher=cipher, allow_plaintext=False)
        with pytest.raises(ValueError, match="revoked"):
            store.enroll_atomic(
                full_name="Member",
                slack_user_id="U1",
                slack_display_name="m",
                embeddings=[([1.0, 0.0], 90.0, "front")],
                operator_id="operator",
                notice_version="v1",
                notice_sha256="a" * 64,
            )
        assert user.access_revoked
        UserRepository(session).reinstate(user.id)
        store.enroll_atomic(
            full_name="Member",
            slack_user_id="U1",
            slack_display_name="m",
            embeddings=[([1.0, 0.0], 90.0, "front"), ([0.0, 1.0], 90.0, "side")],
            operator_id="operator",
            notice_version="v1",
            notice_sha256="a" * 64,
        )
        assert session.query(BiometricConsentRecord).count() == 1
        rows = session.query(FaceEmbeddingRecord).all()
        rows[1].embedding_vector = rows[0].embedding_vector
        with pytest.raises(RuntimeError, match="decrypt"):
            store.list_embeddings()


def test_rewrap_and_withdrawal_are_immediate(settings):
    from app.scripts.migrate_sensitive_data import migrate_rows
    from app.storage.repositories import face_embedding_purpose

    factory = create_session_factory(settings)
    init_db(factory)
    cipher = SensitiveDataCipher(base64.urlsafe_b64encode(b"w" * 32).decode())
    with session_scope(factory) as session:
        user = UserRepository(session).upsert(
            full_name="Member", slack_user_id="U2", slack_display_name="m", biometric_consent=True
        )
        row = FaceEmbeddingRecord(
            id="legacy-row",
            user_id=user.id,
            embedding_vector=cipher.encrypt_json([1.0, 0.0], purpose="face_embedding"),
            quality_score=90.0,
            model_id="sface-2021dec",
            capture_source="legacy",
        )
        session.add(row)
        session.add(
            BiometricConsentRecord(
                user_id=user.id,
                notice_version="v1",
                notice_sha256="a" * 64,
                method="member_checkbox",
                operator_id="operator",
            )
        )
        session.flush()
        counts = migrate_rows(session, cipher, rewrap=True, apply=True)
        assert counts["legacy_aad_templates"] == 1
        assert cipher.decrypt_json(
            row.embedding_vector, purpose=face_embedding_purpose(user.id, row.id), allow_plaintext=False
        ) == [1.0, 0.0]
        store = SqlFaceStore(session, cipher=cipher, allow_plaintext=False)
        assert user.id in store.list_embeddings()
        UserRepository(session).revoke(user.id)
        assert store.list_embeddings() == {}
        assert session.query(FaceEmbeddingRecord).count() == 0


def test_enrollment_failure_does_not_write_or_clear_revocation(settings):
    from app.services.vision.enrollment import EnrollmentService

    factory = create_session_factory(settings)
    init_db(factory)
    cipher = SensitiveDataCipher(base64.urlsafe_b64encode(b"e" * 32).decode())

    class Detector:
        def detect(self, frame):
            return [DetectedFace((20, 20, 160, 160), 0.99)]

    class Embedder:
        model_id = "fixture"
        model_sha256 = "a" * 64

        def embed(self, frame, face):
            assert EmbeddingGuard.active()
            return [1.0, 0.0]

    image = np.random.default_rng(123).integers(70, 190, (200, 200, 3), dtype=np.uint8)
    out = io.BytesIO()
    Image.fromarray(image).save(out, "PNG")
    sample = {"image_b64": base64.b64encode(out.getvalue()).decode()}
    with session_scope(factory) as session:
        repo = UserRepository(session)
        user = repo.upsert(
            full_name="Old",
            slack_user_id="U3",
            slack_display_name="old",
            biometric_consent=True,
            access_revoked=True,
        )
        service = EnrollmentService(
            settings=settings,
            detector=Detector(),
            embedder=Embedder(),
            user_repo=repo,
            face_store=SqlFaceStore(session, cipher=cipher, allow_plaintext=False),
        )
        with pytest.raises(ValueError):
            service.enroll(
                full_name="New",
                slack_user_id="U3",
                slack_display_name="new",
                company=None,
                biometric_consent=True,
                samples=[sample, {"image_b64": "bad"}],
                operator_id="operator",
                notice_version="v1",
                notice_sha256="a" * 64,
                reinstate=True,
            )
        assert user.access_revoked and user.full_name == "Old"
        assert session.query(FaceEmbeddingRecord).count() == 0
        assert session.query(BiometricConsentRecord).count() == 0


@pytest.mark.asyncio
async def test_identification_consensus_zeroizes_owned_probe_buffers(settings):
    from app.services.vision.face_match import IdentificationService

    class Camera:
        frames = []

        def get_frame(self):
            frame = np.random.default_rng(9).integers(70, 190, (200, 200, 3), dtype=np.uint8)
            self.frames.append(frame)
            return frame

    class Detector:
        def detect(self, frame):
            return [DetectedFace((20, 20, 160, 160), 0.99)]

    class Embedder:
        vectors = []

        def embed(self, frame, face):
            assert EmbeddingGuard.active()
            vector = [1.0, 0.0]
            self.vectors.append(vector)
            return vector

    class Store:
        def list_embeddings(self, *, model_id=None):
            return {"a": [[1.0, 0.0]], "b": [[0.0, 1.0]]}

    camera, embedder = Camera(), Embedder()
    identifier = IdentificationService(
        settings=settings,
        camera=camera,
        detector=Detector(),
        embedder=embedder,
        matcher=FaceMatcher(),
        face_store=Store(),
    )
    with pytest.raises(RuntimeError, match="scope"):
        await identifier.identify()
    with AccessAttemptScope("attempt"):
        result = await identifier.identify()
    assert result.match_status == "MATCH" and result.matched_user_id == "a"
    assert result.embedding is None and result.stable_frames == 3
    assert all(not np.any(frame) for frame in camera.frames)
    assert all(vector == [0.0, 0.0] for vector in embedder.vectors)


def test_calibration_uses_all_pairs_and_exposes_evidence_limits():
    from app.scripts.calibrate_face_threshold import calibrate_gallery

    report = calibrate_gallery(
        {"a": [[1.0, 0.0], [0.99, 0.1], [0.9, 0.1]], "b": [[0.0, 1.0], [0.1, 0.99], [0.1, 0.9]]}
    )
    assert report["genuine_template_pairs"] == 6
    assert report["impostor_template_pairs"] == 9
    assert report["genuine_probes"] == 6
    assert report["shipping_ready"] is False
    assert report["required_impostor_comparisons_if_zero_errors"] == 450000
    assert report["zero_error_request_upper_95"] == 0.5


def test_shadow_bypasses_only_pad_and_never_authorizes(known_face_result):
    from app.models.domain import BookingResult, SystemStatusSnapshot
    from app.models.enums import BookingStatus, Decision, HealthStatus, ReasonCode
    from app.services.policy.decision_engine import DecisionEngine
    from app.services.system.health import ACCESS_REQUIRED_COMPONENTS

    components = {name: HealthStatus.HEALTHY for name in ACCESS_REQUIRED_COMPONENTS}
    components["face_liveness"] = HealthStatus.ERROR
    engine = DecisionEngine(face_match_threshold=0.48, access_mode="shadow")
    booking = BookingResult(BookingStatus.ACTIVE, True)
    decision = engine.evaluate(
        face_match_result=known_face_result,
        booking_result=booking,
        system_status=SystemStatusSnapshot(False, components),
    )
    assert not decision.allowed and decision.decision == Decision.DENY
    assert decision.reason_code == ReasonCode.SHADOW_WOULD_ALLOW
    for component in ACCESS_REQUIRED_COMPONENTS:
        if component == "face_liveness":
            continue
        broken = {**components, component: HealthStatus.ERROR}
        blocked = engine.evaluate(
            face_match_result=known_face_result,
            booking_result=booking,
            system_status=SystemStatusSnapshot(True, broken),
        )
        assert blocked.reason_code == ReasonCode.ERROR_ACCESS_NOT_READY


def test_signed_nonbiometric_claim_is_authenticated_expiring_and_single_use():
    import hashlib
    import hmac
    import json

    from app.services.vision.claims import IdentityClaimVerifier

    key = b"q" * 32
    payload = {
        "user_id": "member",
        "source": "signed_qr",
        "purpose": "reachy_access",
        "nonce": "a" * 24,
        "exp": 130.0,
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    signature = (
        base64.urlsafe_b64encode(hmac.new(key, encoded.encode(), hashlib.sha256).digest())
        .decode()
        .rstrip("=")
    )
    token = encoded + "." + signature
    verifier = IdentityClaimVerifier(key, clock=lambda: 100.0)
    assert verifier.verify(token).user_id == "member"
    with pytest.raises(ValueError, match="already"):
        verifier.verify(token)
    with pytest.raises(ValueError, match="signature"):
        IdentityClaimVerifier(b"r" * 32, clock=lambda: 100.0).verify(token)
    with pytest.raises(ValueError, match="expired"):
        IdentityClaimVerifier(key, clock=lambda: 140.0).verify(token)


def test_pose_uses_measured_k_and_distortion():
    from types import SimpleNamespace

    import cv2

    from app.services.vision.pose import FacePoseEstimator

    calibration = SimpleNamespace(
        matrix=np.array([[600.0, 0.0, 320.0], [0.0, 600.0, 240.0], [0.0, 0.0, 1.0]]),
        distortion=np.zeros(5),
        width=640,
        height=480,
    )
    estimator = FacePoseEstimator(calibration)
    rotation = np.array([[0.0], [np.radians(20.0)], [0.0]])
    points, _ = cv2.projectPoints(
        estimator.geometry,
        rotation,
        np.array([[0.0], [0.0], [400.0]]),
        calibration.matrix,
        calibration.distortion,
    )
    face = DetectedFace((200, 100, 200, 200), 0.99, points.reshape(5, 2).tolist())
    pose = estimator.estimate(face, (480, 640, 3))
    assert pose is not None
    assert pose.yaw_degrees == pytest.approx(20.0, abs=0.1)
    assert abs(pose.pitch_degrees) < 0.1


@pytest.mark.asyncio
async def test_cancellation_erases_probe_after_cpu_worker_finishes(settings):
    import asyncio
    import threading

    from app.services.vision.face_match import IdentificationService

    started, release = threading.Event(), threading.Event()
    frames, vectors = [], []

    class Camera:
        def get_frame(self):
            frame = np.random.default_rng(9).integers(70, 190, (200, 200, 3), dtype=np.uint8)
            frames.append(frame)
            return frame

    class Detector:
        def detect(self, frame):
            return [DetectedFace((20, 20, 160, 160), 0.99)]

    class Embedder:
        def embed(self, frame, face):
            started.set()
            release.wait(timeout=1)
            vector = [1.0, 0.0]
            vectors.append(vector)
            return vector

    class Store:
        def list_embeddings(self, *, model_id=None):
            return {"a": [[1.0, 0.0]]}

    identifier = IdentificationService(
        settings=settings,
        camera=Camera(),
        detector=Detector(),
        embedder=Embedder(),
        matcher=FaceMatcher(),
        face_store=Store(),
    )
    with AccessAttemptScope("cancel-test"):
        task = asyncio.create_task(identifier.identify())
        assert await asyncio.to_thread(started.wait, 1.0)
        task.cancel()
        await asyncio.sleep(0.02)
        assert np.any(frames[0])  # an active worker still owns its readable image
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert all(not np.any(frame) for frame in frames)
    assert vectors == [[0.0, 0.0]]


def test_shared_yunet_serializes_input_size_and_inference():
    import time
    from concurrent.futures import ThreadPoolExecutor

    from app.services.vision.face_detect import YuNetFaceDetector

    class MutableNetwork:
        busy = False

        def setInputSize(self, size):
            assert not self.busy
            self.busy = True
            time.sleep(0.005)

        def detect(self, frame):
            assert self.busy
            self.busy = False
            return None, None

    detector = YuNetFaceDetector()
    detector._detector = MutableNetwork()
    frames = [np.zeros((100 + index, 200 + index, 3), dtype=np.uint8) for index in range(8)]
    with ThreadPoolExecutor(max_workers=4) as workers:
        assert list(workers.map(detector.detect, frames)) == [[]] * 8


def test_enrollment_savepoint_obeys_outer_rollback(settings):
    factory = create_session_factory(settings)
    init_db(factory)
    cipher = SensitiveDataCipher(base64.urlsafe_b64encode(b"r" * 32).decode())
    session = factory()
    try:
        SqlFaceStore(session, cipher=cipher, allow_plaintext=False).enroll_atomic(
            full_name="Rollback",
            slack_user_id="UROLLBACK",
            slack_display_name="r",
            embeddings=[([1.0, 0.0], 90.0, "front")],
            operator_id="operator",
            notice_version="v1",
            notice_sha256="a" * 64,
        )
        session.rollback()
    finally:
        session.close()
    with session_scope(factory) as another:
        assert UserRepository(another).get_by_slack_id("UROLLBACK") is None
        assert another.query(BiometricConsentRecord).count() == 0
        assert another.query(FaceEmbeddingRecord).count() == 0


@pytest.mark.asyncio
async def test_standalone_remote_key_authorization_is_scoped_and_revocation_fails_closed(
    settings, monkeypatch
):
    import app.vision_stack as vision

    encoded = base64.urlsafe_b64encode(b"t" * 32).decode()
    instances = []

    class Provider:
        result = encoded

        def __init__(self, settings):
            self.closed = False
            instances.append(self)

        async def fetch(self):
            return self.result

        async def close(self):
            self.closed = True

    monkeypatch.setattr(vision, "BackendKeyProvider", Provider)
    original = settings.model_copy(
        update={"kek_endpoint": "https://keys.example.test", "data_encryption_key": None}
    )
    async with vision.authorized_vision_settings(original) as authorized:
        assert authorized.data_encryption_key.get_secret_value() == encoded
        assert original.data_encryption_key is None
    assert authorized.data_encryption_key is None and instances[-1].closed
    Provider.result = None
    with pytest.raises(RuntimeError, match="authorized"):
        async with vision.authorized_vision_settings(original):
            pass
    assert instances[-1].closed
    async with vision.authorized_vision_settings(original, require_key=False) as withdrawn:
        assert withdrawn.data_encryption_key is None


def test_legacy_checkbox_does_not_authorize_gallery_and_foreign_keys_cascade(settings):
    from sqlalchemy import text

    factory = create_session_factory(settings)
    init_db(factory)
    cipher = SensitiveDataCipher(base64.urlsafe_b64encode(b"c" * 32).decode())
    with session_scope(factory) as session:
        user = UserRepository(session).upsert(
            full_name="Old Checkbox",
            slack_user_id="ULEGACY",
            slack_display_name="old",
            biometric_consent=True,
        )
        store = SqlFaceStore(session, cipher=cipher, allow_plaintext=False)
        store.replace_embeddings(user.id, [([1.0, 0.0], 90.0, "front")])
        assert store.list_embeddings() == {}
        session.add(
            BiometricConsentRecord(
                user_id=user.id,
                notice_version="v1",
                notice_sha256="a" * 64,
                method="member_checkbox",
                operator_id="operator",
            )
        )
        session.flush()
        assert user.id in store.list_embeddings()
        user_id = user.id
    engine = factory.kw["bind"]
    engine.dispose()  # force the per-connection FK hook to run again
    with engine.begin() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        connection.execute(text("DELETE FROM users WHERE id=:id"), {"id": user_id})
    with session_scope(factory) as session:
        assert session.query(FaceEmbeddingRecord).count() == 0
        assert session.query(BiometricConsentRecord).count() == 0
