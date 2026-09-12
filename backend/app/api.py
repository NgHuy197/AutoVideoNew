from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import shutil
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Cookie, Depends, FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AliasChoices, BaseModel, Field
from sqlalchemy import select, update

from .config import settings
from .db import Artifact, Event, Job, Preset, Segment, SessionLocal, Source, StageRun, Upload, WatchFolder, Setting, init_db, json_text, now
from .media import MediaError, sha256_file
from .paths import confined, safe_filename
from .service import supervisor
from .upload_lock import upload_lock


class JobCreate(BaseModel):
    source_id: str
    preset_id: str | None = None
    audio_mode: str = Field("replace", pattern="^(replace|mix)$")
    subtitle_mode: str = Field("burn", pattern="^(burn|soft|srt|none)$")
    source_language: str | None = None
    # ``None`` means the container's default audio disposition.  Many files
    # intentionally mark a non-zero track as default.
    audio_index: int | None = Field(default=None, ge=0)
    include_dubbing: bool = True
    include_original_track: bool = Field(default=False, validation_alias=AliasChoices(
        "include_original_track", "include_original_audio", "include_original_audio_track",
        "original_audio_secondary"))
    priority: int = 0


class SegmentPatch(BaseModel):
    id: int
    translated_text: str = Field(max_length=20000)
    reading_text: str | None = Field(default=None, max_length=20000)
    # Every editor write is conditional.  A missing revision is ambiguous and
    # would let an old browser overwrite a newer draft.
    revision: int = Field(ge=1)


class WatchCreate(BaseModel):
    path: str
    preset: dict[str, Any] = Field(default_factory=dict)


class PresetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    config: dict[str, Any] = Field(default_factory=dict)
    description: str | None = Field(default=None, max_length=1000)


class PresetPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    config: dict[str, Any] | None = None
    description: str | None = Field(default=None, max_length=1000)


class PriorityPatch(BaseModel):
    priority: int


class SettingsPatch(BaseModel):
    data_root: str | None = None
    output_root: str | None = None
    max_file_bytes: int | None = Field(default=None, ge=1)
    max_duration_seconds: int | None = Field(default=None, ge=1)
    stable_seconds: int | None = Field(default=None, ge=1)
    cpu_threads: int | None = Field(default=None, ge=1, le=64)


def _normalize_preset(preset: dict | None) -> dict[str, Any]:
    value = dict(preset or {})
    audio_mode = value.get("audio_mode", "replace")
    subtitle_mode = value.get("subtitle_mode", "burn")
    if audio_mode not in {"replace", "mix"}:
        raise ValueError("audio_mode must be replace or mix")
    if subtitle_mode not in {"burn", "soft", "srt", "none"}:
        raise ValueError("subtitle_mode must be burn, soft, srt, or none")
    include_dubbing = value.get("include_dubbing", True)
    if not isinstance(include_dubbing, bool):
        raise ValueError("include_dubbing must be a boolean")
    audio_index = value.get("audio_index")
    if audio_index is not None:
        try:
            audio_index = int(audio_index)
        except (TypeError, ValueError) as exc:
            raise ValueError("audio_index must be a non-negative integer") from exc
        if audio_index < 0:
            raise ValueError("audio_index must be a non-negative integer")
    source_language = value.get("source_language")
    if source_language is not None and not isinstance(source_language, str):
        raise ValueError("source_language must be a string or null")
    priority = value.get("priority", 0)
    try:
        priority = int(priority)
    except (TypeError, ValueError) as exc:
        raise ValueError("priority must be an integer") from exc
    # Accept the API names used by older clients while persisting one stable
    # key so deduplication and upload retry comparisons remain deterministic.
    original_track_values = [value.get(key) for key in
                             ("include_original_track", "include_original_audio", "include_original_audio_track",
                              "original_audio_secondary") if key in value]
    if any(not isinstance(item, bool) for item in original_track_values):
        raise ValueError("include_original_track must be a boolean")
    include_original_track = any(original_track_values)
    value = {key: item for key, item in value.items() if key not in
             {"include_original_audio", "include_original_audio_track", "original_audio_secondary"}}
    return {**value, "audio_mode": audio_mode, "subtitle_mode": subtitle_mode,
            "include_dubbing": include_dubbing, "audio_index": audio_index,
            "source_language": source_language, "include_original_track": include_original_track,
            "priority": priority}


