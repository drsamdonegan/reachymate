"""Name-free durable visitor routing reservations."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003_durable_unlock_commands"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "visitor_routes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_ref", sa.String(36), nullable=False),
        sa.Column("intent", sa.String(32), nullable=False),
        sa.Column("skill_tool", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_visitor_routes_created_at", "visitor_routes", ["created_at"])


def downgrade():
    op.drop_table("visitor_routes")
