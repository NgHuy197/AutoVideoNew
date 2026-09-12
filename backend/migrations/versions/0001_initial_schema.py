"""Create the durable local queue schema.

The application also runs a small additive SQLite compatibility migration at
startup because older desktop installs may predate this revision.
"""
from alembic import op

from backend.app.db import Base

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    # A local queue downgrade must never delete user media or job history.
    pass