def create_source_and_job(path: Path, preset: dict | None, watch_folder_id: str | None = None, original_name: str | None = None,
                          expected_sha256: str | None = None, upload_id: str | None = None) -> tuple[Source, Job]:
    path = path.resolve(); size = path.stat().st_size
    if size > settings.max_file_bytes: raise ValueError("file exceeds configured size limit")
    digest = sha256_file(path)
    if expected_sha256 and not secrets.compare_digest(digest, expected_sha256):
        raise ValueError("source changed while it was being stabilized")
    settings.sources_root.mkdir(parents=True, exist_ok=True)
    # The source blob is content addressed, so two independent uploads of the
    # same bytes must serialize promotion of the shared destination.  The
    # upload-id lock cannot protect this path because the ids differ.
    with upload_lock(settings.sources_root / f".{digest}.lock"):
        target = None
        for candidate in sorted(settings.sources_root.glob(f"{digest}.*")):
            if not candidate.is_file() or candidate.name.endswith(".partial") or candidate.name.endswith(".lock"):
                continue
            try:
                if secrets.compare_digest(sha256_file(candidate), digest):
                    target = candidate; break
            except OSError:
                continue
        if target is None:
            target = settings.sources_root / f"{digest}{path.suffix.lower()}"
            partial = settings.sources_root / f".{digest}.{secrets.token_hex(8)}.partial"
            try:
                shutil.copy2(path, partial)
                if sha256_file(partial) != digest: raise ValueError("source changed while being copied")
                os.replace(partial, target)
            except Exception:
                partial.unlink(missing_ok=True)
                raise
        with SessionLocal() as session:
            source = session.scalar(select(Source).where(Source.sha256 == digest))
            if not source:
                source = Source(original_name=original_name or path.name, path=str(target), sha256=digest, size_bytes=size, media_json="{}"); session.add(source); session.flush()
            elif not Path(source.path).is_file():
                source.path = str(target)
            mode = _normalize_preset(preset)
            if upload_id:
                # This lookup is the durable half of upload finalization
                # idempotency. A process can crash after this insert and before
                # marking Upload.COMPLETED; a later completion reuses the job.
                existing_upload_job = session.scalar(select(Job).where(Job.upload_id == upload_id).limit(1))
                if existing_upload_job:
                    return source, existing_upload_job
            if watch_folder_id:
                # Include the immutable preset in deduplication, even after an earlier
                # job completed, so rescans cannot enqueue a duplicate generation.
                same_preset = session.scalar(select(Job).where(Job.source_id == source.id,
                    Job.mode_json == json_text(mode), Job.status != "CANCELLED").order_by(Job.created_at.desc()).limit(1))
                if same_preset:
                    return source, same_preset
            job_name = safe_filename(original_name or source.original_name or path.name)
            job = Job(source_id=source.id, upload_id=upload_id, original_name=job_name, display_name=job_name,
                      mode_json=json_text(mode), config_json=json_text(mode), settings_snapshot_json=json_text(settings.snapshot()),
                      priority=int(mode.get("priority", 0))); session.add(job); session.commit()
            session.refresh(source); session.refresh(job); return source, job


def _csrf(request: Request, csrf_cookie: str | None = Cookie(default=None, alias="videoauto_csrf"), x_csrf_token: str | None = Header(default=None)) -> None:
    if request.method in {"GET", "HEAD", "OPTIONS"}: return
    if not csrf_cookie or not x_csrf_token or not secrets.compare_digest(csrf_cookie, x_csrf_token): raise HTTPException(403, "CSRF token required")


def _job_dict(session, job: Job) -> dict:
    source = session.get(Source, job.source_id)
    def iso(value):
        if not value: return None
        if value.tzinfo is None: value = value.replace(tzinfo=timezone.utc)
        return value.isoformat().replace("+00:00", "Z")
    filename = job.display_name or job.original_name or (source.original_name if source else None)
    return {"id": job.id, "source_id": job.source_id, "filename": filename,
            "original_name": job.original_name or filename, "display_name": filename,
            "status": job.status, "stage": job.stage, "progress": job.progress, "priority": job.priority, "mode": job.mode, "error": job.error,
            "warnings": job.warnings, "revision": job.revision, "parent_job_id": job.parent_job_id,
            "settings_snapshot": job.settings_snapshot, "created_at": iso(job.created_at),
            "completed_at": iso(job.completed_at)}


router = APIRouter(prefix="/api/v1")


@router.get("/health/live")
def live(): return {"status": "ok"}


@router.get("/health/ready")
def ready():
    try:
        init_db(); return {"status": "ready", "paths": settings.manifest()}
    except Exception as exc: raise HTTPException(503, str(exc))


@router.get("/diagnostics")
def diagnostics():
    paths = settings.manifest(); checks = {}
    for key, value in paths.items():
        p = Path(value); checks[key] = {"path": str(p), "exists": p.exists(), "file": p.is_file()}
    return {"checks": checks, "python": os.sys.version, "offline": True}


@router.post("/uploads")
def create_upload(payload: dict, _: None = Depends(_csrf)):
    filename = safe_filename(str(payload.get("filename", "video"))); total = int(payload.get("total_bytes", 0))
    if total <= 0 or total > settings.max_file_bytes: raise HTTPException(413, "invalid upload size")
    try:
        if shutil.disk_usage(settings.data_root).free < settings.min_free_bytes + total:
            raise HTTPException(507, "not enough free disk space for this upload")
    except OSError as exc:
        raise HTTPException(503, f"cannot inspect free disk space: {exc}") from exc
    upload_id = secrets.token_hex(16); path = settings.inbox_root / f"{upload_id}.partial"; path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle: handle.truncate(total)
    with SessionLocal() as session:
        upload = Upload(id=upload_id, filename=filename, temp_path=str(path), total_bytes=total, sha256=payload.get("sha256")); session.add(upload); session.commit()
    return {"id": upload_id, "chunk_size": 8 * 1024 * 1024, "received": []}


