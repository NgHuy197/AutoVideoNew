from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import sessionmaker

from backend.app import api, pipeline
from backend.app.db import Base, Event, Job, Segment, Source, StageRun, json_text


def _mock_pipeline_media(monkeypatch, tmp_path: Path, calls: list[str]) -> None:
    class FakeSynthesizer:
        def __init__(self, *_args, **_kwargs):
            calls.append("tts:init")

        def synthesize(self, _text, path, **_kwargs):
            calls.append("tts:synthesize")
            return path, 500

    monkeypatch.setattr(pipeline.settings, "jobs_root", tmp_path / "jobs")
    monkeypatch.setattr(pipeline.settings, "output_root", tmp_path / "output")
    monkeypatch.setattr(pipeline.settings, "piper_model", tmp_path / "piper.onnx")
    monkeypatch.setattr(pipeline.settings, "piper_config", None)
    monkeypatch.setattr(pipeline, "validate_video", lambda _path: {
        "duration": 1.0,
        "audio": [{"start_time": 0}],
        "video": {"width": 1280, "height": 720},
    })
    monkeypatch.setattr(pipeline, "extract_audio", lambda *_args, **_kwargs: calls.append("extract"))
    monkeypatch.setattr(pipeline, "PiperSynthesizer", FakeSynthesizer)
    monkeypatch.setattr(
        pipeline,
        "fit_to_slot",
        lambda _source, destination, _duration: (destination, 500, 1.0, []),
    )
    monkeypatch.setattr(
        pipeline,
        "build_voice_track",
        lambda *_args, **_kwargs: calls.append("build_voice") or (tmp_path / "voice.wav"),
    )
    monkeypatch.setattr(
        pipeline,
        "render_video",
        lambda *_args, **_kwargs: calls.append("render") or {},
    )


@pytest.fixture()
def review_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'script-review.sqlite3').as_posix()}", future=True)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, future=True)
    with sessions() as session:
        source_path = tmp_path / "review.mp4"
        source_path.write_bytes(b"video")
        source = Source(original_name="review.mp4", path=str(source_path), sha256="7" * 64, size_bytes=5)
        session.add(source); session.flush()
        review_config = {
            "audio_mode": "replace",
            "subtitle_mode": "burn",
            "include_dubbing": True,
            "source_language": "vi",
            "review_before_render": True,
        }
        job = Job(
            source_id=source.id,
            status="DRAFT",
            stage="review",
            progress=58,
            lease_id=None,
            mode_json=json.dumps(review_config),
            config_json=json.dumps(review_config),
        )
        session.add(job); session.flush()
        segment = Segment(
            job_id=job.id,
            ordinal=0,
            start_ms=0,
            end_ms=1000,
            source_text="hello",
            translated_text="xin chao",
            reading_text="xin chao",
        )
        session.add(segment)
        session.add(StageRun(job_id=job.id, stage="review", unit_key="all", status="WAITING"))
        session.commit()
        job_id, segment_id = job.id, segment.id
    monkeypatch.setattr(api, "SessionLocal", sessions)
    monkeypatch.setattr(pipeline, "SessionLocal", sessions)
    yield sessions, job_id, segment_id, tmp_path
    engine.dispose()


def test_normalize_preset_defaults_script_review_to_fully_automatic():
    assert api._normalize_preset({})["review_before_render"] is False
    assert api._normalize_preset({"review_before_render": True})["review_before_render"] is True


def test_direct_job_request_accepts_script_review_option():
    payload = api.JobCreate(source_id="source", review_before_render=True)

    assert payload.model_dump()["review_before_render"] is True


@pytest.mark.parametrize("invalid", [None, 0, 1, "true", [], {}])
def test_normalize_preset_rejects_non_boolean_script_review(invalid):
    with pytest.raises(ValueError, match="review_before_render must be a boolean"):
        api._normalize_preset({"review_before_render": invalid})


def test_review_checkpoint_segments_remain_editable(review_db):
    sessions, job_id, segment_id, _ = review_db

    response = api.patch_segments(
        job_id,
        [api.SegmentPatch(id=segment_id, translated_text="ban da sua", reading_text="ban doc", revision=1)],
    )

    assert response["revision"] == 2
    with sessions() as session:
        job = session.get(Job, job_id)
        segment = session.get(Segment, segment_id)
        assert job.status == "DRAFT"
        assert job.stage == "review"
        assert segment.translated_text == "ban da sua"
        assert segment.reading_text == "ban doc"
        assert segment.revision == 2


