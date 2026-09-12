from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app import api
from backend.app.config import settings
from backend.app.db import Artifact, Base, Job, Source, Upload
from backend.app.renderer import _transcript_payload, _write_json_atomic, render_video


@pytest.fixture()
def product_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'product.sqlite3').as_posix()}", future=True)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, future=True)
    with sessions() as session:
        source = Source(original_name="first-name.mp4", path=str(tmp_path / "source.mp4"), sha256="1" * 64, size_bytes=1)
        session.add(source); session.flush()
        job = Job(source_id=source.id, original_name="second-name.mp4", display_name="second-name.mp4",
                  status="QUEUED", mode_json=json.dumps({"audio_mode": "replace", "subtitle_mode": "burn"}),
                  settings_snapshot_json=json.dumps({"paths": {"ffmpeg": "old"}, "resources": {"cpu_threads": 6}}))
        session.add(job); session.commit()
        source_id, job_id = source.id, job.id
    monkeypatch.setattr(api, "SessionLocal", sessions)
    yield sessions, source_id, job_id, tmp_path
    engine.dispose()


def test_job_dict_exposes_job_filename_and_immutable_snapshot(product_db):
    sessions, _, job_id, _ = product_db
    with sessions() as session:
        value = api._job_dict(session, session.get(Job, job_id))
    assert value["status"] == "QUEUED"
    assert value["filename"] == "second-name.mp4"
    assert value["original_name"] == "second-name.mp4"
    assert value["settings_snapshot"]["paths"]["ffmpeg"] == "old"


def test_content_addressed_source_keeps_distinct_job_names(tmp_path, product_db, monkeypatch):
    sessions, _, _, _ = product_db
    first = tmp_path / "A.mp4"; second = tmp_path / "B.mp4"
    first.write_bytes(b"same-content"); second.write_bytes(first.read_bytes())
    old_values = {name: getattr(settings, name) for name in
                  ("data_root", "output_root", "sources_root", "jobs_root", "inbox_root", "cache_root",
                   "logs_root", "database_root", "config_root", "backups_root", "tools_root")}
    data_root = tmp_path / "managed"; output_root = tmp_path / "exports"
    settings.data_root = data_root; settings.output_root = output_root
    settings.sources_root = data_root / "sources"; settings.jobs_root = data_root / "jobs"
    settings.inbox_root = data_root / "inbox"; settings.cache_root = data_root / "cache"
    settings.logs_root = data_root / "logs"; settings.database_root = data_root / "database"
    settings.config_root = data_root / "config"; settings.backups_root = data_root / "backups"
    settings.tools_root = data_root / "tools"; settings.ensure_dirs()
    try:
        first_source, first_job = api.create_source_and_job(first, {"subtitle_mode": "burn"}, original_name="A.mp4")
        second_source, second_job = api.create_source_and_job(second, {"subtitle_mode": "burn"}, original_name="B.mp4")
        assert first_source.id == second_source.id
        assert first_job.display_name == "A.mp4" and second_job.display_name == "B.mp4"
    finally:
        for name, value in old_values.items(): setattr(settings, name, value)


def test_preset_crud_normalizes_original_track_and_priority(product_db):
    sessions, source_id, job_id, _ = product_db
    created = api.create_preset(api.PresetCreate(name="Dub", config={"include_original_audio": True}), None)
    assert created["config"]["include_original_track"] is True
    listed = api.list_presets()
    assert [item["name"] for item in listed] == ["Dub"]
    updated = api.patch_preset(created["id"], api.PresetPatch(config={"subtitle_mode": "srt"}), None)
    assert updated["config"]["subtitle_mode"] == "srt"
    result = api.set_job_priority(job_id, api.PriorityPatch(priority=9), None)
    assert result["priority"] == 9
    with sessions() as session:
        assert session.get(Job, job_id).priority == 9


