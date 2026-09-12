from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import settings
from .media import MediaError, audio_duration, ffprobe, run_command, sha256_file
from .subtitles import write_ass, write_srt


def _escape_filter_path(path: Path) -> str:
    return str(path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def build_voice_track(segments: list, duration_ms: int, destination: Path) -> Path:
    """Concatenate fixed-length TTS slots using a concat manifest.

    A filter graph with one ``-i`` per sentence exceeds Windows' command-line limit
    on long clips. A UTF-8 concat manifest keeps argv bounded and lets FFmpeg stream
    the pieces directly from disk.
    """
    pieces: list[Path] = []; silence_cache: dict[int, Path] = {}; cursor = 0
    for segment in segments:
        if not getattr(segment, "tts_path", None): continue
        start = max(cursor, int(segment.start_ms)); gap = start - cursor
        if gap > 0:
            silence = silence_cache.get(gap)
            if silence is None:
                silence = destination.parent / "silence" / f"{gap}.wav"; silence.parent.mkdir(parents=True, exist_ok=True)
                if not silence.exists():
                    run_command([str(settings.ffmpeg), "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", f"{gap / 1000:.6f}", "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", "-f", "wav", str(silence)], timeout=120)
                silence_cache[gap] = silence
            pieces.append(silence)
        pieces.append(Path(segment.tts_path)); cursor = max(cursor, int(segment.end_ms))
    tail = max(0, duration_ms - cursor)
    if tail > 0:
        silence = silence_cache.get(tail)
        if silence is None:
            silence = destination.parent / "silence" / f"{tail}.wav"; silence.parent.mkdir(parents=True, exist_ok=True)
            if not silence.exists(): run_command([str(settings.ffmpeg), "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", f"{tail / 1000:.6f}", "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", "-f", "wav", str(silence)], timeout=120)
            silence_cache[tail] = silence
        pieces.append(silence)
    if not pieces: raise MediaError("no TTS segments available")
    destination.parent.mkdir(parents=True, exist_ok=True); partial = destination.with_suffix(destination.suffix + ".partial"); manifest = destination.with_suffix(".concat.txt.partial")
    if partial.exists(): partial.unlink()
    def concat_line(path: Path) -> str:
        # concat demuxer uses single-quoted POSIX paths even on Windows.
        return "file '" + path.resolve().as_posix().replace("'", "'\\''") + "'\n"
    manifest.write_text("".join(concat_line(piece) for piece in pieces), encoding="utf-8", newline="\n")
    run_command([str(settings.ffmpeg), "-y", "-f", "concat", "-safe", "0", "-i", str(manifest), "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", "-f", "wav", str(partial)], timeout=900)
    if not partial.exists(): raise MediaError("voice track was not created")
    measured = round(audio_duration(partial) * 1000)
    if abs(measured - duration_ms) > 40: raise MediaError(f"voice track duration mismatch ({measured}ms vs {duration_ms}ms)")
    partial.replace(destination); return destination


def _verify_output(path: Path, duration_ms: int, expect_subtitle: bool = False,
                   expect_original_track: bool = False) -> dict:
    info = ffprobe(path); streams = info.get("streams", [])
    if not any(x.get("codec_type") == "video" for x in streams): raise MediaError("render has no video")
    audio_streams = [x for x in streams if x.get("codec_type") == "audio"]
    if not audio_streams: raise MediaError("render has no audio")
    if expect_original_track and len(audio_streams) < 2:
        raise MediaError("requested original secondary audio track is missing")
    if expect_subtitle and not any(x.get("codec_type") == "subtitle" for x in streams): raise MediaError("requested soft subtitle track is missing")
    duration = float((info.get("format") or {}).get("duration") or 0)
    video_stream = next(x for x in streams if x.get("codec_type") == "video")
    fps = 30.0
    try:
        numerator, denominator = str(video_stream.get("avg_frame_rate", "30/1")).split("/"); fps = float(numerator) / max(float(denominator), 1)
    except (ValueError, ZeroDivisionError): pass
    tolerance_ms = max(100.0, 2000.0 / max(fps, 1.0))
    if abs(duration * 1000 - duration_ms) > tolerance_ms: raise MediaError(f"render duration mismatch ({duration:.3f}s, tolerance {tolerance_ms:.0f}ms)")
    # A successful mux does not guarantee every packet decodes. Run a full decode pass
    # before publication so corrupt output never becomes a valid Artifact.
    # FFmpeg can report a late packet decode error on stderr while still
    # returning exit code 0. ``-xerror`` turns every decode error into a hard
    # failure, so corrupt media is never published as a valid Artifact.
    run_command([str(settings.ffmpeg), "-v", "error", "-xerror", "-err_detect", "explode", "-i", str(path),
                 "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"], timeout=600)
    return {"duration": duration, "streams": streams, "audio_stream_count": len(audio_streams)}


def _write_json_atomic(path: Path, value: dict) -> Path:
    """Write a UTF-8 JSON sidecar with a same-directory atomic replacement."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".partial")
    partial.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    partial.replace(path)
    return path


def _transcript_payload(segments: list, duration_ms: int) -> dict:
    rows = []
    for ordinal, segment in enumerate(segments):
        rows.append({
            "id": getattr(segment, "id", None),
            "ordinal": int(getattr(segment, "ordinal", ordinal)),
            "start_ms": int(getattr(segment, "start_ms", 0)),
            "end_ms": int(getattr(segment, "end_ms", 0)),
            "source_text": str(getattr(segment, "source_text", "") or ""),
            "translated_text": str(getattr(segment, "translated_text", "") or ""),
            "reading_text": str(getattr(segment, "reading_text", "") or ""),
            "confidence": getattr(segment, "confidence", None),
            "tts_duration_ms": getattr(segment, "tts_duration_ms", None),
            "tempo": getattr(segment, "tempo", None),
            "warnings": list(getattr(segment, "warnings", []) or []),
        })
    return {"schema_version": 1, "duration_ms": int(duration_ms), "segments": rows}


def render_video(source: Path, voice_track: Path | None, segments: list, duration_ms: int, destination: Path,
                 *, audio_mode: str = "replace", subtitle_mode: str = "burn", audio_index: int = 0,
                 width: int = 1920, height: int = 1080,
                 include_original_track: bool = False) -> dict:
    destination.parent.mkdir(parents=True, exist_ok=True)
    srt_path = destination.with_suffix(".srt")
    # The ASS filter parses its filename as filter syntax.  Keep that token a
    # safe basename and run FFmpeg from the output directory so user names such
    # as ``O'Brien.mp4`` cannot terminate the quoted filter expression.
    ass_path = destination.parent / f".videoauto-{uuid.uuid4().hex}.ass"
    if subtitle_mode in {"srt", "soft"}: write_srt(segments, srt_path)
    if subtitle_mode == "burn": write_ass(segments, ass_path, width, height)
    partial = destination.with_suffix(destination.suffix + ".partial")
    if partial.exists(): partial.unlink()
    args = [str(settings.ffmpeg), "-y", "-i", str(source)]
    if voice_track: args += ["-i", str(voice_track)]
    subtitle_input = None
    if subtitle_mode == "soft":
        args += ["-i", str(srt_path)]
        subtitle_input = 2 if voice_track else 1
    filters: list[str] = []
    if voice_track and audio_mode == "mix":
        filters += [f"[0:a:{audio_index}]volume=0.125[orig]", "[1:a]volume=1.0[voice]", "[orig][voice]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.95[aout]"]
        audio_map = "[aout]"
    elif voice_track:
        audio_map = "1:a"
    else: audio_map = f"0:a:{audio_index}"
    video_map = "0:v"
    if subtitle_mode == "burn":
        ass_value = f"'{ass_path.name}'"
        if settings.font_dir and settings.font_dir.is_dir() and "'" not in str(settings.font_dir): ass_value += f":fontsdir='{_escape_filter_path(settings.font_dir)}'"
        filters.append(f"[0:v]ass={ass_value}[vout]"); video_map = "[vout]"
    if filters: args += ["-filter_complex", ";".join(filters)]
    args += ["-map", video_map, "-map", audio_map]
    # Keep the dubbed/mixed result as the first (default) track and optionally
    # expose the selected source audio as a second selectable track. A
    # subtitle-only/original-audio render already has the source once, so the
    # option is meaningful only when a voice track exists.
    include_original = bool(include_original_track and voice_track)
    if include_original:
        args += ["-map", f"0:a:{audio_index}"]
    if subtitle_mode == "soft":
        args += ["-map", f"{subtitle_input}:0", "-c:s", "mov_text", "-metadata:s:s:0", "language=vie"]
    if include_original:
        args += ["-metadata:s:a:0", "title=Vietnamese dubbing", "-metadata:s:a:1", "title=Original audio",
                 "-disposition:a:0", "default", "-disposition:a:1", "0"]
    args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", "-f", "mp4", str(partial)]
    if voice_track and audio_mode != "mix": args[-1:-1] = ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"]
    run_command(args, timeout=1800, cwd=destination.parent)
    if not partial.exists(): raise MediaError("render output missing")
    verification = _verify_output(partial, duration_ms, subtitle_mode == "soft", include_original)
    partial.replace(destination)
    if subtitle_mode == "burn": ass_path.unlink(missing_ok=True)
    transcript_path = destination.with_suffix(".transcript.json")
    report_path = destination.with_suffix(".report.json")
    _write_json_atomic(transcript_path, _transcript_payload(segments, duration_ms))
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": str(source),
        "output": str(destination),
        "duration_ms": int(duration_ms),
        "audio_mode": audio_mode,
        "subtitle_mode": subtitle_mode,
        "include_original_track": include_original,
        "segment_count": len(segments),
        "verification": verification,
    }
    _write_json_atomic(report_path, report)
    return {"video": destination, "srt": srt_path if subtitle_mode in {"srt", "soft"} else None,
            "transcript": transcript_path, "report": report_path,
            "ass": ass_path if subtitle_mode == "burn" else None, "sha256": sha256_file(destination),
            "size_bytes": destination.stat().st_size, "verification": verification}
