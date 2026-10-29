from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app import api, renderer
from backend.app.db import Base, Job, Source, Upload
from backend.app.pipeline import Pipeline
from backend.app.subtitle_style import normalize_subtitle_style
from backend.app.subtitles import write_ass


@pytest.mark.parametrize("style", [
    {"color": "red"}, {"color": "#FFFFFFFF"}, {"font_family": "Arial,99"},
    {"font_family": "Arial\n[Events]"}, {"font_family": "{Arial}"}, {"font_family": "  "},
    {"font_size": 0}, {"font_size": float("nan")}, {"font_size": float("inf")},
    {"alignment": 10}, {"alignment": True}, {"alignment": 2.5}, {"bold": "false"},
    {"outline_width": -1}, {"shadow": 9}, {"margin_v": 31}, {"margin_h": -1},
    {"max_chars": 9}, {"max_lines": 5}, {"unknown": True}, [], "Arial",
])
def test_reject_invalid_style(style):
    with pytest.raises(ValueError):
        api._normalize_preset({"subtitle_style": style})


def test_style_normalizes_and_legacy_preset_keeps_identity():
    normalized = api._normalize_preset({"subtitle_style": {"color": "#abcdef", "font_family": " Arial "}})
    assert normalized["subtitle_style"]["color"] == "#ABCDEF"
    assert normalized["subtitle_style"]["font_family"] == "Arial"
    assert normalized["subtitle_style"]["font_size"] == 5.5
    assert "subtitle_style" not in api._normalize_preset({})
    assert api._normalize_preset({}) == api._normalize_preset({"subtitle_style": None})


def test_ass_styles_scale_and_preserve_caption_words(tmp_path):
    segment = SimpleNamespace(start_ms=0, end_ms=10000,
                              translated_text="Một đoạn phụ đề tiếng Việt thật dài để kiểm tra chia dòng và giữ đủ nội dung.")
    style = {"font_family": "Arial", "font_size": 8, "color": "#123456",
             "outline_color": "#ABCD01", "outline_width": 4, "shadow": 2,
             "alignment": 9, "margin_v": 10, "margin_h": 5, "bold": True,
             "italic": True, "max_chars": 15, "max_lines": 1}
    for width, height in [(1920, 1080), (1080, 1920), (640, 360)]:
        data = write_ass([segment], tmp_path / f"{width}-{height}.ass", width, height, style).read_text(encoding="utf-8")
        row = next(line for line in data.splitlines() if line.startswith("Style:"))[7:].split(",")
        assert row[1:5] == ["Arial", str(round(height * 0.08)), "&H00563412", "&H000000FF"]
        assert row[5] == "&H0001CDAB"
        assert row[7:9] == ["-1", "-1"]
        assert float(row[16]) == pytest.approx(4 * height / 1080, rel=1e-5)
        assert float(row[17]) == pytest.approx(2 * height / 1080, rel=1e-5)
        assert row[18:22] == ["9", str(round(width * .05)), str(round(width * .05)), str(round(height * .1))]
        captions = [line.split(",", 9)[9] for line in data.splitlines() if line.startswith("Dialogue:")]
        assert len(captions) > 1 and all("\\N" not in caption for caption in captions)
        assert " ".join(captions).split() == segment.translated_text.split()


def test_legacy_ass_retains_original_low_resolution_style(tmp_path):
    data = write_ass([], tmp_path / "legacy.ass", 640, 360).read_text(encoding="utf-8")
    assert "Style: Default,Noto Sans,20," in data
    assert ",1,3,1,2,40,40,20,1" in data


@pytest.fixture
def style_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'style.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(api, "SessionLocal", sessions)
    with sessions() as session:
        source = Source(original_name="clip.mp4", path="clip.mp4", sha256="f" * 64, size_bytes=1)
        session.add(source); session.commit()
        source_id = source.id
    yield sessions, source_id
    engine.dispose()


def test_preset_job_and_pipeline_keep_style_snapshot(style_db):
    sessions, source_id = style_db
    preset = api.create_preset(api.PresetCreate(name="Caption", config={"subtitle_style": {"color": "#00FF00", "alignment": 8}}), None)
    job = api.create_job(api.JobCreate(source_id=source_id, preset_id=preset["id"]), None)
    api.patch_preset(preset["id"], api.PresetPatch(config={"subtitle_style": {"color": "#FF0000"}}), None)
    with sessions() as session:
        saved = session.get(Job, job["id"])
        assert saved.mode["subtitle_style"]["color"] == "#00FF00"
        assert Pipeline(saved.id)._load_config(saved)["subtitle_style"]["alignment"] == 8
    custom = api.create_job(api.JobCreate(source_id=source_id, preset_id=preset["id"], subtitle_style={"font_size": 9}), None)
    assert custom["mode"]["subtitle_style"]["font_size"] == 9
    legacy = api.create_job(api.JobCreate(source_id=source_id, preset_id=preset["id"], subtitle_style=None), None)
    assert "subtitle_style" not in legacy["mode"]


def test_upload_retry_rejects_changed_style(style_db):
    sessions, source_id = style_db
    mode = api._normalize_preset({"subtitle_style": {"color": "#00FF00"}})
    with sessions() as session:
        job = Job(source_id=source_id, mode_json=json.dumps(mode))
        session.add(job); session.flush()
        upload = Upload(filename="clip.mp4", total_bytes=1, temp_path="missing", status="COMPLETED",
                        preset_json=json.dumps(mode), completed_source_id=source_id, completed_job_id=job.id)
        session.add(upload); session.commit(); upload_id = upload.id
    with pytest.raises(api.HTTPException) as error:
        api.complete_upload(upload_id, {"preset": {"subtitle_style": {"color": "#FF0000"}}}, None)
    assert error.value.status_code == 409
    result = api.complete_upload(upload_id, {"preset": {"subtitle_style": {"color": "#00ff00"}}}, None)
    assert result["source_id"] == source_id


def test_renderer_applies_style_and_records_report(tmp_path, monkeypatch):
    captured = []
    def fake_run(args, **kwargs):
        if "-filter_complex" in args:
            path = next(tmp_path.glob(".videoauto-*.ass"))
            captured.append(path.read_text(encoding="utf-8"))
            Path(args[-1]).write_bytes(b"video")
    monkeypatch.setattr(renderer, "run_command", fake_run)
    monkeypatch.setattr(renderer, "_verify_output", lambda *args: {"duration": 1})
    style = {"color": "#123456", "alignment": 7}
    result = renderer.render_video(tmp_path / "source.mp4", None, [], 1000, tmp_path / "out.mp4",
                                   width=640, height=360, subtitle_style=style)
    assert "&H00563412" in captured[0] and ",7,13,13,16,1" in captured[0]
    assert json.loads(result["report"].read_text(encoding="utf-8"))["subtitle_style"] == normalize_subtitle_style(style)
    assert not list(tmp_path.glob(".videoauto-*.ass"))