def test_segment_patch_returns_revision_from_its_own_write(review_db, monkeypatch):
    sessions, job_id, segment_id, _ = review_db
    primary = sessions()
    original_commit = primary.commit
    interleaved = False

    def commit_then_concurrent_edit():
        nonlocal interleaved
        original_commit()
        if interleaved:
            return
        interleaved = True
        with sessions() as concurrent:
            concurrent.execute(update(Segment).where(
                Segment.id == segment_id,
                Segment.revision == 2,
            ).values(translated_text="second editor", reading_text="second editor", revision=3))
            concurrent.execute(update(Job).where(
                Job.id == job_id,
                Job.revision == 2,
            ).values(revision=3))
            concurrent.commit()

    monkeypatch.setattr(primary, "commit", commit_then_concurrent_edit)
    monkeypatch.setattr(api, "SessionLocal", lambda: primary)

    response = api.patch_segments(
        job_id,
        [api.SegmentPatch(id=segment_id, translated_text="first editor", revision=1)],
    )

    assert interleaved is True
    assert response["revision"] == 2
    with sessions() as session:
        assert session.get(Job, job_id).revision == 3
        assert session.get(Segment, segment_id).translated_text == "second editor"


def test_render_approves_review_checkpoint_and_requeues_same_job(review_db):
    sessions, job_id, _, _ = review_db

    response = api.rerender(job_id, api.RenderRequest(expected_revision=1))

    assert response["id"] == job_id
    assert response["status"] == "QUEUED"
    with sessions() as session:
        job = session.get(Job, job_id)
        assert job.status == "QUEUED"
        assert job.config["review_before_render"] is True
        assert job.lease_id is None
        review = session.scalar(select(StageRun).where(
            StageRun.job_id == job_id,
            StageRun.stage == "review",
            StageRun.unit_key == "all",
        ))
        assert review is not None
        assert review.status == "COMPLETED"


def test_render_clears_stale_pause_flags_when_approving_review(review_db):
    sessions, job_id, _, _ = review_db
    with sessions() as session:
        job = session.get(Job, job_id)
        job.pause_requested = True
        job.pause_acknowledged = True
        session.commit()

    response = api.rerender(job_id, api.RenderRequest(expected_revision=1))

    assert response["status"] == "QUEUED"
    with sessions() as session:
        job = session.get(Job, job_id)
        assert job.pause_requested is False
        assert job.pause_acknowledged is False


def test_review_approval_rejects_stale_job_revision(review_db):
    sessions, job_id, segment_id, _ = review_db
    api.patch_segments(
        job_id,
        [api.SegmentPatch(id=segment_id, translated_text="newer edit", revision=1)],
    )
    client = TestClient(api.create_app())
    try:
        client.get("/api/v1/health/live")
        csrf = client.cookies["videoauto_csrf"]
        response = client.post(
            f"/api/v1/jobs/{job_id}/render",
            json={"expected_revision": 1},
            headers={"X-CSRF-Token": csrf},
        )
    finally:
        client.close()

    assert response.status_code == 409
    with sessions() as session:
        job = session.get(Job, job_id)
        assert job.status == "DRAFT"
        assert job.revision == 2
        review = session.scalar(select(StageRun).where(
            StageRun.job_id == job_id,
            StageRun.stage == "review",
            StageRun.unit_key == "all",
        ))
        assert review is not None
        assert review.status == "WAITING"


def test_review_approval_requires_job_revision(review_db):
    sessions, job_id, _, _ = review_db

    with pytest.raises(api.HTTPException) as error:
        api.rerender(job_id)

    assert error.value.status_code == 422
    with sessions() as session:
        assert session.get(Job, job_id).status == "DRAFT"


def test_pipeline_stops_after_translation_for_script_review(review_db, monkeypatch):
    sessions, job_id, _, tmp_path = review_db
    lease_id = "review-lease"
    with sessions() as session:
        job = session.get(Job, job_id)
        segment = session.scalar(select(Segment).where(Segment.job_id == job_id))
        job.status = "RUNNING"
        job.stage = "translation"
        job.lease_id = lease_id
        job.lease_at = pipeline.now()
        segment.translated_text = ""
        segment.reading_text = ""
        session.query(StageRun).filter_by(job_id=job_id, stage="review").delete()
        session.commit()

    calls: list[str] = []

    class FakeSynthesizer:
        def __init__(self, *_args, **_kwargs):
            calls.append("tts:init")

        def synthesize(self, _text, path, **_kwargs):
            calls.append("tts:synthesize")
            return path, 500

    monkeypatch.setattr(pipeline.settings, "jobs_root", tmp_path / "jobs")
    monkeypatch.setattr(pipeline.settings, "output_root", tmp_path / "output")
    monkeypatch.setattr(pipeline.settings, "piper_model", tmp_path / "piper.onnx")
    monkeypatch.setattr(pipeline.settings, "piper_config", None)
    monkeypatch.setattr(pipeline, "validate_video", lambda _path: {
        "duration": 1.0,
        "audio": [{"start_time": 0}],
        "video": {"width": 1280, "height": 720},
    })
    monkeypatch.setattr(pipeline, "extract_audio", lambda *_args, **_kwargs: calls.append("extract"))
    monkeypatch.setattr(pipeline, "PiperSynthesizer", FakeSynthesizer)
    monkeypatch.setattr(
        pipeline,
        "fit_to_slot",
        lambda _source, destination, _duration: (destination, 500, 1.0, []),
    )
    monkeypatch.setattr(
        pipeline,
        "build_voice_track",
        lambda *_args, **_kwargs: calls.append("build_voice") or (tmp_path / "voice.wav"),
    )
    monkeypatch.setattr(
        pipeline,
        "render_video",
        lambda *_args, **_kwargs: calls.append("render") or {},
    )

    pipeline.Pipeline(job_id, lease_id).run()

    with sessions() as session:
        job = session.get(Job, job_id)
        segment = session.get(Segment, session.query(Segment.id).filter_by(job_id=job_id).scalar())
        assert job.status == "DRAFT"
        assert job.stage == "review"
        assert job.progress == 58
        assert job.lease_id is None
        assert job.lease_at is None
        assert job.stage_deadline_at is None
        assert job.worker_pid is None
        assert job.worker_started_at is None
        assert segment.translated_text == "hello"
        assert segment.reading_text == "hello"
        review = session.scalar(select(StageRun).where(
            StageRun.job_id == job_id,
            StageRun.stage == "review",
            StageRun.unit_key == "all",
        ))
        assert review is not None
        assert review.status == "WAITING"
        event = session.scalar(select(Event).where(
            Event.job_id == job_id,
            Event.kind == "review_required",
        ))
        assert event is not None
    assert not any(call.startswith("tts:") or call in {"build_voice", "render"} for call in calls)


