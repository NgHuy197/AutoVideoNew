from __future__ import annotations

from pathlib import Path

from .subtitle_style import normalize_subtitle_style


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


def write_ass(segments: list, path: Path, width: int = 1920, height: int = 1080,
              style: dict | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    options = normalize_subtitle_style(style)
    font_size = max(1, round(height * options["font_size"] / 100))
    margin_v = round(height * options["margin_v"] / 100)
    margin_h = round(width * options["margin_h"] / 100)
    outline = options["outline_width"] * height / 1080
    shadow = options["shadow"] * height / 1080
    # Keep the previous low-resolution defaults for legacy jobs.
    if style is None:
        font_size = max(18, font_size)
        margin_v = max(20, margin_v)
        margin_h = 40
        outline, shadow = 3, 1
    def ass_color(color: str) -> str:
        return f"&H00{color[5:7]}{color[3:5]}{color[1:3]}"
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{options['font_family']},{font_size},{ass_color(options['color'])},&H000000FF,{ass_color(options['outline_color'])},&H80000000,{-1 if options['bold'] else 0},{-1 if options['italic'] else 0},0,0,100,100,0,0,1,{outline:g},{shadow:g},{options['alignment']},{margin_h},{margin_h},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    rows: list[str] = []
    for segment in segments:
        for start, end, text in caption_cues(segment, options["max_chars"], options["max_lines"]):
            text = text.replace("\n", "\\N")
            rows.append(f"Dialogue: 0,{ass_stamp(start)},{ass_stamp(end)},Default,,0,0,0,,{text}")
    path.write_text(header + "\n".join(rows) + "\n", encoding="utf-8", newline="\n")
    return path
