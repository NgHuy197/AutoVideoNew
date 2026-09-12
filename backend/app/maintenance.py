"""Safe queue housekeeping, retention, backups, and AC-only idle inhibition."""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from .config import settings
from .db import Artifact, Job, Segment, SessionLocal, StageRun, Upload, now


TERMINAL_STATUSES = {"COMPLETED", "COMPLETED_WITH_WARNINGS", "FAILED", "CANCELLED",
                     "SKIPPED_NO_AUDIO", "SKIPPED_NO_SPEECH"}
PRESERVED_INTERMEDIATE_SUFFIXES = {".json", ".srt", ".ass", ".txt"}


def _utc_epoch(value: datetime | None) -> float:
    if value is None:
        return 0.0
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def _protected_paths(session) -> set[str]:
    paths: set[str] = set()
    for row in session.scalars(select(Segment).where(Segment.tts_path.is_not(None))):
        try:
            paths.add(str(Path(row.tts_path).resolve()).lower())
        except (OSError, ValueError):
            continue
    for row in session.scalars(select(Artifact)):
        try:
            paths.add(str(Path(row.path).resolve()).lower())
        except (OSError, ValueError):
            continue
    # Stage checkpoints can be the only durable reference to an intermediate
    # file while a job is being resumed. Keep them live even when no Segment or
    # Artifact row points at the path yet.
    for row in session.scalars(select(StageRun).where(StageRun.artifact_path.is_not(None))):
        try:
            paths.add(str(Path(row.artifact_path).resolve()).lower())
        except (OSError, ValueError):
            continue
    return paths


def _is_protected(path: Path, protected: set[str]) -> bool:
    try:
        return str(path.resolve()).lower() in protected
    except (OSError, ValueError):
        return True


def _prune_logs(root: Path, *, cutoff: float, max_bytes: int = 200 * 1024 * 1024) -> dict[str, int]:
    removed = 0
    candidates: list[tuple[float, int, Path]] = []
    for path in root.rglob("*") if root.is_dir() else ():
        if path.is_symlink() or not path.is_file() or path.name.endswith(".partial"):
            continue
        try:
            stat = path.stat()
            if stat.st_mtime < cutoff:
                path.unlink()
                removed += 1
            else:
                candidates.append((stat.st_mtime, stat.st_size, path))
        except OSError:
            continue
    current = sum(size for _, size, _ in candidates)
    for _, size, path in sorted(candidates):
        if current <= max_bytes:
            break
        try:
            path.unlink()
            current -= size
            removed += 1
        except OSError:
            continue
    return {"removed": removed, "bytes": current}


