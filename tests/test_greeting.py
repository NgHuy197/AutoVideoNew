from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from backend.app import api, pipeline, renderer
from backend.app.db import Job, Segment
from backend.app.subtitles import caption_cues
from tests.test_script_review import _mock_pipeline_media, review_db  # noqa: F401


def test_greeting_is_absent_from_preset_unless_enabled():
    assert "greeting_enabled" not in api._normalize_preset({})
    disabled = api._normalize_preset({"greeting_enabled": False, "greeting_text": "Hallo"})
    assert "greeting_enabled" not in disabled and "greeting_text" not in disabled


def test_enabled_greeting_gets_default_and_trimmed_text():
    assert api._normalize_preset({"greeting_enabled": True})["greeting_text"] == "Hallo allemaal, welkom!"
    assert api._normalize_preset({"greeting_enabled": True, "greeting_text": "  Dag!  "})["greeting_text"] == "Dag!"


@pytest.mark.parametrize("preset", [
    {"greeting_enabled": "yes"},
    {"greeting_enabled": True, "greeting_text": "   "},
    {"greeting_enabled": True, "greeting_text": "x" * 201},
    {"greeting_enabled": True, "greeting_text": "Hallo {\\b1}"},
    {"greeting_enabled": True, "greeting_text": "Hallo\nwereld"},
])
def test_invalid_greeting_is_rejected(preset):
    with pytest.raises(ValueError):
        api._normalize_preset(preset)


def test_captions_start_with_greeting_and_shift_speech():
    segment = SimpleNamespace(start_ms=1000, end_ms=2000, translated_text="Xin chào")

    greeting, shifted = renderer._with_greeting([segment], "Hallo!", 2500)

    assert caption_cues(greeting) == [(0, 2500, "Hallo!")]
    assert caption_cues(shifted) == [(3500, 4500, "Xin chào")]
    assert segment.start_ms == 1000


@pytest.fixture()
def captured_render(tmp_path, monkeypatch):
    calls: list[list[str]] = []
    verified: list[int] = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        (tmp_path / "out.mp4.partial").write_bytes(b"video")

    monkeypatch.setattr(renderer, "run_command", fake_run)
    monkeypatch.setattr(renderer, "audio_duration", lambda path: 2.0)
    monkeypatch.setattr(renderer, "_verify_output", lambda path, duration, *_: verified.append(duration) or {})
    segment = SimpleNamespace(start_ms=0, end_ms=1000, translated_text="Xin chào", source_text="Hi")

    def render(**options):
        return renderer.render_video(tmp_path / "in.mp4", options.pop("voice", tmp_path / "voice.wav"), [segment], 10_000,
                                     tmp_path / "out.mp4", audio_index=0, **options)
    return render, calls, verified, tmp_path


def _filter_graph(args: list[str]) -> str:
    return args[args.index("-filter_complex") + 1]


def test_render_without_greeting_keeps_timeline(captured_render):
    render, calls, verified, _ = captured_render

    render(subtitle_mode="none")

    assert verified == [10_000]
    assert "-filter_complex" not in calls[0]
    assert "-af" in calls[0]


def test_render_prepends_greeting_and_normalizes_inside_graph(captured_render):
    render, calls, verified, tmp_path = captured_render

    render(subtitle_mode="srt", greeting_audio=tmp_path / "greeting.wav", greeting_text="Hallo!")

    args, graph = calls[0], _filter_graph(calls[0])
    assert verified == [12_500]
    assert "tpad=start_duration=2.500:start_mode=clone" in graph
    assert "[2:a]" in graph and "apad=whole_dur=2.500" in graph
    assert "[greeting][body]concat=n=2:v=0:a=1,loudnorm" in graph
    assert "-af" not in args
    assert args[args.index("-map") + 1] == "[vout]"
    assert (tmp_path / "out.srt").read_text(encoding="utf-8").startswith(
        "1\n00:00:00,000 --> 00:00:02,500\nHallo!\n")
    transcript = json.loads((tmp_path / "out.transcript.json").read_text(encoding="utf-8"))
    assert transcript["timeline_offset_ms"] == 2500
    assert transcript["segments"][0]["start_ms"] == 0
    report = json.loads((tmp_path / "out.report.json").read_text(encoding="utf-8"))
    assert report["duration_ms"] == 12_500 and report["greeting"]["text"] == "Hallo!"


def test_greeting_input_follows_soft_subtitles_and_delays_original_track(captured_render):
    render, calls, _, tmp_path = captured_render

    render(subtitle_mode="soft", audio_mode="mix", include_original_track=True,
           greeting_audio=tmp_path / "greeting.wav", greeting_text="Hallo!")

    args, graph = calls[0], _filter_graph(calls[0])
    assert "[3:a]" in graph
    assert "[aout]aresample" in graph and "loudnorm" not in graph
    assert "[0:a:0]adelay=2500:all=1[original]" in graph
    assert "[original]" in args


