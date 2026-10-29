"""Upgrade legacy watch markers for preset-aware deduplication."""
from alembic import op
import sqlalchemy as sa

revision = "0003_watch_preset_hash"
down_revision = "0002_product_features"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("watch_seen")}
    if "preset_hash" not in columns:
        op.add_column("watch_seen", sa.Column("preset_hash", sa.String(64), nullable=False, server_default=""))


def downgrade() -> None:
    # Preserve watch history, consistent with desktop additive migrations.
    pass
