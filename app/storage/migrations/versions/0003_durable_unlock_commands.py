"""Persist one idempotent unlock command per access attempt."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_durable_unlock_commands"
down_revision = "0002_member_access_safety"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "unlock_commands",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "access_event_id",
            sa.String(length=36),
            sa.ForeignKey("access_events.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("provider_request_id", sa.String(length=255), nullable=True),
        sa.Column("detail", sa.String(length=255), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("access_event_id", name="uq_unlock_command_access_event"),
    )
    op.create_index("ix_unlock_commands_access_event_id", "unlock_commands", ["access_event_id"], unique=True)
    op.create_index("ix_unlock_commands_status", "unlock_commands", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_unlock_commands_status", table_name="unlock_commands")
    op.drop_index("ix_unlock_commands_access_event_id", table_name="unlock_commands")
    op.drop_table("unlock_commands")
