"""Add presets, immutable job metadata and artifact trust roots.

The desktop startup compatibility migration in ``backend.app.db`` applies the
same additive changes for installations that do not run Alembic on startup.
"""
from alembic import op
import sqlalchemy as sa


revision = "0002_product_features"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    known_columns = {table: {item["name"] for item in inspector.get_columns(table)}
                     for table in ("jobs", "artifacts")}

    def add_column(table: str, column: sa.Column) -> None:
        if column.name not in known_columns[table]:
            op.add_column(table, column)
            known_columns[table].add(column.name)

    add_column("jobs", sa.Column("original_name", sa.String(512), nullable=True))
    add_column("jobs", sa.Column("display_name", sa.String(512), nullable=True))
    add_column("jobs", sa.Column("settings_snapshot_json", sa.Text(), nullable=False, server_default="{}"))
    add_column("artifacts", sa.Column("trusted_root", sa.Text(), nullable=True))

    if "presets" not in inspector.get_table_names():
        op.create_table(
            "presets",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("name", sa.String(160), nullable=False),
            sa.Column("config_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("description", sa.String(1000), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("name", name="uq_presets_name"),
        )
    if "ix_presets_name" not in {item["name"] for item in inspector.get_indexes("presets")}:
        op.create_index("ix_presets_name", "presets", ["name"], unique=False)

    # Preserve legacy user-visible names and make old artifact rows retrievable
    # without trusting the current global output_root.
    op.execute(
        "UPDATE jobs SET original_name = (SELECT original_name FROM sources "
        "WHERE sources.id = jobs.source_id) WHERE original_name IS NULL"
    )
    op.execute(
        "UPDATE jobs SET display_name = COALESCE(original_name, "
        "(SELECT original_name FROM sources WHERE sources.id = jobs.source_id)) "
        "WHERE display_name IS NULL"
    )
    # The parent directory is a conservative fallback for artifacts produced
    # before trusted_root existed.  Do this in Python because SQLite has no
    # portable reverse-string function and Windows paths use backslashes.
    artifact_table = sa.table("artifacts", sa.column("id", sa.String),
                              sa.column("path", sa.Text), sa.column("trusted_root", sa.Text))
    rows = bind.execute(sa.select(artifact_table.c.id, artifact_table.c.path)
                        .where(artifact_table.c.trusted_root.is_(None)))
    for artifact_id, path in rows:
        raw = str(path or "")
        normalized = raw.replace("\\", "/")
        root = normalized.rsplit("/", 1)[0] if "/" in normalized else normalized
        bind.execute(artifact_table.update().where(artifact_table.c.id == artifact_id)
                     .values(trusted_root=root))


def downgrade() -> None:
    # Queue history and media are user data.  Keep this migration additive and
    # avoid destructive downgrade operations in the desktop application.
    pass
