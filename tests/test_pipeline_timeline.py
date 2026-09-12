from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.db import Base, Job, Segment, Source
from backend.app.pipeline import _normalize_chunk_segments


def _session(tmp_path: Path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'timeline.sqlite3').as_posix()}", future=True)
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, future=True)


def test_reconciles_translated_checkpoint_at_real_chunk_boundary(tmp_path):
    engine, sessions = _session(tmp_path)
    with sessions() as session:
        source = Source(original_name="long.mp4", path=str(tmp_path / "long.mp4"), sha256="1" * 64, size_bytes=1)
        session.add(source); session.flush()
        job = Job(source_id=source.id, status="FAILED")
        session.add(job); session.flush()
        first = Segment(job_id=job.id, ordinal=0, chunk_index=0, start_ms=55_000, end_ms=62_000,
                        source_text="And so, ask not what your country can do for you.",
                        translated_text="Và vì vậy, đừng hỏi đất nước của bạn có thể làm gì cho bạn.",
                        reading_text="Và vì vậy, đừng hỏi đất nước của bạn có thể làm gì cho bạn.",
                        tts_path=str(tmp_path / "old-first.wav"), tts_duration_ms=7_000)
        second = Segment(job_id=job.id, ordinal=1, chunk_index=1, start_ms=60_000, end_ms=65_440,
                         source_text="What your country can do for you, ask what you can do.",
                         translated_text="Những gì đất nước của bạn có thể làm cho bạn, hãy hỏi bạn có thể làm gì.",
                         reading_text="Những gì đất nước của bạn có thể làm cho bạn, hãy hỏi bạn có thể làm gì.",
                         tts_path=str(tmp_path / "old-second.wav"), tts_duration_ms=5_440)
        session.add_all([first, second]); session.commit()
        _normalize_chunk_segments(session, job.id, 66_320)
        session.commit()
        rows = list(session.scalars(select(Segment).where(Segment.job_id == job.id).order_by(Segment.ordinal)))
        assert len(rows) == 2
        assert rows[0].end_ms == 60_000
        assert rows[1].start_ms == 60_000 and rows[1].end_ms <= 66_320
        # The automatically generated translation is cleared because its
        # source was shortened; pipeline translation/TTS must regenerate it.
        assert rows[1].source_text == "ask what you can do."
        assert rows[1].translated_text == "" and rows[1].reading_text == ""
        assert rows[0].tts_path is None and rows[1].tts_path is None
    engine.dispose()


def test_dedup_is_case_punctuation_aware_and_boundary_only(tmp_path):
    engine, sessions = _session(tmp_path)
    with sessions() as session:
        source = Source(original_name="short.mp4", path=str(tmp_path / "short.mp4"), sha256="2" * 64, size_bytes=1)
        session.add(source); session.flush()
        job = Job(source_id=source.id, status="RUNNING")
        session.add(job); session.flush()
        session.add_all([
            Segment(job_id=job.id, ordinal=0, chunk_index=0, start_ms=58_000, end_ms=60_000, source_text="Hello, world", translated_text="", reading_text=""),
            Segment(job_id=job.id, ordinal=1, chunk_index=0, start_ms=59_000, end_ms=60_500, source_text="Hello, hello", translated_text="", reading_text=""),
            Segment(job_id=job.id, ordinal=2, chunk_index=1, start_ms=60_000, end_ms=61_000, source_text="Hello! Again", translated_text="", reading_text=""),
        ])
        session.commit()
        _normalize_chunk_segments(session, job.id, 61_000)
        session.commit()
        rows = list(session.scalars(select(Segment).where(Segment.job_id == job.id).order_by(Segment.ordinal)))
        # The same-chunk repetition remains. Only the next chunk loses its
        # case/punctuation-insensitive repeated prefix.
        assert any(row.source_text == "Hello, hello" for row in rows)
        assert any(row.source_text == "Again" for row in rows)
    engine.dispose()


def test_editor_revision_is_never_trimmed(tmp_path):
    engine, sessions = _session(tmp_path)
    with sessions() as session:
        source = Source(original_name="edited.mp4", path=str(tmp_path / "edited.mp4"), sha256="3" * 64, size_bytes=1)
        session.add(source); session.flush()
        job = Job(source_id=source.id, status="RUNNING")
        session.add(job); session.flush()
        session.add_all([
            Segment(job_id=job.id, ordinal=0, chunk_index=0, start_ms=55_000, end_ms=62_000, source_text="what your country can do for you", translated_text="Bản trước", reading_text="Bản trước"),
            Segment(job_id=job.id, ordinal=1, chunk_index=1, start_ms=60_000, end_ms=65_000, source_text="What your country can do for you, today", translated_text="Bản người dùng", reading_text="Bản người dùng", revision=2),
        ])
        session.commit()
        _normalize_chunk_segments(session, job.id, 66_000)
        session.commit()
        edited = session.scalar(select(Segment).where(Segment.job_id == job.id, Segment.ordinal == 1))
        assert edited.source_text == "What your country can do for you, today"
        assert edited.translated_text == "Bản người dùng"
        assert edited.start_ms == 60_000 and edited.end_ms == 65_000
    engine.dispose()


def test_boundary_dedup_requires_raw_temporal_overlap(tmp_path):
    engine, sessions = _session(tmp_path)
    with sessions() as session:
        source = Source(original_name="disjoint.mp4", path=str(tmp_path / "disjoint.mp4"), sha256="4" * 64, size_bytes=1)
        session.add(source); session.flush()
        job = Job(source_id=source.id, status="RUNNING")
        session.add(job); session.flush()
        session.add_all([
            Segment(job_id=job.id, ordinal=0, chunk_index=0, start_ms=1_000, end_ms=2_000, source_text="yes"),
            Segment(job_id=job.id, ordinal=1, chunk_index=1, start_ms=60_000, end_ms=61_000, source_text="yes please"),
        ])
        session.commit()
        _normalize_chunk_segments(session, job.id, 61_000)
        session.commit()
        rows = list(session.scalars(select(Segment).where(Segment.job_id == job.id).order_by(Segment.ordinal)))
        assert [row.source_text for row in rows] == ["yes", "yes please"]
    engine.dispose()


def test_hyphenated_token_uses_consistent_trim_span(tmp_path):
    engine, sessions = _session(tmp_path)
    with sessions() as session:
        source = Source(original_name="hyphen.mp4", path=str(tmp_path / "hyphen.mp4"), sha256="5" * 64, size_bytes=1)
        session.add(source); session.flush()
        job = Job(source_id=source.id, status="RUNNING")
        session.add(job); session.flush()
        session.add_all([
            Segment(job_id=job.id, ordinal=0, chunk_index=0, start_ms=59_000, end_ms=60_500, source_text="Well-being"),
            Segment(job_id=job.id, ordinal=1, chunk_index=1, start_ms=60_000, end_ms=61_000, source_text="well-being matters"),
        ])
        session.commit()
        _normalize_chunk_segments(session, job.id, 61_000)
        session.commit()
        rows = list(session.scalars(select(Segment).where(Segment.job_id == job.id).order_by(Segment.ordinal)))
        assert len(rows) == 2
        assert rows[1].source_text == "matters"
    engine.dispose()
