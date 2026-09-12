from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, create_engine, event, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import settings


def now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    original_name: Mapped[str] = mapped_column(String(512))
    path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    media_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    @property
    def media(self) -> dict: return json.loads(self.media_json or "{}")


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    source_id: Mapped[str] = mapped_column(String(36), index=True)
    status: Mapped[str] = mapped_column(String(40), default="QUEUED", index=True)
    stage: Mapped[str] = mapped_column(String(40), default="queued")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    mode_json: Mapped[str] = mapped_column(Text, default="{}")
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    # ``Source`` is content-addressed and therefore retains the first upload's
    # name.  These job-level names preserve the name selected for every upload
    # and are used for output stems and API presentation.
    original_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Immutable process/model/resource settings captured at queue admission.
    settings_snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    warning_json: Mapped[str] = mapped_column(Text, default="[]")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    parent_job_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # Upload finalization uses this immutable identity to make a crash/retry
    # idempotent: at most one queue job can be created for an upload.
    upload_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True, unique=True)
    lease_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retry_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stage_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    worker_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    pause_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    pause_acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def mode(self) -> dict: return json.loads(self.mode_json or "{}")
    @property
    def config(self) -> dict: return json.loads(self.config_json or "{}")
    @property
    def settings_snapshot(self) -> dict: return json.loads(self.settings_snapshot_json or "{}")
    @property
    def warnings(self) -> list: return json.loads(self.warning_json or "[]")


class StageRun(Base):
    __tablename__ = "stage_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(36), index=True)
    stage: Mapped[str] = mapped_column(String(40))
    unit_key: Mapped[str] = mapped_column(String(200), default="all")
    status: Mapped[str] = mapped_column(String(30), default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    artifact_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Segment(Base):
    __tablename__ = "segments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(36), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    start_ms: Mapped[int] = mapped_column(Integer)
    end_ms: Mapped[int] = mapped_column(Integer)
    chunk_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_text: Mapped[str] = mapped_column(Text, default="")
    translated_text: Mapped[str] = mapped_column(Text, default="")
    reading_text: Mapped[str] = mapped_column(Text, default="")
    tts_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    tts_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tempo: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    warning_json: Mapped[str] = mapped_column(Text, default="[]")
    revision: Mapped[int] = mapped_column(Integer, default=1)

    @property
    def warnings(self) -> list: return json.loads(self.warning_json or "[]")


class Artifact(Base):
    __tablename__ = "artifacts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    job_id: Mapped[str] = mapped_column(String(36), index=True)
    kind: Mapped[str] = mapped_column(String(50))
    path: Mapped[str] = mapped_column(Text)
    # Absolute root trusted for this artifact at publication time.  It remains
    # valid if the global output_root setting changes later.
    trusted_root: Mapped[str | None] = mapped_column(Text, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    valid: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class WatchFolder(Base):
    __tablename__ = "watch_folders"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    path: Mapped[str] = mapped_column(Text, unique=True)
    preset_json: Mapped[str] = mapped_column(Text, default="{}")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Preset(Base):
    __tablename__ = "presets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)

    @property
    def config(self) -> dict:
        return json.loads(self.config_json or "{}")


class WatchSeen(Base):
    __tablename__ = "watch_seen"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    folder_id: Mapped[str] = mapped_column(String(36), index=True)
    path: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(Integer)
    mtime_ns: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    preset_hash: Mapped[str] = mapped_column(String(64), default="")
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    value_json: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(50))
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Upload(Base):
    __tablename__ = "uploads"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    filename: Mapped[str] = mapped_column(String(512))
    temp_path: Mapped[str] = mapped_column(Text)
    total_bytes: Mapped[int] = mapped_column(Integer)
    chunk_size: Mapped[int] = mapped_column(Integer, default=8 * 1024 * 1024)
    received_json: Mapped[str] = mapped_column(Text, default="[]")
    # Immutable finalization intent. This prevents a retry after a crash from
    # attaching the already-created upload job to a different preset.
    preset_json: Mapped[str] = mapped_column(Text, default="{}")
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="UPLOADING")
    completed_source_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    completed_job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    finalizing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finalizing_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    @property
    def received(self) -> list: return json.loads(self.received_json or "[]")


