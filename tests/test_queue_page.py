from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app import api
from backend.app.db import Artifact, Base, Event, Job, Segment, Source, StageRun, json_text


@pytest.fixture()
def queue_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'queue.sqlite3').as_posix()}", future=True)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, future=True)
    with sessions() as session:
        source_path = tmp_path / "queue.mp4"
        source_path.write_bytes(b"video")
        source = Source(original_name="queue.mp4", path=str(source_path), sha256="9" * 64, size_bytes=5)
        session.add(source); session.commit()
        source_id = source.id
    monkeypatch.setattr(api, "SessionLocal", sessions)
    yield sessions, source_id
    engine.dispose()


def _add_job(sessions, source_id: str, status: str) -> str:
    mode = json_text(api._normalize_preset({}))
    with sessions() as session:
        job = Job(source_id=source_id, status=status, mode_json=mode, config_json=mode)
        session.add(job); session.commit()
        return job.id


def test_page_counts_every_unfinished_status_as_active(queue_db):
    sessions, source_id = queue_db
    for status in ("QUEUED", "RUNNING", "PAUSING", "CANCELLING", "RETRY_WAIT", "WAITING_RESOURCES",
                   "COMPLETED", "FAILED", "DRAFT"):
        _add_job(sessions, source_id, status)

    page = api.page_jobs(limit=4, offset=0)

    assert page["total"] == 9
    assert len(page["items"]) == 4
    assert page["counts"] == {"active": 6, "done": 1, "failed": 2}
    assert len(api.page_jobs(limit=4, offset=8)["items"]) == 1


def test_delete_job_removes_queue_rows_including_events(queue_db):
    sessions, source_id = queue_db
    job_id = _add_job(sessions, source_id, "COMPLETED")
    keep_id = _add_job(sessions, source_id, "COMPLETED")
    with sessions() as session:
        for owner in (job_id, keep_id):
            session.add(Segment(job_id=owner, ordinal=0, start_ms=0, end_ms=1000, source_text="hi"))
            session.add(StageRun(job_id=owner, stage="render", unit_key="all", status="COMPLETED"))
            session.add(Artifact(job_id=owner, kind="video", path="out.mp4"))
            session.add(Event(job_id=owner, kind="completed"))
        session.commit()

    assert api.delete_job(job_id) == {"deleted": True, "id": job_id, "media_preserved": True}

    with sessions() as session:
        assert session.get(Job, job_id) is None
        for model in (Segment, StageRun, Artifact, Event):
            owners = {row.job_id for row in session.scalars(select(model))}
            assert owners == {keep_id}


def test_delete_job_refuses_running_and_unknown_jobs(queue_db):
    sessions, source_id = queue_db
    running_id = _add_job(sessions, source_id, "RUNNING")

    with pytest.raises(HTTPException) as running:
        api.delete_job(running_id)
    with pytest.raises(HTTPException) as missing:
        api.delete_job("missing")

    assert running.value.status_code == 409
    assert missing.value.status_code == 404
    with sessions() as session:
        assert session.get(Job, running_id) is not None


def _watch_setup(tmp_path, monkeypatch, sessions):
    source_path = tmp_path / "watched.mp4"
    source_path.write_bytes(b"watched video")
    monkeypatch.setattr(api.settings, "sources_root", tmp_path / "managed-sources")
    with sessions() as session:
        source = Source(original_name=source_path.name, path=str(source_path),
                        sha256=api.sha256_file(source_path), size_bytes=source_path.stat().st_size)
        session.add(source); session.commit()
        return source_path, source.id


@pytest.mark.parametrize("dropped", [("input_folder", "output_folder"), ("output_folder",),
                                     ("review_before_render", "input_folder")])
def test_watch_dedup_accepts_each_legacy_key_subset(queue_db, tmp_path, monkeypatch, dropped):
    sessions, _ = queue_db
    source_path, source_id = _watch_setup(tmp_path, monkeypatch, sessions)
    legacy = {key: value for key, value in api._normalize_preset({}).items() if key not in dropped}
    with sessions() as session:
        existing = Job(source_id=source_id, status="COMPLETED", mode_json=json_text(legacy), config_json=json_text(legacy))
        session.add(existing); session.commit()
        existing_id = existing.id

    _source, reused = api.create_source_and_job(source_path, {}, watch_folder_id="watch-folder")

    assert reused.id == existing_id


def test_watch_reuses_job_from_web_upload_with_other_preset(queue_db, tmp_path, monkeypatch):
    sessions, _ = queue_db
    source_path, source_id = _watch_setup(tmp_path, monkeypatch, sessions)
    upload_mode = json_text(api._normalize_preset({"input_folder": "khach-hang", "subtitle_mode": "srt"}))
    with sessions() as session:
        uploaded = Job(source_id=source_id, upload_id="upload-1", status="QUEUED",
                       mode_json=upload_mode, config_json=upload_mode)
        session.add(uploaded); session.commit()
        uploaded_id = uploaded.id

    _source, reused = api.create_source_and_job(source_path, {}, watch_folder_id="watch-folder")

    assert reused.id == uploaded_id
    with sessions() as session:
        assert len(list(session.scalars(select(Job)))) == 1