@router.get("/uploads/{upload_id}")
def get_upload(upload_id: str):
    with SessionLocal() as session:
        upload = session.get(Upload, upload_id)
        if not upload: raise HTTPException(404, "upload not found")
        return {"id": upload.id, "filename": upload.filename, "total_bytes": upload.total_bytes, "chunk_size": upload.chunk_size, "received": upload.received, "status": upload.status}


@router.put("/uploads/{upload_id}/chunks/{index}")
async def put_chunk(upload_id: str, index: int, request: Request, _: None = Depends(_csrf)):
    with SessionLocal() as session: upload = session.get(Upload, upload_id)
    if not upload: raise HTTPException(404, "upload not found")
    if upload.status != "UPLOADING": raise HTTPException(409, "upload is already completed")
    start = index * upload.chunk_size
    expected_len = min(upload.chunk_size, upload.total_bytes - start) if index >= 0 and start < upload.total_bytes else 0
    if index < 0 or start >= upload.total_bytes or expected_len <= 0:
        raise HTTPException(400, "invalid chunk length")
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > expected_len:
                raise HTTPException(413, "chunk is too large")
            if int(declared) != expected_len:
                raise HTTPException(400, "invalid chunk length")
        except ValueError:
            raise HTTPException(400, "invalid content length")
    # Keep the upload endpoint bounded even when a client uses chunked transfer
    # encoding.  Nothing is written until the complete chunk has been checked.
    data = bytearray()
    async for piece in request.stream():
        if len(data) + len(piece) > expected_len:
            raise HTTPException(413, "chunk is too large")
        data.extend(piece)
    if len(data) != expected_len:
        raise HTTPException(400, "invalid chunk length")
    # Re-read metadata while holding the id-derived OS lock.  This closes the
    # race where a finalizer renames the file between the initial read and the
    # random-access write, and also serializes two workers writing the same
    # chunk index.
    with upload_lock(settings.inbox_root / f"{upload_id}.lock"):
        with SessionLocal() as session:
            current = session.get(Upload, upload_id)
            if not current: raise HTTPException(404, "upload not found")
            if current.status != "UPLOADING": raise HTTPException(409, "upload is already completing")
            current_start = index * current.chunk_size
            current_expected = min(current.chunk_size, current.total_bytes - current_start) if index >= 0 and current_start < current.total_bytes else 0
            if current_start != start or current_expected != expected_len:
                raise HTTPException(409, "upload metadata changed; retry the chunk")
            target = Path(current.temp_path)
            if not target.is_file(): raise HTTPException(409, "upload temporary file is unavailable")
            with target.open("r+b") as handle:
                handle.seek(start); handle.write(data); handle.flush(); os.fsync(handle.fileno())
            received = set(current.received); received.add(index)
            current.received_json = json_text(sorted(received)); session.commit()
    return {"id": upload_id, "index": index, "received": sorted(received)}


