"""Add explicit biometric consent and access revocation controls."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_member_access_safety"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("biometric_consent", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "users",
        sa.Column("access_revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("users", "access_revoked")
    op.drop_column("users", "biometric_consent")
