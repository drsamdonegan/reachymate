"""Initial office bouncer schema."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("slack_user_id", sa.String(length=64), nullable=False),
        sa.Column("slack_display_name", sa.String(length=255), nullable=False),
        sa.Column("company", sa.String(length=255), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True, server_default=sa.func.now()),
    )
    op.create_index("ix_users_slack_user_id", "users", ["slack_user_id"], unique=True)

    op.create_table(
        "face_embeddings",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("embedding_vector", sa.JSON(), nullable=False),
        sa.Column("quality_score", sa.Float(), nullable=False),
        sa.Column("sample_label", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True, server_default=sa.func.now()),
    )
    op.create_index("ix_face_embeddings_user_id", "face_embeddings", ["user_id"], unique=False)

    op.create_table(
        "access_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detected_user_id", sa.String(length=36), nullable=True),
        sa.Column("matched_confidence", sa.Float(), nullable=True),
        sa.Column("booking_status", sa.String(length=64), nullable=False),
        sa.Column("face_quality", sa.JSON(), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("salto_result", sa.String(length=64), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
    )
    op.create_index("ix_access_events_timestamp", "access_events", ["timestamp"], unique=False)
    op.create_index("ix_access_events_reason_code", "access_events", ["reason_code"], unique=False)
    op.create_index("ix_access_events_detected_user_id", "access_events", ["detected_user_id"], unique=False)

    op.create_table(
        "system_health",
        sa.Column("component", sa.String(length=64), primary_key=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("details_json", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_system_health_status", "system_health", ["status"], unique=False)

    op.create_table(
        "salto_tokens",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("refresh_token", sa.Text(), nullable=True),
        sa.Column("token_type", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider", name="uq_salto_provider"),
    )


def downgrade() -> None:
    op.drop_table("salto_tokens")
    op.drop_index("ix_system_health_status", table_name="system_health")
    op.drop_table("system_health")
    op.drop_index("ix_access_events_detected_user_id", table_name="access_events")
    op.drop_index("ix_access_events_reason_code", table_name="access_events")
    op.drop_index("ix_access_events_timestamp", table_name="access_events")
    op.drop_table("access_events")
    op.drop_index("ix_face_embeddings_user_id", table_name="face_embeddings")
    op.drop_table("face_embeddings")
    op.drop_index("ix_users_slack_user_id", table_name="users")
    op.drop_table("users")