def test_greeting_works_without_dubbing(captured_render):
    render, calls, verified, tmp_path = captured_render

    render(voice=None, audio_mode="original", subtitle_mode="burn",
           greeting_audio=tmp_path / "greeting.wav", greeting_text="Hallo!")

    graph = _filter_graph(calls[0])
    assert "[1:a]" in graph and "[0:a:0]aresample" in graph
    assert "[0:v]tpad=start_duration=2.500:start_mode=clone,ass=" in graph
    assert verified == [12_500]


def _approve_with_greeting(sessions, job_id: str, lease_id: str) -> None:
    api.rerender(job_id, api.RenderRequest(expected_revision=1))
    with sessions() as session:
        job = session.get(Job, job_id)
        config = {**job.config, "greeting_enabled": True, "greeting_text": "Hallo allemaal, welkom!"}
        job.config_json = json.dumps(config)
        job.status = "RUNNING"; job.lease_id = lease_id; job.lease_at = pipeline.now()
        session.commit()


def test_pipeline_passes_dutch_greeting_to_render(review_db, monkeypatch):
    sessions, job_id, _, tmp_path = review_db
    voice = tmp_path / "dutch.onnx"; voice.write_bytes(b"model")
    monkeypatch.setenv("VIDEOAUTO_PIPER_GREETING_MODEL", str(voice))
    _approve_with_greeting(sessions, job_id, "greeting-lease")
    calls: list[str] = []
    _mock_pipeline_media(monkeypatch, tmp_path, calls)
    seen: dict = {}
    monkeypatch.setattr(pipeline, "render_video", lambda *_args, **kwargs: seen.update(kwargs) or {})

    pipeline.Pipeline(job_id, "greeting-lease").run()

    assert seen["greeting_text"] == "Hallo allemaal, welkom!"
    assert seen["greeting_audio"].name == "greeting.wav"
    with sessions() as session:
        assert session.get(Job, job_id).status == "COMPLETED"


def test_pipeline_fails_clearly_without_dutch_voice(review_db, monkeypatch):
    sessions, job_id, _, tmp_path = review_db
    monkeypatch.setenv("VIDEOAUTO_PIPER_GREETING_MODEL", str(tmp_path / "missing.onnx"))
    _approve_with_greeting(sessions, job_id, "greeting-lease")
    calls: list[str] = []
    _mock_pipeline_media(monkeypatch, tmp_path, calls)

    pipeline.Pipeline(job_id, "greeting-lease").run()

    assert "render" not in calls
    with sessions() as session:
        job = session.get(Job, job_id)
        assert job.status == "FAILED" and "tiếng Hà Lan" in job.error

def _prepare_short_speech(sessions, job_id: str, *, text: str, end_ms: int) -> None:
    api.rerender(job_id, api.RenderRequest(expected_revision=1))
    with sessions() as session:
        job = session.get(Job, job_id)
        job.status = "RUNNING"; job.lease_id = "dub-lease"; job.lease_at = pipeline.now()
        segment = session.scalars(select(Segment).where(Segment.job_id == job_id)).one()
        segment.reading_text = text; segment.translated_text = text; segment.end_ms = end_ms
        session.commit()


def _media_seconds(monkeypatch, seconds: float) -> None:
    monkeypatch.setattr(pipeline, "validate_video", lambda _path: {
        "duration": seconds, "audio": [{"start_time": 0}], "video": {"width": 1280, "height": 720}})


def test_lone_interjection_keeps_original_audio(review_db, monkeypatch):
    sessions, job_id, _, tmp_path = review_db
    _prepare_short_speech(sessions, job_id, text="Ô oh", end_ms=900)
    calls: list[str] = []
    _mock_pipeline_media(monkeypatch, tmp_path, calls)
    _media_seconds(monkeypatch, 43.0)
    seen: dict = {}
    monkeypatch.setattr(pipeline, "render_video", lambda *_args, **kwargs: seen.update(kwargs) or {})

    pipeline.Pipeline(job_id, "dub-lease").run()

    assert seen["audio_mode"] == "original"
    assert "build_voice" not in calls and "tts:synthesize" not in calls


def test_short_clip_of_real_speech_is_still_dubbed(review_db, monkeypatch):
    sessions, job_id, _, tmp_path = review_db
    _prepare_short_speech(sessions, job_id, text="Xin chào", end_ms=900)
    calls: list[str] = []
    _mock_pipeline_media(monkeypatch, tmp_path, calls)
    seen: dict = {}
    monkeypatch.setattr(pipeline, "render_video", lambda *_args, **kwargs: seen.update(kwargs) or {})

    pipeline.Pipeline(job_id, "dub-lease").run()

    assert seen["audio_mode"] == "replace" and "build_voice" in calls