@router.post("/uploads/{upload_id}/complete")
def complete_upload(upload_id: str, payload: dict | None = None, _: None = Depends(_csrf)):
    # The lock is derived only from the opaque upload id, never from the user
    # supplied filename.  It serializes chunks, competing completion requests,
    # and the rename into the durable sources area.
    with upload_lock(settings.inbox_root / f"{upload_id}.lock"):
        with SessionLocal() as session:
            upload = session.get(Upload, upload_id)
            if not upload: raise HTTPException(404, "upload not found")
            requested_preset = (payload or {}).get("preset")
            if upload.status == "COMPLETED" and upload.completed_source_id and upload.completed_job_id:
                # Idempotent completion is only safe when a caller that sends
                # a preset repeats the exact accepted intent.  The old early
                # return accepted a changed preset and silently returned the
                # previous job, which is particularly confusing after a
                # browser retry.
                if requested_preset is not None:
                    try:
                        requested_mode = _normalize_preset(requested_preset)
                    except ValueError as exc:
                        raise HTTPException(422, str(exc)) from exc
                    existing_job = session.get(Job, upload.completed_job_id)
                    existing_mode = _normalize_preset(existing_job.mode) if existing_job else None
                    if existing_mode is not None and existing_mode != requested_mode:
                        raise HTTPException(409, "upload was finalized with a different preset")
                return {"source_id": upload.completed_source_id, "job_id": upload.completed_job_id, "idempotent": True}
            try:
                preset = _normalize_preset((payload or {}).get("preset"))
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            # A source/job may already have been committed when the process
            # crashed. Check its immutable mode before accepting a retry.
            existing_upload_job = session.scalar(select(Job).where(Job.upload_id == upload_id).limit(1))
            stored_preset = json.loads(upload.preset_json or "{}")
            if existing_upload_job:
                existing_mode = _normalize_preset(existing_upload_job.mode)
                stored_mode = _normalize_preset(stored_preset) if stored_preset else None
                if stored_mode and stored_mode != existing_mode:
                    raise HTTPException(409, "upload was finalized with a different preset")
                if existing_mode != preset:
                    raise HTTPException(409, "upload was finalized with a different preset")
            elif stored_preset and _normalize_preset(stored_preset) != preset:
                raise HTTPException(409, "upload finalization preset cannot be changed")
            if upload.status == "COMPLETING":
                # Reaching this point means this request acquired the
                # upload-id OS lock. A live finalizer would still hold it, so
                # any COMPLETING row seen here belongs to a process that has
                # exited and can be recovered immediately, even when its
                # timestamp is recent.
                upload.status = "UPLOADING"; upload.finalizing_at = None; upload.finalizing_token = None
                session.commit()
            if upload.status != "UPLOADING": raise HTTPException(409, "upload finalization is already in progress")
            count = (upload.total_bytes + upload.chunk_size - 1) // upload.chunk_size
            if upload.received != list(range(count)): raise HTTPException(409, "upload is incomplete")
            token = secrets.token_hex(32)
            claimed = session.execute(update(Upload).where(Upload.id == upload_id, Upload.status == "UPLOADING").values(
                status="COMPLETING", finalizing_at=now(), finalizing_token=token, preset_json=json_text(preset)))
            if claimed.rowcount != 1:
                session.rollback(); raise HTTPException(409, "upload finalization is already in progress")
            session.commit()
            filename = upload.filename
            current_path = Path(upload.temp_path)
            original_temp_path = str(upload.temp_path)
            expected = (payload or {}).get("sha256") or upload.sha256

        source: Source | None = None
        job: Job | None = None
        try:
            if not current_path.is_file():
                # A crash can occur after the media rename and before the
                # Upload.temp_path metadata commit. Recover the deterministic
                # destination from the opaque upload id.
                renamed_candidate = current_path.with_name(upload_id + (Path(filename).suffix.lower() or ".mp4"))
                if renamed_candidate.is_file(): current_path = renamed_candidate
                else: raise ValueError("upload temporary file is unavailable")
            digest = sha256_file(current_path)
            if expected and not secrets.compare_digest(digest, expected): raise ValueError("checksum mismatch")
            # Give the completed temporary file its original media extension
            # before source preflight.  A retry after a crash may already have
            # performed this rename, so it must be safe to run again.
            suffix = Path(filename).suffix.lower() or ".mp4"
            source_input = current_path if current_path.suffix.lower() == suffix and not current_path.name.endswith(".partial") else current_path.with_name(upload_id + suffix)
            if current_path != source_input:
                if source_input.exists():
                    if sha256_file(source_input) != digest: raise ValueError("upload destination already exists with different content")
                    current_path.unlink(missing_ok=True)
                else:
                    current_path.replace(source_input)
            if str(source_input) != original_temp_path:
                with SessionLocal() as session:
                    result = session.execute(update(Upload).where(Upload.id == upload_id, Upload.status == "COMPLETING", Upload.finalizing_token == token).values(temp_path=str(source_input)))
                    if result.rowcount != 1: raise HTTPException(409, "upload finalization lost ownership")
                    session.commit()
            source, job = create_source_and_job(source_input, preset, original_name=filename, upload_id=upload_id)
        except Exception as exc:
            with SessionLocal() as session:
                session.execute(update(Upload).where(Upload.id == upload_id, Upload.status == "COMPLETING", Upload.finalizing_token == token).values(
                    status="UPLOADING", finalizing_at=None, finalizing_token=None))
                session.commit()
            if isinstance(exc, HTTPException): raise
            raise HTTPException(422, str(exc)) from exc
        if source is None or job is None: raise HTTPException(500, "upload finalization did not create a job")
        with SessionLocal() as session:
            result = session.execute(update(Upload).where(Upload.id == upload_id, Upload.status == "COMPLETING", Upload.finalizing_token == token).values(
                status="COMPLETED", completed_source_id=source.id, completed_job_id=job.id,
                finalizing_at=None, finalizing_token=None))
            if result.rowcount != 1:
                session.rollback(); raise HTTPException(409, "upload finalization lost ownership")
            session.commit()
        return {"source_id": source.id, "job_id": job.id}


@router.post("/jobs")
def create_job(payload: JobCreate, _: None = Depends(_csrf)):
    with SessionLocal() as session:
        source = session.get(Source, payload.source_id)
        if not source: raise HTTPException(404, "source not found")
        values = payload.model_dump()
        preset_id = values.pop("preset_id", None)
        if preset_id:
            preset = session.get(Preset, preset_id)
            if not preset: raise HTTPException(404, "preset not found")
            # Explicit job fields win over preset values.  This endpoint keeps
            # backwards-compatible defaults, so clients that want a preset's
            # values should send the preset's fields too (the upload endpoint
            # accepts a raw preset document without this ambiguity).
            values = {**preset.config, **values}
        try:
            mode = _normalize_preset(values)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        name = safe_filename(source.original_name or "video")
        job = Job(source_id=source.id, original_name=name, display_name=name,
                  mode_json=json_text(mode), config_json=json_text(mode),
                  settings_snapshot_json=json_text(settings.snapshot()), priority=int(mode.get("priority", 0)))
        session.add(job); session.commit(); session.refresh(job); return _job_dict(session, job)