def _prune_intermediates(session, *, now_epoch: float) -> dict[str, int]:
    protected = _protected_paths(session)
    jobs = {job.id: job for job in session.scalars(select(Job))}
    removed = 0
    for job_dir in settings.jobs_root.iterdir() if settings.jobs_root.is_dir() else ():
        if not job_dir.is_dir() or job_dir.is_symlink():
            continue
        job = jobs.get(job_dir.name)
        if not job or job.status not in TERMINAL_STATUSES:
            continue
        age_days = 7 if job.status in {"COMPLETED", "COMPLETED_WITH_WARNINGS"} else 14
        finished = _utc_epoch(job.completed_at) or _utc_epoch(job.updated_at) or _utc_epoch(job.created_at)
        if finished <= 0 or now_epoch - finished < age_days * 86400:
            continue
        for path in sorted(job_dir.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            if not path.is_file() or path.is_symlink() or _is_protected(path, protected):
                continue
            # Transcript/report files are durable user data. Intermediate WAVs,
            # concat manifests, and failed partials are safe to reclaim.
            if path.suffix.lower() in PRESERVED_INTERMEDIATE_SUFFIXES:
                continue
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
        for directory in sorted((item for item in job_dir.rglob("*") if item.is_dir()),
                                key=lambda item: len(item.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass
    return {"removed": removed}


def _prune_cache(*, now_epoch: float, protected: set[str] | None = None) -> dict[str, int]:
    removed = 0
    current = 0
    cutoff = now_epoch - 7 * 86400
    protected = protected or set()
    for path in settings.cache_root.rglob("*") if settings.cache_root.is_dir() else ():
        if not path.is_file() or path.is_symlink():
            continue
        try:
            stat = path.stat()
            if _is_protected(path, protected):
                current += stat.st_size
                continue
            if stat.st_mtime < cutoff:
                path.unlink()
                removed += 1
            else:
                current += stat.st_size
        except OSError:
            continue
    return {"removed": removed, "bytes": current}


def _prune_backups(*, now_epoch: float) -> int:
    cutoff = now_epoch - settings.backup_retention_days * 86400
    removed = 0
    for path in settings.backups_root.glob("videoauto-*.sqlite3") if settings.backups_root.is_dir() else ():
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def create_database_backup() -> Path | None:
    """Create a consistent SQLite backup and publish it atomically."""

    database = settings.database_root / "videoauto.sqlite3"
    if not database.is_file():
        return None
    settings.backups_root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    target = settings.backups_root / f"videoauto-{stamp}.sqlite3"
    if target.exists():
        target = settings.backups_root / f"videoauto-{stamp}-{time.time_ns()}.sqlite3"
    partial = target.with_suffix(target.suffix + ".partial")
    source = destination = None
    try:
        source = sqlite3.connect(str(database), timeout=30)
        destination = sqlite3.connect(str(partial), timeout=30)
        source.backup(destination)
        destination.commit()
        destination.close(); destination = None
        source.close(); source = None
        os.replace(partial, target)
        return target
    except (OSError, sqlite3.Error):
        return None
    finally:
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()
        partial.unlink(missing_ok=True)


def reconcile_completing_uploads() -> dict[str, int]:
    """Retry durable upload finalization using the persisted preset.

    The API route owns the same upload-id lock used by browsers. If a live
    finalizer currently owns that lock, this function uses a non-blocking
    acquisition probe and leaves the row for the next pass.
    """

    from .api import complete_upload
    from .upload_lock import upload_lock

    attempted = completed = contended = failed = 0
    with SessionLocal() as session:
        rows = list(session.scalars(select(Upload).where(Upload.status == "COMPLETING")))
    for upload in rows:
        lock_path = settings.inbox_root / f"{upload.id}.lock"
        try:
            # upload_lock is blocking on Windows by design, so this lightweight
            # probe uses the platform-specific nonblocking mode where possible.
            if os.name == "nt":
                import msvcrt
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                with lock_path.open("a+b") as handle:
                    handle.seek(0); handle.write(b"\0"); handle.flush(); handle.seek(0)
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    except OSError:
                        contended += 1
                        continue
                    finally:
                        try:
                            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                        except OSError:
                            pass
            attempted += 1
            preset = json.loads(upload.preset_json or "{}")
            result = complete_upload(upload.id, {"preset": preset})
            if result:
                completed += 1
        except Exception as exc:
            failed += 1
            with SessionLocal() as session:
                current = session.get(Upload, upload.id)
                if current and current.status == "COMPLETING":
                    # Leave a durable, actionable state for diagnostics and a
                    # later retry; do not silently delete the uploaded bytes.
                    current.status = "UPLOADING"
                    current.finalizing_at = None
                    current.finalizing_token = None
                    session.commit()
    return {"attempted": attempted, "completed": completed, "contended": contended, "failed": failed}


def run_housekeeping() -> dict[str, object]:
    """Run bounded cleanup and report counts for diagnostics."""

    current = time.time()
    result: dict[str, object] = {}
    result["backup"] = str(create_database_backup() or "")
    result["backups_removed"] = _prune_backups(now_epoch=current)
    protected: set[str]
    with SessionLocal() as session:
        result["intermediates"] = _prune_intermediates(session, now_epoch=current)
        protected = _protected_paths(session)
        cutoff = current - settings.partial_upload_retention_hours * 3600
        uploads_removed = 0
        for upload in session.scalars(select(Upload).where(Upload.status == "UPLOADING")):
            if _utc_epoch(upload.created_at) >= cutoff:
                continue
            try:
                Path(upload.temp_path).unlink(missing_ok=True)
                session.delete(upload)
                uploads_removed += 1
            except OSError:
                continue
        session.commit()
        result["uploads_removed"] = uploads_removed
    result["cache"] = _prune_cache(now_epoch=current, protected=protected)
    result["logs"] = _prune_logs(settings.logs_root, cutoff=current - 30 * 86400)
    return result


class AcIdleInhibitor:
    """Prevent system idle sleep on AC only when explicitly enabled."""

    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001

    def __init__(self) -> None:
        self.enabled = os.environ.get("VIDEOAUTO_24_7_ENABLED", "0").lower() in {"1", "true", "yes", "on"}
        self.active = False

    def refresh(self) -> bool:
        if not self.enabled or os.name != "nt":
            self.clear()
            return False
        try:
            status = ctypes.c_ubyte()
            # SYSTEM_POWER_STATUS.ac_line_status is the first byte.
            class _PowerStatus(ctypes.Structure):
                _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                            ("BatteryLifePercent", ctypes.c_ubyte), ("Reserved", ctypes.c_ubyte),
                            ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]
            power = _PowerStatus()
            if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(power)) or power.ACLineStatus != 1:
                self.clear()
                return False
            result = ctypes.windll.kernel32.SetThreadExecutionState(self.ES_CONTINUOUS | self.ES_SYSTEM_REQUIRED)
            self.active = bool(result)
            return self.active
        except (AttributeError, OSError):
            self.clear()
            return False

    def clear(self) -> None:
        if os.name == "nt" and self.active:
            try:
                ctypes.windll.kernel32.SetThreadExecutionState(self.ES_CONTINUOUS)
            except (AttributeError, OSError):
                pass
        self.active = False
