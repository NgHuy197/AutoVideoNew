from __future__ import annotations

import wave
from pathlib import Path

import pytest

from backend.app import tts


def _write_wav(path: Path, milliseconds: int = 100) -> None:
    frames = 48 * milliseconds
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1); handle.setsampwidth(2); handle.setframerate(48_000)
        handle.writeframes(b"\x01\x00" * frames)


def _duration_from_wave(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / handle.getframerate()


class _FakeVoice:
    def synthesize_wav(self, _text: str, handle) -> None:
        handle.setnchannels(1); handle.setsampwidth(2); handle.setframerate(48_000)
        handle.writeframes(b"\x01\x00" * 4_800)


def test_cache_key_includes_full_model_and_config_content(tmp_path):
    model = tmp_path / "voice.onnx"; config = tmp_path / "voice.onnx.json"
    model.write_bytes(b"model-v1"); config.write_text('{"sample_rate": 48000}', encoding="utf-8")
    first = tts.PiperSynthesizer(model, config)
    first_key = tts._cache_key("Xin chào", first.model_digest, first.config_digest)
    model.write_bytes(b"model-v2")
    second = tts.PiperSynthesizer(model, config)
    second_key = tts._cache_key("Xin chào", second.model_digest, second.config_digest)
    assert first_key != second_key
    config.write_text('{"sample_rate": 22050}', encoding="utf-8")
    third = tts.PiperSynthesizer(model, config)
    assert second.config_digest != third.config_digest


def test_cache_hit_is_atomic_and_corrupt_cache_is_regenerated(tmp_path, monkeypatch):
    model = tmp_path / "voice.onnx"; config = tmp_path / "voice.onnx.json"
    model.write_bytes(b"model"); config.write_text("{}", encoding="utf-8")
    # Keep this test independent of a globally configured ffprobe while still
    # exercising the real WAV header/size validation path.
    monkeypatch.setattr(tts, "audio_duration", _duration_from_wave)
    first = tts.PiperSynthesizer(model, config); first._voice = _FakeVoice()
    cache_root = tmp_path / "cache"
    output = tmp_path / "first.wav"
    first.synthesize("Xin chào", output, cache_root=cache_root)
    cached = next((cache_root / "tts").glob("*.wav"))
    assert cached.is_file() and not list((cache_root / "tts").glob("*.partial"))

    class _FailVoice:
        def synthesize_wav(self, *_args):
            raise AssertionError("cache miss unexpectedly loaded Piper")

    hit = tts.PiperSynthesizer(model, config); hit._voice = _FailVoice()
    hit_output = tmp_path / "hit.wav"
    hit.synthesize("Xin chào", hit_output, cache_root=cache_root)
    assert _duration_from_wave(hit_output) > 0

    cached.write_bytes(b"broken")
    repaired = tts.PiperSynthesizer(model, config); repaired._voice = _FakeVoice()
    repaired_output = tmp_path / "repaired.wav"
    repaired.synthesize("Xin chào", repaired_output, cache_root=cache_root)
    assert _duration_from_wave(repaired_output) > 0
    assert not list((cache_root / "tts").glob("*.partial"))


def test_oversize_fit_runs_tempo_without_speech_trimming(tmp_path, monkeypatch):
    source = tmp_path / "source.wav"; destination = tmp_path / "slot.wav"
    _write_wav(source, 2_000)
    expressions: list[str] = []
    durations = iter([2.0, 1.0, 1.0])

    def fake_filter(_source: Path, target: Path, expression: str) -> None:
        expressions.append(expression)
        _write_wav(target, 1_000)

    monkeypatch.setattr(tts, "audio_duration", lambda _path: next(durations))
    monkeypatch.setattr(tts, "_ffmpeg_audio_filter", fake_filter)
    result = tts.fit_to_slot(source, destination, 1_000)
    assert result[2] >= 2.0
    assert all("atrim" not in expression for expression in expressions[:-1]), "speech-bearing tempo pass must never trim"
    if len(expressions) > 1:
        assert "atrim" in expressions[-1], "only generated padding may be trimmed"


def test_tempo_warnings_use_converged_applied_tempo(tmp_path, monkeypatch):
    source = tmp_path / "source.wav"; destination = tmp_path / "slot.wav"
    _write_wav(source, 1_600)
    durations = iter([1.6, 1.4, 1.0, 1.0])

    def fake_filter(_source: Path, target: Path, _expression: str) -> None:
        _write_wav(target, 1_000)

    monkeypatch.setattr(tts, "audio_duration", lambda _path: next(durations))
    monkeypatch.setattr(tts, "_ffmpeg_audio_filter", fake_filter)
    _destination, _raw_ms, tempo, warnings = tts.fit_to_slot(source, destination, 1_000)

    assert tempo == pytest.approx(1.6 * 1.4 * 1.0005)
    assert warnings == [f"TTS accelerated {tempo:.2f}x",
                        f"TTS acceleration exceeds 2x ({tempo:.2f}x)"]
    assert all("1.60x" not in warning for warning in warnings)
