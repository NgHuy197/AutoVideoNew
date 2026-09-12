from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

from .config import settings
from .media import MediaError, audio_duration, run_command, sha256_file


def tempo_chain(factor: float) -> str:
    """Return an ``atempo`` chain with each filter within FFmpeg's bounds."""
    if factor <= 0: raise ValueError("tempo must be positive")
    filters: list[str] = []
    value = factor
    while value > 2.0:
        filters.append("atempo=2.0"); value /= 2.0
    while value < 0.5:
        filters.append("atempo=0.5"); value /= 0.5
    filters.append(f"atempo={value:.8f}")
    return ",".join(filters)


def _content_hash(path: Path) -> str:
    # This is called once per PiperSynthesizer/job, never once per sentence.
    # Reading the bytes avoids a same-size/same-timestamp replacement being
    # mistaken for the old model on filesystems with coarse mtime precision.
    return sha256_file(path)


def _cache_key(text: str, model_digest: str, config_digest: str) -> str:
    digest = hashlib.sha256()
    digest.update(b"videoauto-tts-v2\0")
    for value in (text, model_digest, config_digest):
        digest.update(value.encode("utf-8")); digest.update(b"\0")
    return digest.hexdigest()


def _valid_wav(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 44 and audio_duration(path) > 0.001
    except (OSError, MediaError, ValueError):
        return False


def _unique_partial(path: Path) -> Path:
    """Return a process/thread-unique sibling used for atomic publication."""
    import threading
    return path.with_name(f".{path.name}.{os.getpid()}-{threading.get_ident()}.partial")


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = _unique_partial(destination)
    try:
        shutil.copy2(source, partial)
        os.replace(partial, destination)
    finally:
        partial.unlink(missing_ok=True)


class PiperSynthesizer:
    def __init__(self, model_path: Path, config_path: Path | None = None, *, threads: int | None = None):
        if not model_path.exists(): raise MediaError(f"Piper model not found: {model_path}")
        self.model_path = model_path.resolve()
        sidecar = self.model_path.with_name(self.model_path.name + ".json")
        self.config_path = (config_path or sidecar).resolve() if (config_path or sidecar).exists() else None
        self.threads = max(1, int(threads or settings.cpu_threads))
        # The content versions form the cache identity and are computed once
        # when a job creates its synthesizer, rather than once per sentence.
        self.model_digest = _content_hash(self.model_path)
        self.config_digest = _content_hash(self.config_path) if self.config_path else "missing-config"
        self._voice = None

    def _load_voice(self):
        if self._voice is not None: return self._voice
        try:
            from piper.voice import PiperConfig, PiperVoice
            import onnxruntime
        except ImportError as exc:
            raise MediaError("piper-tts and onnxruntime are not installed") from exc
        if self.config_path is None:
            raise MediaError(f"Piper model config not found beside {self.model_path}")
        try:
            config = PiperConfig.from_dict(json.loads(self.config_path.read_text(encoding="utf-8")))
            options = onnxruntime.SessionOptions()
            options.intra_op_num_threads = self.threads
            options.inter_op_num_threads = 1
            options.execution_mode = onnxruntime.ExecutionMode.ORT_SEQUENTIAL
            session = onnxruntime.InferenceSession(str(self.model_path), sess_options=options,
                                                    providers=["CPUExecutionProvider"])
            self._voice = PiperVoice(session=session, config=config)
            return self._voice
        except Exception as exc:
            raise MediaError(f"cannot load Piper voice locally: {exc}") from exc

    def synthesize(self, text: str, output: Path, *, cache_root: Path | None = None) -> tuple[Path, int]:
        if not text.strip(): raise MediaError("cannot synthesize empty text")
        digest = _cache_key(text, self.model_digest, self.config_digest)
        cached = (cache_root / "tts" / f"{digest}.wav") if cache_root else None
        if cached is not None:
            if cached.exists() and _valid_wav(cached):
                output.parent.mkdir(parents=True, exist_ok=True)
                if cached.resolve() != output.resolve(): _atomic_copy(cached, output)
                if not _valid_wav(output): raise MediaError("cached TTS audio failed validation")
                return output, round(audio_duration(output) * 1000)
            if cached.exists(): cached.unlink(missing_ok=True)
        output.parent.mkdir(parents=True, exist_ok=True)
        partial = _unique_partial(output)
        try:
            import wave
            with wave.open(str(partial), "wb") as handle:
                self._load_voice().synthesize_wav(text, handle)
            if not _valid_wav(partial): raise MediaError("Piper produced invalid audio")
            os.replace(partial, output)
            if cached is not None:
                cached.parent.mkdir(parents=True, exist_ok=True)
                cache_partial = _unique_partial(cached)
                try:
                    shutil.copy2(output, cache_partial)
                    os.replace(cache_partial, cached)
                except OSError:
                    cache_partial.unlink(missing_ok=True)
            return output, round(audio_duration(output) * 1000)
        except MediaError:
            partial.unlink(missing_ok=True)
            raise
        except Exception as exc:
            partial.unlink(missing_ok=True)
            raise MediaError(f"Piper synthesis failed: {exc}") from exc


def _ffmpeg_audio_filter(source: Path, destination: Path, filter_expr: str) -> None:
    partial = _unique_partial(destination)
    try:
        run_command([str(settings.ffmpeg), "-y", "-i", str(source), "-af", filter_expr,
                     "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", "-f", "wav", str(partial)], timeout=300)
        if not _valid_wav(partial): raise MediaError("FFmpeg produced invalid fitted audio")
        os.replace(partial, destination)
    finally:
        partial.unlink(missing_ok=True)


def _tempo_warnings(tempo: float) -> list[str]:
    """Describe the tempo that was actually applied by the final pass."""
    warnings: list[str] = []
    if tempo > 1.5:
        warnings.append(f"TTS accelerated {tempo:.2f}x")
    if tempo > 2.0:
        warnings.append(f"TTS acceleration exceeds 2x ({tempo:.2f}x)")
    return warnings


def fit_to_slot(source: Path, destination: Path, slot_ms: int) -> tuple[Path, int, float, list[str]]:
    """Fit a complete utterance into a slot without trimming spoken audio.

    Oversize audio is first tempo-compressed and measured. If FFmpeg rounding
    still puts the result over the slot, the tempo is increased and the whole
    utterance is rendered again. Padding/``atrim`` is applied only after the
    complete transformed speech is known to fit, so any trim can remove only
    generated silence.
    """
    actual_ms = round(audio_duration(source) * 1000)
    if slot_ms <= 0: raise MediaError("invalid audio slot")
    warnings: list[str] = []
    destination.parent.mkdir(parents=True, exist_ok=True)
    if actual_ms <= slot_ms:
        pad = max(0, slot_ms - actual_ms)
        filt = f"apad=pad_dur={pad / 1000:.6f},atrim=duration={slot_ms / 1000:.6f}"
        _ffmpeg_audio_filter(source, destination, filt)
        measured_ms = round(audio_duration(destination) * 1000)
        if abs(measured_ms - slot_ms) > 25: raise MediaError(f"TTS slot duration mismatch ({measured_ms}ms vs {slot_ms}ms)")
        return destination, actual_ms, 1.0, warnings

    tempo = actual_ms / slot_ms
    transformed = destination.with_name(destination.stem + ".tempo.wav.partial")
    transformed.unlink(missing_ok=True)
    try:
        measured_seconds = 0.0
        for _ in range(4):
            _ffmpeg_audio_filter(source, transformed, tempo_chain(tempo))
            measured_seconds = audio_duration(transformed)
            if measured_seconds <= slot_ms / 1000:
                break
            # Leave a small sample-sized headroom on the next pass instead of
            # accepting an oversize result and trimming spoken samples.
            tempo *= (measured_seconds / (slot_ms / 1000)) * 1.0005
        if measured_seconds > slot_ms / 1000:
            measured_ms = round(measured_seconds * 1000)
            raise MediaError(f"tempo fitting could not preserve full audio ({measured_ms}ms vs {slot_ms}ms)")
        pad = max(0, slot_ms - round(measured_seconds * 1000))
        if pad:
            # The input is already guaranteed to contain the complete
            # transformed utterance. Any atrim here removes only generated
            # padding after the speech.
            _ffmpeg_audio_filter(transformed, destination,
                                 f"apad=pad_dur={pad / 1000:.6f},atrim=duration={slot_ms / 1000:.6f}")
        else:
            os.replace(transformed, destination)
        final_ms = round(audio_duration(destination) * 1000)
        if abs(final_ms - slot_ms) > 25: raise MediaError(f"TTS slot duration mismatch ({final_ms}ms vs {slot_ms}ms)")
        # ``tempo`` may have been increased by one or more convergence passes.
        # Emit thresholds only after that final value is known, so persisted
        # warnings describe the tempo returned to the pipeline.
        warnings = _tempo_warnings(tempo)
        return destination, actual_ms, tempo, warnings
    finally:
        transformed.unlink(missing_ok=True)