def test_approved_blank_segment_stays_blank_and_skips_tts(review_db, monkeypatch):
    sessions, job_id, segment_id, tmp_path = review_db
    api.patch_segments(
        job_id,
        [api.SegmentPatch(id=segment_id, translated_text="", reading_text="", revision=1)],
    )
    api.rerender(job_id, api.RenderRequest(expected_revision=2))
    lease_id = "approved-review-lease"
    with sessions() as session:
        job = session.get(Job, job_id)
        job.status = "RUNNING"
        job.lease_id = lease_id
        job.lease_at = pipeline.now()
        session.commit()

    calls: list[str] = []
    _mock_pipeline_media(monkeypatch, tmp_path, calls)

    pipeline.Pipeline(job_id, lease_id).run()

    with sessions() as session:
        job = session.get(Job, job_id)
        segment = session.get(Segment, segment_id)
        assert job.status == "COMPLETED"
        assert segment.revision == 2
        assert segment.translated_text == ""
        assert segment.reading_text == ""
        assert segment.tts_path is None
    assert "tts:synthesize" not in calls
    assert "render" in calls


def test_pause_winning_before_review_checkpoint_is_not_overwritten(review_db, monkeypatch):
    sessions, job_id, _, tmp_path = review_db
    lease_id = "pause-race-lease"
    with sessions() as session:
        job = session.get(Job, job_id)
        job.status = "RUNNING"
        job.stage = "translation"
        job.lease_id = lease_id
        job.lease_at = pipeline.now()
        session.query(StageRun).filter_by(job_id=job_id, stage="review").delete()
        session.commit()

    calls: list[str] = []
    _mock_pipeline_media(monkeypatch, tmp_path, calls)
    original_ensure_owned = pipeline.Pipeline._ensure_owned
    pause_injected = False

    def inject_pause_after_ownership_check(worker):
        nonlocal pause_injected
        original_ensure_owned(worker)
        if pause_injected:
            return
        with sessions() as session:
            job = session.get(Job, job_id)
            if job.stage == "translation" and job.progress == 58:
                job.status = "PAUSING"
                job.pause_requested = True
                session.commit()
                pause_injected = True

    monkeypatch.setattr(pipeline.Pipeline, "_ensure_owned", inject_pause_after_ownership_check)

    pipeline.Pipeline(job_id, lease_id).run()

    assert pause_injected is True
    with sessions() as session:
        job = session.get(Job, job_id)
        assert job.status in {"PAUSING", "PAUSED"}
        assert job.status != "DRAFT"
        assert session.scalar(select(StageRun).where(
            StageRun.job_id == job_id,
            StageRun.stage == "review",
        )) is None
    assert not any(call.startswith("tts:") or call in {"build_voice", "render"} for call in calls)


def test_watch_dedup_treats_missing_legacy_review_flag_as_false(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'legacy-watch.sqlite3').as_posix()}", future=True)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, future=True)
    source_path = tmp_path / "watched.mp4"
    source_path.write_bytes(b"legacy watched video")
    legacy_mode = api._normalize_preset({})
    legacy_mode.pop("review_before_render")
    with sessions() as session:
        source = Source(
            original_name=source_path.name,
            path=str(source_path),
            sha256=api.sha256_file(source_path),
            size_bytes=source_path.stat().st_size,
        )
        session.add(source); session.flush()
        existing = Job(
            source_id=source.id,
            status="COMPLETED",
            mode_json=json_text(legacy_mode),
            config_json=json_text(legacy_mode),
        )
        session.add(existing); session.commit()
        existing_id = existing.id

    monkeypatch.setattr(api, "SessionLocal", sessions)
    monkeypatch.setattr(api.settings, "sources_root", tmp_path / "managed-sources")

    _source, reused = api.create_source_and_job(source_path, {}, watch_folder_id="watch-folder")

    assert reused.id == existing_id
    with sessions() as session:
        assert len(list(session.scalars(select(Job)))) == 1
    engine.dispose()
