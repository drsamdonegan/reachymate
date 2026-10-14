from __future__ import annotations

import base64

from app.storage.db import create_session_factory, init_db, session_scope
from app.storage.encryption import PREFIX, SensitiveDataCipher
from app.storage.repositories import SaltoTokenRepository, SqlFaceStore, UserRepository
from app.storage.tables import BiometricConsentRecord, FaceEmbeddingRecord, SaltoTokenRecord


def test_face_templates_are_encrypted_and_ineligible_users_are_filtered(settings) -> None:
    cipher = SensitiveDataCipher(base64.urlsafe_b64encode(b"k" * 32).decode("ascii"))
    factory = create_session_factory(settings)
    init_db(factory)
    with session_scope(factory) as session:
        users = UserRepository(session)
        eligible = users.upsert(
            user_id="eligible",
            full_name="Eligible",
            slack_user_id="U1",
            slack_display_name="eligible",
            biometric_consent=True,
        )
        revoked = users.upsert(
            user_id="revoked",
            full_name="Revoked",
            slack_user_id="U2",
            slack_display_name="revoked",
            biometric_consent=True,
            access_revoked=True,
        )
        store = SqlFaceStore(session, cipher=cipher, allow_plaintext=False)
        session.add(
            BiometricConsentRecord(
                user_id=eligible.id,
                notice_version="v1",
                notice_sha256="a" * 64,
                method="member_checkbox",
                operator_id="operator",
            )
        )
        store.replace_embeddings(eligible.id, [([0.1, 0.2], 1.0, "front")])
        store.replace_embeddings(revoked.id, [([0.3, 0.4], 1.0, "front")])

    with session_scope(factory) as session:
        raw = session.query(FaceEmbeddingRecord).all()
        assert all(isinstance(row.embedding_vector, str) for row in raw)
        assert all(row.embedding_vector.startswith(PREFIX) for row in raw)
        visible = SqlFaceStore(session, cipher=cipher, allow_plaintext=False).list_embeddings()

    assert visible == {"eligible": [[0.1, 0.2]]}


def test_salto_tokens_are_encrypted_at_rest(settings) -> None:
    cipher = SensitiveDataCipher(base64.urlsafe_b64encode(b"s" * 32).decode("ascii"))
    factory = create_session_factory(settings)
    init_db(factory)
    with session_scope(factory) as session:
        SaltoTokenRepository(session, cipher=cipher, allow_plaintext=False).save(
            access_token="access-secret",
            refresh_token="refresh-secret",
            expires_at=None,
        )

    with session_scope(factory) as session:
        raw = session.query(SaltoTokenRecord).one()
        assert raw.access_token.startswith(PREFIX)
        assert "access-secret" not in raw.access_token

    with session_scope(factory) as session:
        token = SaltoTokenRepository(session, cipher=cipher, allow_plaintext=False).get()
        assert token is not None
        assert token.access_token == "access-secret"
        assert token.refresh_token == "refresh-secret"
