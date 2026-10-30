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
    assert page["counts"] == {"active": 6, "done": 1, "failed": 2, "pausable": 4, "paused": 1}
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


def test_pause_queue_holds_waiting_jobs_and_asks_running_workers(queue_db):
    sessions, source_id = queue_db
    jobs = {status: _add_job(sessions, source_id, status)
            for status in ("QUEUED", "WAITING_RESOURCES", "RUNNING", "COMPLETED", "FAILED", "DRAFT")}

    assert api.pause_queue() == {"held": 2, "pausing": 1}

    with sessions() as session:
        result = {status: session.get(Job, job_id) for status, job_id in jobs.items()}
    assert result["QUEUED"].status == "PAUSED"
    assert result["WAITING_RESOURCES"].status == "PAUSED"
    assert result["RUNNING"].status == "PAUSING" and result["RUNNING"].pause_requested is True
    assert [result[status].status for status in ("COMPLETED", "FAILED", "DRAFT")] == ["COMPLETED", "FAILED", "DRAFT"]


def test_resume_queue_requeues_paused_and_keeps_unstopped_worker(queue_db):
    sessions, source_id = queue_db
    paused = _add_job(sessions, source_id, "PAUSED")
    pausing = _add_job(sessions, source_id, "PAUSING")
    failed = _add_job(sessions, source_id, "FAILED")
    with sessions() as session:
        session.get(Job, pausing).pause_requested = True
        session.commit()

    assert api.resume_queue() == {"resumed": 2}

    with sessions() as session:
        assert session.get(Job, paused).status == "QUEUED"
        assert session.get(Job, pausing).status == "RUNNING"
        assert session.get(Job, pausing).pause_requested is False
        assert session.get(Job, failed).status == "FAILED"


def test_clear_completed_removes_only_finished_jobs_and_their_rows(queue_db):
    sessions, source_id = queue_db
    done = _add_job(sessions, source_id, "COMPLETED")
    warned = _add_job(sessions, source_id, "COMPLETED_WITH_WARNINGS")
    keep = {_add_job(sessions, source_id, status) for status in ("QUEUED", "RUNNING", "FAILED", "PAUSED", "DRAFT")}
    with sessions() as session:
        session.add(Segment(job_id=done, ordinal=0, start_ms=0, end_ms=1000, source_text="hi"))
        session.add(Artifact(job_id=warned, kind="video", path="out.mp4"))
        session.add(Event(job_id=done, kind="completed"))
        session.get(Job, next(iter(keep))).parent_job_id = done
        session.commit()

    assert api.clear_completed_jobs() == {"deleted": 2, "media_preserved": True}

    with sessions() as session:
        assert set(session.scalars(select(Job.id))) == keep
        assert not list(session.scalars(select(Job).where(Job.parent_job_id.is_not(None))))
        for model in (Segment, Artifact, Event):
            assert not list(session.scalars(select(model)))
    assert api.clear_completed_jobs() == {"deleted": 0, "media_preserved": True}


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


def test_output_folder_list_hides_per_video_export_folders(tmp_path):
    from backend.app.storage_folders import list_folders
    (tmp_path / "Khach hang" / "clip").mkdir(parents=True)
    (tmp_path / "Khach hang" / "clip" / "clip.vi.12345678.abcdef12.mp4").write_bytes(b"x")
    (tmp_path / "Download").mkdir()
    (tmp_path / "Download" / "Download.vi.12345678.manual.mp4").write_bytes(b"x")
    (tmp_path / "Empty group").mkdir()

    output = [row["relative"] for row in list_folders(tmp_path, skip_exports=True)]
    every = [row["relative"] for row in list_folders(tmp_path)]

    assert output == ["", "Empty group", "Khach hang"]
    assert "Download" in every and "Khach hang/clip" in every


def test_reaper_keeps_pipeline_failure_reason(queue_db, monkeypatch):
    from types import SimpleNamespace
    from backend.app import service
    sessions, source_id = queue_db
    job_id = _add_job(sessions, source_id, "FAILED")
    crashed_id = _add_job(sessions, source_id, "RUNNING")
    with sessions() as session:
        job = session.get(Job, job_id); job.error = "Whisper model is missing"; job.retry_count = 2
        session.get(Job, crashed_id).retry_count = 2
        session.commit()
    monkeypatch.setattr(service, "SessionLocal", sessions)
    monkeypatch.setattr(service, "close_owned", lambda child: None)
    queue = service.QueueSupervisor()
    for owner in (job_id, crashed_id):
        queue._children[owner] = SimpleNamespace(process=SimpleNamespace(returncode=0, poll=lambda: 0))

    queue._reap()

    with sessions() as session:
        assert session.get(Job, job_id).error == "Whisper model is missing"
        assert "dừng đột ngột (mã 0)" in session.get(Job, crashed_id).error
