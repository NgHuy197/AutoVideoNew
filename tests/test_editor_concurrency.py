from __future__ import annotations

from pathlib import Path
import tempfile

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app import api
from backend.app.db import Base, Job, Segment, Source


@pytest.fixture()
def editor_db(monkeypatch):
    # The packaged Windows runner denies enumeration of its global TEMP root;
    # keep this isolated test database inside the repository runtime directory.
    root = Path(".runtime")
    root.mkdir(parents=True, exist_ok=True)
    temp = tempfile.TemporaryDirectory(prefix="editor-test-", dir=root)
    tmp_path = Path(temp.name)
    engine = create_engine(f"sqlite:///{(tmp_path / 'editor.sqlite3').as_posix()}", future=True)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, future=True)
    with sessions() as session:
        source = Source(original_name="editor.mp4", path=str(tmp_path / "editor.mp4"), sha256="0" * 64, size_bytes=1)
        session.add(source); session.flush()
        job = Job(source_id=source.id, status="DRAFT", lease_id=None)
        session.add(job); session.flush()
        segment = Segment(job_id=job.id, ordinal=0, start_ms=0, end_ms=1000, source_text="hello",
                          translated_text="original", reading_text="original")
        session.add(segment); session.commit()
        job_id, segment_id = job.id, segment.id
    monkeypatch.setattr(api, "SessionLocal", sessions)
    yield sessions, job_id, segment_id
    engine.dispose(); temp.cleanup()


def test_editor_write_rechecks_revision_after_stale_read(editor_db, monkeypatch):
    sessions, job_id, segment_id = editor_db
    with sessions() as stale:
        stale.get(Job, job_id); stale.get(Segment, segment_id)
        api.patch_segments(job_id, [api.SegmentPatch(id=segment_id, translated_text="winner", revision=1)])
        monkeypatch.setattr(api, "SessionLocal", lambda: stale)
        with pytest.raises(HTTPException) as error:
            api.patch_segments(job_id, [api.SegmentPatch(id=segment_id, translated_text="loser", revision=1)])
        assert error.value.status_code == 409
    with sessions() as fresh:
        assert fresh.get(Segment, segment_id).translated_text == "winner"


def test_editor_write_rechecks_draft_freeze_after_stale_read(editor_db, monkeypatch):
    sessions, job_id, segment_id = editor_db
    with sessions() as stale:
        stale.get(Job, job_id); stale.get(Segment, segment_id)
        api.rerender(job_id)
        monkeypatch.setattr(api, "SessionLocal", lambda: stale)
        with pytest.raises(HTTPException) as error:
            api.patch_segments(job_id, [api.SegmentPatch(id=segment_id, translated_text="too late", revision=1)])
        assert error.value.status_code == 409
    with sessions() as fresh:
        assert fresh.get(Job, job_id).status == "QUEUED"
        assert fresh.get(Segment, segment_id).translated_text == "original"


def test_rerender_rejects_active_or_queued_parent(editor_db):
    sessions, job_id, _ = editor_db
    with sessions() as session:
        job = session.get(Job, job_id)
        job.status = "RUNNING"
        job.lease_id = "active-generation"
        session.commit()
    with pytest.raises(HTTPException) as error:
        api.rerender(job_id)
    assert error.value.status_code == 409
