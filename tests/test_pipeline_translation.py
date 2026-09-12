from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.db import Base, Job, Segment, Source, StageRun, now
from backend.app.pipeline import Pipeline, STAGE_TIMEOUTS_SECONDS
from backend.app.translate import Translation
from backend.app.media import MediaError


class _FakeTranslator:
    batch_size = 2

    def __init__(self, *, fail_on_call: int | None = None):
        self.fail_on_call = fail_on_call
        self.calls: list[tuple[list[str], int, float | None]] = []

    def translate_batch(self, texts: list[str], _source_code: str, *, batch_size: int,
                        timeout_seconds: float | None):
        self.calls.append((list(texts), batch_size, timeout_seconds))
        if self.fail_on_call == len(self.calls):
            raise MediaError("simulated translation failure")
        return [Translation(f"vi:{text}", "eng_Latn") for text in texts]


def _sessions(tmp_path: Path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'translation.sqlite3').as_posix()}", future=True)
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, future=True)


def test_translation_batches_checkpoint_independently_and_resume_missing_only(tmp_path, monkeypatch):
    _engine, sessions = _sessions(tmp_path)
    monkeypatch.setattr(Pipeline, "_ensure_owned", lambda _self: None)
    with sessions() as session:
        source = Source(original_name="source.mp4", path=str(tmp_path / "source.mp4"), sha256="1" * 64, size_bytes=1)
        session.add(source); session.flush()
        job = Job(source_id=source.id, status="RUNNING", lease_id="lease-a", cancel_requested=False)
        session.add(job); session.flush()
        segments = [Segment(job_id=job.id, ordinal=index, start_ms=index * 1000,
                            end_ms=(index + 1) * 1000, source_text=f"cue-{index}")
                    for index in range(5)]
        session.add_all(segments); session.commit()

        first = _FakeTranslator(fail_on_call=2)
        with pytest.raises(MediaError, match="simulated translation failure"):
            Pipeline(job.id, "lease-a")._translate_segments_in_batches(session, segments, first, "en")
        assert first.calls == [
            (["cue-0", "cue-1"], 2, STAGE_TIMEOUTS_SECONDS["translation"]),
            (["cue-2", "cue-3"], 2, STAGE_TIMEOUTS_SECONDS["translation"]),
        ]

        session.expire_all()
        rows = list(session.scalars(select(Segment).where(Segment.job_id == job.id).order_by(Segment.ordinal)))
        runs = {run.unit_key: run.status for run in session.scalars(select(StageRun).where(StageRun.job_id == job.id))}
        assert [row.translated_text for row in rows] == ["vi:cue-0", "vi:cue-1", "", "", ""]
        assert runs == {"batch:0": "COMPLETED", "batch:1": "RUNNING"}

    with sessions() as session:
        rows = list(session.scalars(select(Segment).where(Segment.job_id == job.id).order_by(Segment.ordinal)))
        resumed = _FakeTranslator()
        Pipeline(job.id, "lease-a")._translate_segments_in_batches(session, rows, resumed, "en")
        assert resumed.calls == [
            (["cue-2", "cue-3"], 2, STAGE_TIMEOUTS_SECONDS["translation"]),
            (["cue-4"], 2, STAGE_TIMEOUTS_SECONDS["translation"]),
        ]
        assert all(row.translated_text == f"vi:cue-{row.ordinal}" for row in rows)
        assert {run.unit_key: run.status for run in session.scalars(select(StageRun).where(StageRun.job_id == job.id))} == {
            "batch:0": "COMPLETED", "batch:1": "COMPLETED", "batch:2": "COMPLETED"
        }
    _engine.dispose()


def test_model_loading_can_clear_batch_deadline_under_lease(tmp_path):
    _engine, sessions = _sessions(tmp_path)
    with sessions() as session:
        source = Source(original_name="source.mp4", path=str(tmp_path / "source.mp4"), sha256="2" * 64, size_bytes=1)
        session.add(source); session.flush()
        deadline = now() + timedelta(seconds=STAGE_TIMEOUTS_SECONDS["translation"])
        job = Job(source_id=source.id, status="RUNNING", lease_id="lease-b", stage_deadline_at=deadline)
        session.add(job); session.commit()
        Pipeline(job.id, "lease-b")._clear_stage_deadline(session)
        session.refresh(job)
        assert job.stage_deadline_at is None
    _engine.dispose()
