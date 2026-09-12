from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app import maintenance, service
from backend.app.db import Base, Job, Segment, Source
from backend.app.diagnostics import identity_matches, process_identity
from backend.app.process_control import close_owned, spawn_owned, terminate_owned


def _sessions(tmp_path: Path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'reliability.sqlite3').as_posix()}", future=True)
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, future=True)


def test_resource_waiters_resume_after_capacity_returns(tmp_path, monkeypatch):
    engine, sessions = _sessions(tmp_path)
    monkeypatch.setattr(service, "SessionLocal", sessions)
    with sessions() as session:
        source = Source(original_name="wait.mp4", path=str(tmp_path / "wait.mp4"), sha256="a" * 64, size_bytes=1)
        session.add(source); session.flush()
        job = Job(source_id=source.id, status="QUEUED")
        session.add(job); session.commit(); job_id = job.id
    queue = service.QueueSupervisor()
    queue._update_resource_waiters(False, "RAM reserve")
    with sessions() as session:
        assert session.get(Job, job_id).status == "WAITING_RESOURCES"
    queue._update_resource_waiters(True, "ready")
    with sessions() as session:
        restored = session.get(Job, job_id)
        assert restored.status == "QUEUED" and restored.error is None
    engine.dispose()


def test_retention_keeps_live_references_and_final_outputs(tmp_path, monkeypatch):
    engine, sessions = _sessions(tmp_path)
    monkeypatch.setattr(maintenance, "SessionLocal", sessions)
    for name in ("jobs", "cache", "logs", "backups"):
        (tmp_path / name).mkdir()
    old = time.time() - 31 * 86400
    output = tmp_path / "final.mp4"; output.write_bytes(b"final")
    cache_live = tmp_path / "cache" / "tts" / "live.wav"; cache_live.parent.mkdir(parents=True, exist_ok=True); cache_live.write_bytes(b"live")
    cache_dead = tmp_path / "cache" / "tts" / "dead.wav"; cache_dead.write_bytes(b"dead")
    job_dir = tmp_path / "jobs" / "job-old"; job_dir.mkdir(parents=True)
    intermediate = job_dir / "audio.wav"; intermediate.write_bytes(b"audio")
    transcript = job_dir / "transcript.json"; transcript.write_text("{}", encoding="utf-8")
    for path in (cache_live, cache_dead, intermediate, transcript):
        os.utime(path, (old, old))
    monkeypatch.setattr(maintenance.settings, "jobs_root", tmp_path / "jobs")
    monkeypatch.setattr(maintenance.settings, "cache_root", tmp_path / "cache")
    monkeypatch.setattr(maintenance.settings, "logs_root", tmp_path / "logs")
    monkeypatch.setattr(maintenance.settings, "backups_root", tmp_path / "backups")
    with sessions() as session:
        source = Source(original_name="old.mp4", path=str(tmp_path / "source.mp4"), sha256="b" * 64, size_bytes=1)
        session.add(source); session.flush()
        job = Job(id="job-old", source_id=source.id, status="COMPLETED",
                  completed_at=datetime.now(timezone.utc) - timedelta(days=31))
        session.add(job); session.flush()
        session.add(Segment(job_id=job.id, ordinal=0, start_ms=0, end_ms=1000,
                             source_text="x", tts_path=str(cache_live)))
        session.commit()
    with sessions() as session:
        protected = maintenance._protected_paths(session)
        maintenance._prune_intermediates(session, now_epoch=time.time())
    maintenance._prune_cache(now_epoch=time.time(), protected=protected)
    assert cache_live.exists()
    assert not cache_dead.exists()
    assert not intermediate.exists()
    assert transcript.exists()
    assert output.exists()
    engine.dispose()


def test_owned_process_has_creation_fence_and_terminates_tree(tmp_path):
    child = spawn_owned(
        [sys.executable, "-X", "utf8", "-c", "import time; print('owned', flush=True); time.sleep(60)"],
        cwd=Path.cwd(), log_path=tmp_path / "worker.log", job_name=f"VideoAuto-test-{os.getpid()}-{time.time_ns()}")
    try:
        assert child.is_running() and child.identity is not None
        assert identity_matches(process_identity(child.pid), pid=child.pid,
                                started_at=child.identity.create_time)
        assert terminate_owned(child)
        assert process_identity(child.pid) is None
    finally:
        close_owned(child)
