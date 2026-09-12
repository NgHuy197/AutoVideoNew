from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterable

from .config import settings


class MediaError(RuntimeError):
    pass


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_command(args: list[str], *, timeout: int | float | None = None, progress: callable | None = None,
                cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run a trusted executable with an argv list; shell execution is deliberately disabled."""
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace",
                                universal_newlines=True, cwd=str(cwd) if cwd else None,
                                shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as exc:
        raise MediaError(f"cannot start {args[0]}: {exc}") from exc
    lines: list[str] = []
    try:
        output, _ = proc.communicate(timeout=timeout)
        lines = output.splitlines(keepends=True)
        if progress:
            for line in lines: progress(line.rstrip("\r\n"))
        code = proc.returncode
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        output, _ = proc.communicate()
        lines = output.splitlines(keepends=True)
        raise MediaError(f"command timed out: {args[0]}") from exc
    output = "".join(lines)
    if code != 0:
        raise MediaError(f"{Path(args[0]).name} exited {code}: {output[-4000:]}")
    return subprocess.CompletedProcess(args, code, output, "")


def ffprobe(path: Path) -> dict:
    result = run_command([str(settings.ffprobe), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)], timeout=120)
    try:
        return json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise MediaError("ffprobe returned invalid JSON") from exc


def validate_video(path: Path) -> dict:
    if not path.is_file():
        raise MediaError("video does not exist")
    if path.suffix.lower() not in {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v"}:
        raise MediaError(f"unsupported video extension: {path.suffix}")
    info = ffprobe(path)
    streams = info.get("streams", [])
    video = next((x for x in streams if x.get("codec_type") == "video"), None)
    audio = [x for x in streams if x.get("codec_type") == "audio"]
    if not video:
        raise MediaError("video stream not found")
    duration = float((info.get("format") or {}).get("duration") or video.get("duration") or 0)
    if duration <= 0:
        raise MediaError("media duration is invalid")
    if duration > settings.max_duration_seconds:
        raise MediaError(f"duration {duration:.1f}s exceeds configured limit")
    color_transfer = str(video.get("color_transfer") or "").lower()
    color_primaries = str(video.get("color_primaries") or "").lower()
    if any(v in {"smpte2084", "arib-std-b67", "bt2020"} for v in (color_transfer, color_primaries)):
        raise MediaError("HDR/BT.2020 source is not supported in this release")
    selected_audio = next((i for i, stream in enumerate(audio) if stream.get("disposition", {}).get("default")), 0)
    return {"duration": duration, "video": video, "audio": audio, "audio_index": selected_audio,
            "streams": streams, "format": info.get("format", {})}


def extract_audio(source: Path, destination: Path, audio_index: int = 0, *, offset_ms: int = 0,
                  duration_seconds: float | None = None, timeout: int = 900) -> Path:
    # When called directly, derive the media timeline from ffprobe so an audio stream
    # that starts late is represented by leading silence. The pipeline passes these
    # values explicitly after its single preflight probe.
    if offset_ms == 0 and duration_seconds is None:
        info = ffprobe(source); streams = [x for x in info.get("streams", []) if x.get("codec_type") == "audio"]
        if streams:
            offset_ms = max(0, round(float(streams[min(audio_index, len(streams) - 1)].get("start_time") or 0) * 1000))
            duration_seconds = float((info.get("format") or {}).get("duration") or 0) or None
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".partial")
    if partial.exists(): partial.unlink()
    args = [str(settings.ffmpeg), "-y", "-i", str(source), "-map", f"0:a:{audio_index}", "-vn"]
    if offset_ms > 0 or duration_seconds is not None:
        filters = []
        if offset_ms > 0: filters.append(f"adelay={offset_ms}:all=1")
        if duration_seconds is not None: filters.append(f"apad,atrim=duration={duration_seconds:.6f}")
        args += ["-af", ",".join(filters)]
    args += ["-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-f", "wav", str(partial)]
    run_command(args, timeout=timeout)
    if not partial.is_file() or partial.stat().st_size == 0:
        raise MediaError("audio extraction produced no output")
    partial.replace(destination)
    return destination


def audio_duration(path: Path) -> float:
    info = ffprobe(path)
    return float((info.get("format") or {}).get("duration") or 0)


def atomic_move(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".partial")
    if partial.exists(): partial.unlink()
    shutil.copy2(source, partial)
    partial.replace(destination)
    return destination


def media_duration_ms(path: Path) -> int:
    return round(audio_duration(path) * 1000)
