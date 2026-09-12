from __future__ import annotations

from pathlib import Path


def stamp(ms: int) -> str:
    hours, remain = divmod(max(0, ms), 3_600_000)
    minutes, remain = divmod(remain, 60_000)
    seconds, millis = divmod(remain, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def ass_stamp(ms: int) -> str:
    hours, remain = divmod(max(0, ms), 3_600_000)
    minutes, remain = divmod(remain, 60_000)
    seconds, millis = divmod(remain, 1_000)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{millis // 10:02d}"


def wrap_caption(text: str, max_chars: int = 42) -> str:
    words = text.split(); lines: list[str] = []; current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > max_chars and current:
            lines.append(current); current = word
        else: current = candidate
    if current: lines.append(current)
    return "\n".join(lines) if lines else ""


def caption_cues(segment, max_chars: int = 42, max_lines: int = 2) -> list[tuple[int, int, str]]:
    """Split a segment into readable cues while preserving every source token."""
    words = str(getattr(segment, "translated_text", "") or getattr(segment, "text", "")).split()
    if not words: return []
    groups: list[str] = []; lines: list[str] = []; line = ""
    for word in words:
        if len(word) > max_chars:
            if line: lines.append(line); line = ""
            if lines: groups.append("\n".join(lines)); lines = []
            groups.append(word); continue
        candidate = f"{line} {word}".strip()
        if len(candidate) <= max_chars:
            line = candidate; continue
        if line: lines.append(line); line = word
        if len(lines) == max_lines:
            groups.append("\n".join(lines)); lines = []
    if line: lines.append(line)
    if lines: groups.append("\n".join(lines))
    start, end = int(segment.start_ms), max(int(segment.end_ms), int(segment.start_ms) + 100)
    weights = [max(1, len(group.replace(" ", "").replace("\n", ""))) for group in groups]; total = sum(weights)
    cues: list[tuple[int, int, str]] = []; cursor = start
    for index, (group, weight) in enumerate(zip(groups, weights)):
        cue_end = end if index == len(groups) - 1 else min(end, cursor + max(100, round((end - start) * weight / total)))
        cues.append((cursor, cue_end, group)); cursor = cue_end
    return cues


def write_srt(segments: list, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    index = 0
    for segment in segments:
        for start, end, text in caption_cues(segment):
            index += 1; lines += [str(index), f"{stamp(start)} --> {stamp(end)}", text, ""]
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return path


def write_ass(segments: list, path: Path, width: int = 1920, height: int = 1080) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Noto Sans,{max(18, round(height * 0.055))},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,3,1,2,40,40,{max(20, round(height * 0.045))},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    rows: list[str] = []
    for segment in segments:
        for start, end, text in caption_cues(segment):
            text = text.replace("\n", "\\N")
            rows.append(f"Dialogue: 0,{ass_stamp(start)},{ass_stamp(end)},Default,,0,0,0,,{text}")
    path.write_text(header + "\n".join(rows) + "\n", encoding="utf-8", newline="\n")
    return path
