"""Add per-day usage counters for the external translation API.

Desktop startup creates the same table through ``Base.metadata.create_all``.
"""
from alembic import op
import sqlalchemy as sa

revision = "0004_api_usage"
down_revision = "0003_watch_preset_hash"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "api_usage" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "api_usage",
            sa.Column("day", sa.String(10), primary_key=True),
            sa.Column("calls", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("errors", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("rate_limited", sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    op.drop_table("api_usage")