@router.get("/jobs")
def list_jobs(status: str | None = None, limit: int = Query(100, ge=1, le=500)):
    with SessionLocal() as session:
        query = select(Job).order_by(Job.created_at.desc()).limit(limit)
        if status: query = select(Job).where(Job.status == status).order_by(Job.created_at.desc()).limit(limit)
        return [_job_dict(session, j) for j in session.scalars(query)]


@router.get("/jobs/{job_id}")
def get_job(job_id: str):
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if not job: raise HTTPException(404, "job not found")
        return _job_dict(session, job)


def _preset_dict(preset: Preset) -> dict[str, Any]:
    def iso(value):
        if not value: return None
        if value.tzinfo is None: value = value.replace(tzinfo=timezone.utc)
        return value.isoformat().replace("+00:00", "Z")
    return {"id": preset.id, "name": preset.name, "config": preset.config,
            "description": preset.description, "created_at": iso(preset.created_at),
            "updated_at": iso(preset.updated_at)}


@router.get("/presets")
def list_presets():
    with SessionLocal() as session:
        return [_preset_dict(item) for item in session.scalars(select(Preset).order_by(Preset.name))]


@router.post("/presets")
def create_preset(payload: PresetCreate, _: None = Depends(_csrf)):
    name = payload.name.strip()
    if not name: raise HTTPException(422, "preset name cannot be empty")
    try:
        config = _normalize_preset(payload.config)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    with SessionLocal() as session:
        item = Preset(name=name, config_json=json_text(config), description=payload.description)
        session.add(item)
        try:
            session.commit()
        except Exception as exc:
            session.rollback()
            # Avoid exposing a database-specific error while retaining a
            # deterministic contract for clients creating the same name.
            if session.scalar(select(Preset.id).where(Preset.name == name)):
                raise HTTPException(409, "preset name already exists") from exc
            raise
        session.refresh(item)
        return _preset_dict(item)


@router.get("/presets/{preset_id}")
def get_preset(preset_id: str):
    with SessionLocal() as session:
        item = session.get(Preset, preset_id)
        if not item: raise HTTPException(404, "preset not found")
        return _preset_dict(item)


@router.patch("/presets/{preset_id}")
def patch_preset(preset_id: str, payload: PresetPatch, _: None = Depends(_csrf)):
    values = payload.model_dump(exclude_unset=True)
    with SessionLocal() as session:
        item = session.get(Preset, preset_id)
        if not item: raise HTTPException(404, "preset not found")
        if "name" in values:
            name = str(values["name"]).strip()
            if not name: raise HTTPException(422, "preset name cannot be empty")
            item.name = name
        if "config" in values:
            try:
                item.config_json = json_text(_normalize_preset(values["config"]))
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
        if "description" in values: item.description = values["description"]
        try:
            session.commit()
        except Exception as exc:
            session.rollback()
            if session.scalar(select(Preset.id).where(Preset.name == item.name, Preset.id != preset_id)):
                raise HTTPException(409, "preset name already exists") from exc
            raise
        session.refresh(item)
        return _preset_dict(item)


@router.delete("/presets/{preset_id}")
def delete_preset(preset_id: str, _: None = Depends(_csrf)):
    with SessionLocal() as session:
        item = session.get(Preset, preset_id)
        if not item: raise HTTPException(404, "preset not found")
        session.delete(item); session.commit()
        return {"deleted": True, "id": preset_id}


@router.patch("/jobs/{job_id}/priority")
@router.post("/jobs/{job_id}/priority")
def set_job_priority(job_id: str, payload: PriorityPatch, _: None = Depends(_csrf)):
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if not job: raise HTTPException(404, "job not found")
        if job.status not in {"QUEUED", "RETRY_WAIT", "WAITING_RESOURCES", "PAUSED"}:
            raise HTTPException(409, f"cannot reprioritize job in {job.status}")
        job.priority = int(payload.priority)
        session.commit()
        return _job_dict(session, job)