def test_completed_upload_rejects_conflicting_retry_preset(product_db):
    sessions, _, job_id, _ = product_db
    with sessions() as session:
        upload = Upload(filename="clip.mp4", temp_path="missing", total_bytes=1,
                        status="COMPLETED", completed_source_id="source", completed_job_id=job_id,
                        preset_json=json.dumps({"audio_mode": "replace", "subtitle_mode": "burn",
                                                "include_dubbing": True, "include_original_track": False,
                                                "audio_index": None, "source_language": None, "priority": 0}))
        session.add(upload); session.commit(); upload_id = upload.id
    with pytest.raises(api.HTTPException) as error:
        api.complete_upload(upload_id, {"preset": {"subtitle_mode": "srt"}}, None)
    assert error.value.status_code == 409


def test_priority_rejects_running_job(product_db):
    sessions, _, job_id, _ = product_db
    with sessions() as session:
        session.get(Job, job_id).status = "RUNNING"
        session.commit()
    with pytest.raises(api.HTTPException) as error:
        api.set_job_priority(job_id, api.PriorityPatch(priority=1), None)
    assert error.value.status_code == 409


def test_managed_path_change_is_blocked_for_pausing_job(product_db, monkeypatch):
    sessions, _, job_id, tmp_path = product_db
    with sessions() as session:
        session.get(Job, job_id).status = "PAUSING"
        session.commit()
    with pytest.raises(api.HTTPException) as error:
        api.patch_settings(api.SettingsPatch(output_root=str(tmp_path / "new-output")), None)
    assert error.value.status_code == 409


def test_transcript_sidecar_is_machine_readable_and_atomic(tmp_path):
    class Segment:
        id = 7; ordinal = 0; start_ms = 100; end_ms = 900
        source_text = "hello"; translated_text = "xin chào"; reading_text = "xin chào"
        confidence = 0.9; tts_duration_ms = 800; tempo = 1.0; warnings = []

    payload = _transcript_payload([Segment()], 1000)
    path = _write_json_atomic(tmp_path / "clip.transcript.json", payload)
    assert json.loads(path.read_text(encoding="utf-8"))["segments"][0]["translated_text"] == "xin chào"
    assert not path.with_suffix(path.suffix + ".partial").exists()


def test_render_maps_optional_original_audio_as_second_track(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"; source.write_bytes(b"source")
    voice = tmp_path / "voice.wav"; voice.write_bytes(b"voice")
    destination = tmp_path / "render.mp4"
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if "-f" in args and args[args.index("-f") + 1] == "mp4":
            Path(args[-1]).write_bytes(b"rendered")

    monkeypatch.setattr("backend.app.renderer.run_command", fake_run)
    monkeypatch.setattr("backend.app.renderer.ffprobe", lambda path: {
        "format": {"duration": "1.0"},
        "streams": [{"codec_type": "video", "avg_frame_rate": "30/1"},
                    {"codec_type": "audio"}, {"codec_type": "audio"}],
    })
    result = render_video(source, voice, [], 1000, destination, subtitle_mode="none",
                          include_original_track=True)
    render_call = next(call for call in calls if "-f" in call and call[call.index("-f") + 1] == "mp4")
    assert render_call.count("-map") == 3
    assert "0:a:0" in render_call
    assert result["transcript"].is_file() and result["report"].is_file()


def test_artifact_with_historical_trust_root_remains_downloadable(product_db):
    sessions, _, job_id, tmp_path = product_db
    old_root = tmp_path / "old-output"; old_root.mkdir()
    media = old_root / "clip.mp4"; media.write_bytes(b"media")
    with sessions() as session:
        artifact = Artifact(job_id=job_id, kind="video", path=str(media), trusted_root=str(old_root), valid=True,
                            size_bytes=media.stat().st_size)
        session.add(artifact); session.commit(); artifact_id = artifact.id
    # The current root is deliberately unrelated.  download_artifact should
    # use the artifact's stored trust root after the setting changes.
    original_root = settings.output_root
    settings.output_root = (tmp_path / "new-output").resolve()
    try:
        from starlette.requests import Request
        request = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""})
        response = api.download_artifact(artifact_id, request)
        assert Path(response.path) == media.resolve()
    finally:
        settings.output_root = original_root
