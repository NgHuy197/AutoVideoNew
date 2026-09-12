from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .config import settings
from .media import MediaError, run_command


@dataclass
class ASRSegment:
    start_ms: int
    end_ms: int
    text: str
    confidence: float | None = None
    language: str | None = None


def _ms(value: float | int | None) -> int:
    return round(float(value or 0) * 1000)


def _text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def parse_whisper_json(path: Path, offset_ms: int = 0) -> tuple[list[ASRSegment], str | None]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    raw = data.get("transcription") or data.get("segments") or data.get("results") or []
    if isinstance(raw, dict): raw = raw.get("segments", [])
    language = data.get("language") or (data.get("result") or {}).get("language")
    out: list[ASRSegment] = []
    for item in raw:
        text = _text(item.get("text", ""))
        if not text: continue
        start = item.get("offsets", {}).get("from") if isinstance(item.get("offsets"), dict) else item.get("start")
        end = item.get("offsets", {}).get("to") if isinstance(item.get("offsets"), dict) else item.get("end")
        # whisper.cpp offsets are milliseconds, while start/end from other JSON formats are seconds.
        if isinstance(item.get("offsets"), dict):
            start_ms, end_ms = int(start or 0) + offset_ms, int(end or 0) + offset_ms
        else:
            start_ms, end_ms = _ms(start) + offset_ms, _ms(end) + offset_ms
        if end_ms <= start_ms: end_ms = start_ms + 100
        out.append(ASRSegment(start_ms, end_ms, text, item.get("confidence"), language))
    return _merge_overlap(out), language


def _merge_overlap(items: list[ASRSegment]) -> list[ASRSegment]:
    result: list[ASRSegment] = []
    for item in sorted(items, key=lambda x: (x.start_ms, x.end_ms)):
        if result and item.start_ms < result[-1].end_ms:
            previous = result[-1]
            if item.text == previous.text or item.start_ms < previous.start_ms + (previous.end_ms - previous.start_ms) // 3:
                previous.end_ms = max(previous.end_ms, item.end_ms)
                if item.text not in previous.text and item.text not in previous.text[-len(item.text) * 2:]:
                    previous.text = f"{previous.text} {item.text}".strip()
                continue
        result.append(item)
    return result


def transcribe(audio: Path, output_json: Path, *, language: str | None = None, threads: int | None = None,
               timeout: int = 1200, offset_ms: int = 0) -> tuple[list[ASRSegment], str | None]:
    if not settings.whisper_model: raise MediaError("Whisper model path is not configured")
    output_json.parent.mkdir(parents=True, exist_ok=True)
    args = [str(settings.whisper_cli), "-m", str(settings.whisper_model), "-f", str(audio),
            "-ojf", "-of", str(output_json.with_suffix("")), "-t", str(threads or settings.cpu_threads),
            "--print-progress", "-l", language or "auto"]
    run_command(args, timeout=timeout)
    actual = output_json if output_json.exists() else output_json.with_suffix(".json")
    if not actual.exists():
        raise MediaError("Whisper did not produce JSON output")
    return parse_whisper_json(actual, offset_ms)