def _mutate_job(job_id: str, action: str):
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if not job: raise HTTPException(404, "job not found")
        if action == "pause" and job.status == "QUEUED": job.status = "PAUSED"; job.pause_acknowledged = True
        elif action == "pause" and job.status == "RUNNING": job.status = "PAUSING"; job.pause_requested = True
        elif action == "resume" and job.status in {"PAUSED", "FAILED"}: job.status = "QUEUED"; job.error = None; job.pause_requested = False; job.pause_acknowledged = False; job.lease_id = None
        elif action == "resume" and job.status == "PAUSING": raise HTTPException(409, "worker is still acknowledging pause")
        elif action == "retry" and job.status == "FAILED": job.status = "QUEUED"; job.error = None
        elif action == "cancel" and job.status in {"QUEUED", "PAUSED", "DRAFT"}:
            job.status = "CANCELLED"; job.cancel_requested = True; job.pause_requested = False; job.lease_id = None; job.lease_at = None; job.worker_pid = None; job.worker_started_at = None
        elif action == "cancel" and job.status in {"RUNNING", "PAUSING", "CANCELLING"}:
            # The API can be a different process from the dispatcher. Persist the
            # request and let the owning supervisor kill the exact worker tree.
            job.status = "CANCELLING"; job.cancel_requested = True; job.pause_requested = False
        else: raise HTTPException(409, f"cannot {action} job in {job.status}")
        session.commit(); return _job_dict(session, job)


for _action in ("pause", "resume", "cancel", "retry"):
    router.add_api_route(f"/jobs/{{job_id}}/{_action}", lambda job_id, action=_action, _=Depends(_csrf): _mutate_job(job_id, action), methods=["POST"])


@router.get("/jobs/{job_id}/segments")
def get_segments(job_id: str):
    with SessionLocal() as session:
        if not session.get(Job, job_id): raise HTTPException(404, "job not found")
        return [{"id": x.id, "ordinal": x.ordinal, "start_ms": x.start_ms, "end_ms": x.end_ms, "chunk_index": x.chunk_index, "source_text": x.source_text, "translated_text": x.translated_text, "reading_text": x.reading_text, "tempo": x.tempo, "warnings": x.warnings, "revision": x.revision} for x in session.scalars(select(Segment).where(Segment.job_id == job_id).order_by(Segment.ordinal))]


@router.patch("/jobs/{job_id}/segments")
def patch_segments(job_id: str, payload: list[SegmentPatch], _: None = Depends(_csrf)):
    with SessionLocal() as session:
        if not session.scalar(select(Job.id).where(Job.id == job_id)):
            raise HTTPException(404, "job not found")
        # The subquery is part of each segment UPDATE. It prevents a queued
        # claim/freeze racing with an editor request that already read DRAFT.
        eligible_job = select(Job.id).where(Job.id == job_id, Job.status == "DRAFT", Job.lease_id.is_(None)).scalar_subquery()
        ids = [patch.id for patch in payload]
        present = set(session.scalars(select(Segment.id).where(Segment.job_id == job_id, Segment.id.in_(ids))))
        for segment_id in ids:
            if segment_id not in present:
                raise HTTPException(404, f"segment {segment_id} not found")
        for patch in payload:
            result = session.execute(update(Segment).where(Segment.id == patch.id, Segment.job_id == eligible_job,
                Segment.revision == patch.revision).values(translated_text=patch.translated_text,
                reading_text=patch.reading_text if patch.reading_text is not None else patch.translated_text,
                revision=Segment.revision + 1))
            if result.rowcount != 1:
                session.rollback()
                raise HTTPException(409, f"segment {patch.id} changed or draft is no longer editable")
        job_update = session.execute(update(Job).where(Job.id == job_id, Job.status == "DRAFT", Job.lease_id.is_(None)).values(
            revision=Job.revision + 1, updated_at=now()))
        if job_update.rowcount != 1:
            session.rollback()
            raise HTTPException(409, "draft changed or is no longer editable")
        session.commit()
        return {"revision": session.scalar(select(Job.revision).where(Job.id == job_id))}


@router.post("/jobs/{job_id}/render")
def rerender(job_id: str, _: None = Depends(_csrf)):
    with SessionLocal() as session:
        # Freeze a draft with a single conditional UPDATE. A stale editor cannot
        # queue a draft after another request has changed or claimed it.
        frozen = session.execute(update(Job).where(Job.id == job_id, Job.status == "DRAFT", Job.lease_id.is_(None)).values(
            status="QUEUED", error=None, updated_at=now()))
        if frozen.rowcount == 1:
            session.commit()
            queued = session.get(Job, job_id)
            return _job_dict(session, queued)
        parent = session.get(Job, job_id)
        if not parent: raise HTTPException(404, "job not found")
        if parent.status == "DRAFT": raise HTTPException(409, "draft changed or is already claimed")
        if parent.status not in {"COMPLETED", "COMPLETED_WITH_WARNINGS"} or parent.lease_id is not None:
            raise HTTPException(409, "only a completed, unowned job can be rerendered")
        child = Job(source_id=parent.source_id, original_name=parent.original_name, display_name=parent.display_name,
                    mode_json=parent.mode_json, config_json=parent.config_json,
                    settings_snapshot_json=parent.settings_snapshot_json, priority=parent.priority,
                    parent_job_id=parent.id, revision=parent.revision)
        session.add(child); session.flush()
        for old in session.scalars(select(Segment).where(Segment.job_id == parent.id).order_by(Segment.ordinal)):
            session.add(Segment(job_id=child.id, ordinal=old.ordinal, start_ms=old.start_ms, end_ms=old.end_ms, chunk_index=old.chunk_index, source_text=old.source_text,
                                translated_text=old.translated_text, reading_text=old.reading_text, confidence=old.confidence,
                                warning_json=old.warning_json, revision=old.revision))
        for run in session.scalars(select(StageRun).where(StageRun.job_id == parent.id, StageRun.stage == "asr", StageRun.status == "COMPLETED")):
            session.add(StageRun(job_id=child.id, stage=run.stage, unit_key=run.unit_key, status=run.status, attempts=run.attempts,
                                 artifact_path=None, error=run.error, started_at=run.started_at, finished_at=run.finished_at))
        session.commit(); session.refresh(child); return _job_dict(session, child)


