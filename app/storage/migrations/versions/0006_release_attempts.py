"""Durable staged door release attempts, including uncertain outcomes."""

import sqlalchemy as sa
from alembic import op

revision = "0006_release_attempts"
down_revision = "0005_consent_template_model"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "release_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("access_event_id", sa.String(36), sa.ForeignKey("access_events.id"), nullable=False),
        sa.Column("device_id", sa.String(128), nullable=False),
        sa.Column("site_id", sa.String(128), nullable=False),
        sa.Column("lock_id", sa.String(128), nullable=False),
        sa.Column("stage", sa.String(2), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("detail", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("send_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_request_id", sa.String(128), nullable=True),
        sa.Column("evidence_ref", sa.String(128), nullable=True),
        sa.UniqueConstraint("access_event_id", name="uq_release_attempt_event"),
        sa.CheckConstraint(
            "status IN ('PENDING','SENDING','ACTUATED_CONFIRMED','ACCEPTED_UNCONFIRMED',"
            "'REJECTED','FAILED','UNCERTAIN')",
            name="ck_release_status",
        ),
    )
    op.create_index("ix_release_attempt_lock_status", "release_attempts", ["lock_id", "status"])
    op.create_index("ix_release_attempt_created", "release_attempts", ["created_at"])


def downgrade():
    op.drop_table("release_attempts")