engine = create_engine(settings.database_url, future=True, connect_args={"check_same_thread": False, "timeout": 30})

@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, _connection_record):
    cur = dbapi_connection.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=FULL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=30000")
    cur.close()

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, future=True)


def init_db() -> None:
    settings.ensure_dirs()
    Base.metadata.create_all(engine)
    _migrate_legacy_schema()
    with SessionLocal() as session:
        persisted = {row.key: json.loads(row.value_json) for row in session.scalars(select(Setting))}
    for key, value in persisted.items():
        if key in {"output_root", "max_file_bytes", "max_duration_seconds", "stable_seconds", "cpu_threads"}:
            if key == "output_root": setattr(settings, key, Path(str(value)).resolve())
            else: setattr(settings, key, int(value))
    # Backfill immutable job snapshots after persisted global settings have
    # been applied.  This gives legacy rows the settings that were in force at
    # the first upgrade and keeps later edits from changing those rows.
    with SessionLocal() as session:
        snapshot = json_text(settings.snapshot())
        for job in session.scalars(select(Job)):
            if not job.settings_snapshot_json or job.settings_snapshot_json == "{}":
                job.settings_snapshot_json = snapshot
        for artifact in session.scalars(select(Artifact)):
            if not artifact.trusted_root and artifact.path:
                artifact.trusted_root = str(Path(artifact.path).resolve().parent)
        session.commit()
    settings.output_root.mkdir(parents=True, exist_ok=True)


def _migrate_legacy_schema() -> None:
    """Add columns introduced after the first local release.

    SQLite's ``create_all`` deliberately does not alter an existing table.  The
    desktop app must therefore upgrade a user's durable queue before any worker
    or API code reads the new lease/control fields.  Each statement is additive
    and safe to run on every startup; no user media is touched.
    """
    additions: dict[str, dict[str, str]] = {
        "jobs": {
            "retry_after": "DATETIME",
            "stage_deadline_at": "DATETIME",
            "worker_pid": "INTEGER",
            "worker_started_at": "DATETIME",
            "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
            "pause_requested": "INTEGER NOT NULL DEFAULT 0",
            "pause_acknowledged": "INTEGER NOT NULL DEFAULT 0",
            "upload_id": "VARCHAR(36)",
            "original_name": "VARCHAR(512)",
            "display_name": "VARCHAR(512)",
            "settings_snapshot_json": "TEXT NOT NULL DEFAULT '{}'",
        },
        "artifacts": {
            "trusted_root": "TEXT",
        },
        "uploads": {
            "completed_source_id": "VARCHAR(36)",
            "completed_job_id": "VARCHAR(36)",
            "finalizing_at": "DATETIME",
            "finalizing_token": "VARCHAR(64)",
            "preset_json": "TEXT NOT NULL DEFAULT '{}'",
        },
        "segments": {
            "chunk_index": "INTEGER",
        },
    }
    with engine.begin() as connection:
        for table, columns in additions.items():
            present = {str(row[1]) for row in connection.execute(text(f"PRAGMA table_info({table})"))}
            for name, definition in columns.items():
                if name not in present:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}"))
        # ``ALTER TABLE ADD COLUMN`` cannot express a partial unique
        # constraint.  Nullable upload ids are deliberately unique only when
        # present, so old databases can be upgraded without touching existing
        # manually-created jobs.
        connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_upload_id ON jobs(upload_id) WHERE upload_id IS NOT NULL"))
        # Existing sources keep their first content-addressed filename.  Fill
        # the new job names from that legacy value once, preserving any names
        # already supplied by a newer client.
        connection.execute(text("UPDATE jobs SET original_name = (SELECT original_name FROM sources WHERE sources.id = jobs.source_id) WHERE original_name IS NULL"))
        connection.execute(text("UPDATE jobs SET display_name = COALESCE(original_name, (SELECT original_name FROM sources WHERE sources.id = jobs.source_id)) WHERE display_name IS NULL"))


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