@router.post("/jobs/{job_id}/draft")
def create_draft(job_id: str, _: None = Depends(_csrf)):
    with SessionLocal() as session:
        parent = session.get(Job, job_id)
        if not parent: raise HTTPException(404, "job not found")
        if parent.status not in {"COMPLETED", "COMPLETED_WITH_WARNINGS"} or parent.lease_id is not None:
            raise HTTPException(409, "only a completed, unowned job can be edited")
        child = Job(source_id=parent.source_id, original_name=parent.original_name, display_name=parent.display_name,
                    mode_json=parent.mode_json, config_json=parent.config_json,
                    settings_snapshot_json=parent.settings_snapshot_json, priority=parent.priority,
                    parent_job_id=parent.id, revision=parent.revision, status="DRAFT")
        session.add(child); session.flush()
        for old in session.scalars(select(Segment).where(Segment.job_id == parent.id).order_by(Segment.ordinal)):
            session.add(Segment(job_id=child.id, ordinal=old.ordinal, start_ms=old.start_ms, end_ms=old.end_ms, chunk_index=old.chunk_index, source_text=old.source_text,
                                translated_text=old.translated_text, reading_text=old.reading_text, confidence=old.confidence,
                                warning_json=old.warning_json, revision=old.revision))
        for run in session.scalars(select(StageRun).where(StageRun.job_id == parent.id, StageRun.stage == "asr", StageRun.status == "COMPLETED")):
            session.add(StageRun(job_id=child.id, stage=run.stage, unit_key=run.unit_key, status=run.status, attempts=run.attempts,
                                 artifact_path=None, error=run.error, started_at=run.started_at, finished_at=run.finished_at))
        session.commit(); session.refresh(child); return _job_dict(session, child)


@router.get("/jobs/{job_id}/artifacts")
def artifacts(job_id: str):
    with SessionLocal() as session:
        if not session.scalar(select(Job.id).where(Job.id == job_id)):
            raise HTTPException(404, "job not found")
        return [{"id": a.id, "kind": a.kind, "path": Path(a.path).name, "size_bytes": a.size_bytes,
                 "valid": a.valid, "revision": a.revision} for a in
                session.scalars(select(Artifact).where(Artifact.job_id == job_id).order_by(Artifact.created_at))]


@router.get("/artifacts/{artifact_id}")
def download_artifact(artifact_id: str, request: Request):
    with SessionLocal() as session: artifact = session.get(Artifact, artifact_id)
    if not artifact or not artifact.valid: raise HTTPException(404, "artifact not found")
    path = Path(artifact.path).resolve()
    # Trust the root captured at publication time.  A global output_root edit
    # must not strand completed artifacts or force callers to know historical
    # settings; the stored root is still confined to this one artifact.
    trusted_root = Path(artifact.trusted_root).resolve() if artifact.trusted_root else path.parent
    try: confined(path, settings.output_root)
    except ValueError:
        try: confined(path, trusted_root)
        except ValueError: raise HTTPException(403, "artifact path is outside its trusted root")
    if not path.is_file(): raise HTTPException(404, "artifact file missing")
    media_type = {"video": "video/mp4", "srt": "application/x-subrip",
                  "transcript": "application/json", "report": "application/json"}.get(
                      artifact.kind, "application/octet-stream")
    total = path.stat().st_size; range_header = request.headers.get("range")
    if not range_header: return FileResponse(path, media_type=media_type)
    try:
        start, end = range_header.replace("bytes=", "").split("-"); start = int(start); end = int(end or total - 1); end = min(end, total - 1)
        if start < 0 or start > end: raise ValueError
    except ValueError: raise HTTPException(416, "invalid range")
    def iterator():
        with path.open("rb") as handle:
            handle.seek(start); remaining = end - start + 1
            while remaining:
                data = handle.read(min(1024 * 1024, remaining))
                if not data: break
                remaining -= len(data); yield data
    return StreamingResponse(iterator(), status_code=206, media_type=media_type, headers={"Content-Range": f"bytes {start}-{end}/{total}", "Accept-Ranges": "bytes", "Content-Length": str(end - start + 1)})


@router.get("/events")
async def events(after_id: int = 0):
    async def stream():
        current = after_id
        for _ in range(120):
            with SessionLocal() as session: rows = list(session.scalars(select(Event).where(Event.id > current).order_by(Event.id).limit(100)))
            for event in rows:
                current = event.id; yield f"id: {event.id}\nevent: {event.kind}\ndata: {event.payload_json}\n\n"
            await asyncio.sleep(1)
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/watch-folders")
def watch_folders():
    with SessionLocal() as session: return [{"id": x.id, "path": x.path, "preset": json.loads(x.preset_json), "enabled": x.enabled} for x in session.scalars(select(WatchFolder))]


