from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from backend.app.asr import parse_whisper_json
from backend.app.paths import confined, safe_filename
from backend.app.subtitles import ass_stamp, stamp, wrap_caption
from backend.app.tts import tempo_chain


def test_tempo_chain_supports_large_acceleration_without_invalid_filter():
    expression = tempo_chain(9.5)
    factors = [float(x.split("=")[1]) for x in expression.split(",")]
    assert all(0.5 <= factor <= 2.0 for factor in factors)
    assert 9.49 < __import__("math").prod(factors) < 9.51


def test_subtitles_preserve_long_text_and_use_media_clocks():
    text = "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen"
    caption = wrap_caption(text, 20)
    assert "fifteen" in caption and "\n" in caption
    assert stamp(12_345) == "00:00:12,345"
    assert ass_stamp(12_345) == "0:00:12.34"


def test_whisper_cpp_json_reads_result_language_and_offsets():
    target = Path(".runtime") / "test-whisper-json.json"; target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"result": {"language": "en"}, "transcription": [{"offsets": {"from": 100, "to": 900}, "text": " hello  world "}]}), encoding="utf-8")
    rows, language = parse_whisper_json(target, offset_ms=200)
    assert language == "en"
    assert rows[0].start_ms == 300 and rows[0].end_ms == 1100 and rows[0].text == "hello world"
    target.unlink(missing_ok=True)


def test_paths_are_confined_and_filename_is_sanitized():
    root = (Path(".runtime") / "test-path-root").resolve(); root.mkdir(parents=True, exist_ok=True)
    assert confined(root / "nested" / "x.mp4", root).parent.name == "nested"
    with pytest.raises(ValueError): confined(root.parent / "escape.mp4", root)
    assert safe_filename("..\\video:<bad>.mp4") == "video__bad_.mp4"
