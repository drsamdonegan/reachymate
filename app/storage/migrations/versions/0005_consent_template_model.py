"""Recorded consent and template/model provenance; SHADOW uses existing status column."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_consent_template_model"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("greet_by_name", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.add_column(
        "face_embeddings",
        sa.Column("model_id", sa.String(128), nullable=False, server_default="sface-2021dec"),
    )
    op.add_column("face_embeddings", sa.Column("model_sha256", sa.String(64), nullable=True))
    op.add_column(
        "face_embeddings", sa.Column("capture_source", sa.String(64), nullable=False, server_default="legacy")
    )
    op.create_table(
        "biometric_consents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("notice_version", sa.String(64), nullable=False),
        sa.Column("notice_sha256", sa.String(64), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("method", sa.String(64), nullable=False),
        sa.Column("operator_id", sa.String(128), nullable=False),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_biometric_consents_user_id", "biometric_consents", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_biometric_consents_user_id", table_name="biometric_consents")
    op.drop_table("biometric_consents")
    with op.batch_alter_table("face_embeddings") as batch:
        batch.drop_column("capture_source")
        batch.drop_column("model_sha256")
        batch.drop_column("model_id")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("greet_by_name")