@router.post("/watch-folders")
def add_watch_folder(payload: WatchCreate, _: None = Depends(_csrf)):
    path = Path(payload.path).expanduser().resolve()
    if not path.is_dir(): raise HTTPException(422, "watch folder does not exist")
    with SessionLocal() as session:
        folder = WatchFolder(path=str(path), preset_json=json_text(payload.preset)); session.add(folder)
        try: session.commit()
        except Exception: session.rollback(); raise HTTPException(409, "watch folder already exists")
        return {"id": folder.id, "path": folder.path, "preset": payload.preset, "enabled": True}


@router.delete("/watch-folders/{folder_id}")
def remove_watch_folder(folder_id: str, _: None = Depends(_csrf)):
    with SessionLocal() as session:
        folder = session.get(WatchFolder, folder_id)
        if not folder: raise HTTPException(404, "watch folder not found")
        session.delete(folder); session.commit(); return {"deleted": True}


@router.get("/settings")
def get_settings(): return settings.manifest() | {"max_file_bytes": settings.max_file_bytes, "max_duration_seconds": settings.max_duration_seconds, "stable_seconds": settings.stable_seconds, "cpu_threads": settings.cpu_threads}


@router.patch("/settings")
def patch_settings(payload: SettingsPatch, _: None = Depends(_csrf)):
    values = payload.model_dump(exclude_none=True)
    active_statuses = {"QUEUED", "RUNNING", "RETRY_WAIT", "PAUSED", "PAUSING", "CANCELLING", "WAITING_RESOURCES"}
    if {key for key in values if key in {"data_root", "output_root"}}:
        with SessionLocal() as session:
            active = session.scalar(select(Job.id).where(Job.status.in_(active_statuses)).limit(1))
            if active:
                raise HTTPException(409, "pause or finish the queue before changing managed paths")
    if "data_root" in values:
        # The SQLite engine is opened at process startup. A data-root migration must
        # be an explicit idle operation; silently moving only the in-memory paths would
        # split the database and artifacts across two stores.
        raise HTTPException(409, "data_root migration requires an explicit offline migration")
    if "output_root" in values:
        # The active-status gate above includes PAUSING/CANCELLING and all
        # retry/resource states. Completed artifact retrieval uses each
        # artifact's stored trust root.
        pass
    for key, value in values.items():
        if key in {"data_root", "output_root"}: value = Path(value).expanduser().resolve()
        setattr(settings, key, value)
    with SessionLocal() as session:
        for key, value in values.items(): session.merge(Setting(key=key, value_json=json_text(str(value) if isinstance(value, Path) else value)))
        session.commit()
    settings.ensure_dirs(); return get_settings()


def create_app() -> FastAPI:
    settings.ensure_dirs(); init_db()
    embedded_queue = os.environ.get("VIDEOAUTO_DISABLE_EMBEDDED_QUEUE") != "1"

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if embedded_queue:
            supervisor.start()
        try:
            yield
        finally:
            if embedded_queue:
                supervisor.stop()

    app = FastAPI(title="Video Auto", version="0.1.0", docs_url="/docs", redoc_url=None, lifespan=lifespan)
    @app.middleware("http")
    async def local_security(request: Request, call_next):
        raw_host = request.headers.get("host", "").strip()
        host = raw_host.split(":", 1)[0].lower()
        host_port = None
        if ":" in raw_host:
            try: host_port = int(raw_host.rsplit(":", 1)[1])
            except ValueError: return JSONResponse({"detail": "invalid host"}, status_code=403)
        if host and (host not in {"127.0.0.1", "localhost", "testserver"} or (host in {"127.0.0.1", "localhost"} and host_port not in {None, 8765, 5173})):
            return JSONResponse({"detail": "localhost only"}, status_code=403)
        origin = request.headers.get("origin")
        if origin:
            parsed_origin = urlparse(origin)
            try: origin_port = parsed_origin.port or (80 if parsed_origin.scheme == "http" else 443)
            except ValueError: return JSONResponse({"detail": "origin denied"}, status_code=403)
            if parsed_origin.scheme != "http" or parsed_origin.hostname not in {"127.0.0.1", "localhost"} or origin_port not in {8765, 5173}:
                return JSONResponse({"detail": "origin denied"}, status_code=403)
        response = await call_next(request)
        if "videoauto_csrf" not in request.cookies: response.set_cookie("videoauto_csrf", secrets.token_urlsafe(32), httponly=False, samesite="strict")
        return response
    app.include_router(router)
    static_root = Path(__file__).parents[2] / "frontend" / "dist"
    if static_root.is_dir():
        app.mount("/", StaticFiles(directory=str(static_root), html=True), name="frontend")
    return app


app = create_app()
