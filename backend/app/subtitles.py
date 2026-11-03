from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from .sentences import _BINDS_NEXT, _SENTENCE_END, break_cost
from .subtitle_style import normalize_subtitle_style

MERGE_GAP_MS = 400
MERGE_SPAN_MS = 6000
# What one more line or cue costs against a bad break (see ``break_cost``).
PIECE_COST = 16


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


def _partition(words: list[str], piece_cost, spare: int = 0,
               penalty: float = PIECE_COST) -> tuple[list[list[str]], float]:
    """Cut words into pieces that fit, breaking where reading allows.

    Picks the cut with the gentlest breaks (see ``break_cost``) and pieces of
    similar length, instead of filling each piece greedily and cutting
    wherever the limit falls. ``piece_cost`` returns None for a piece that does
    not fit. ``spare`` also tries that many extra pieces, so a tight fit that
    can only break inside a word may take one more piece instead.
    """
    count = len(words)
    found: list[tuple[float, list[list[str]]]] = []
    for pieces in range(1, count + 1):
        ideal = sum(len(word) + 1 for word in words) / pieces
        # best[k][j]: cheapest cut of words[:j] into k pieces.
        best: list[dict[int, tuple[float, int]]] = [{0: (0.0, 0)}] + [{} for _ in range(pieces)]
        for k in range(1, pieces + 1):
            for j in range(k, count - (pieces - k) + 1):
                options = []
                for i, (cost, _) in best[k - 1].items():
                    extra = piece_cost(words[i:j]) if i < j else None
                    if extra is None:
                        continue
                    length = sum(len(word) + 1 for word in words[i:j])
                    seam = 3 * break_cost(words[j - 1], words[j]) if j < count else 0
                    options.append((cost + extra + seam + ((length - ideal) / max(ideal, 1) * 4) ** 2, i))
                if options:
                    best[k][j] = min(options)
        if count in best[pieces]:
            cuts = [count]
            for k in range(pieces, 0, -1):
                cuts.append(best[k][cuts[-1]][1])
            cuts.reverse()
            found.append((best[pieces][count][0] + penalty * pieces, [words[a:b] for a, b in zip(cuts, cuts[1:])]))
            if len(found) > spare:
                break
    if not found:
        return [[word] for word in words], float(penalty * count)
    cost, best_pieces = min(found, key=lambda item: item[0])
    return best_pieces, cost


def _wrap(words: list[str], max_chars: int, max_lines: int) -> tuple[list[str], float] | None:
    """Wrap words into at most ``max_lines`` lines with its break cost, or None."""
    if len(words) == 1:
        return words[:], 0.0
    if sum(len(word) + 1 for word in words) - 1 > max_chars * max_lines:
        return None
    pieces, cost = _partition(words, lambda piece: 0 if len(piece) == 1 or len(" ".join(piece)) <= max_chars else None,
                                  penalty=0)
    return ([" ".join(piece) for piece in pieces], cost) if len(pieces) <= max_lines else None


def _lines(words: list[str], max_chars: int, max_lines: int) -> list[str] | None:
    """Wrap words into at most ``max_lines`` lines, or None when they do not fit."""
    wrapped = _wrap(words, max_chars, max_lines)
    return wrapped[0] if wrapped else None


def wrap_caption(text: str, max_chars: int = 42) -> str:
    words = text.split()
    return "\n".join(_lines(words, max_chars, len(words)) or []) if words else ""


def _dangles(text: str) -> bool:
    """A cue that plainly stops before its sentence does ("Nhưng", "lợi nhuận của")."""
    words = text.split()
    return not _SENTENCE_END.search(text) and (len(words) <= 2 or words[-1].lower() in _BINDS_NEXT)


def merge_fragments(segments: list, max_chars: int = 42, max_lines: int = 2) -> list:
    """Show a cue that stops mid-sentence together with the cue that follows.

    Whisper cuts cues where the speaker breathes, so a caption may flash
    "Nhưng" alone. Such a fragment shares the screen with the next cue, each on
    its own lines, when both fit. Cues that merely lack punctuation stay apart:
    Chinese transcripts have none, and joining them would run sentences together.
    """
    merged: list = []
    for segment in segments:
        text = str(getattr(segment, "translated_text", "") or getattr(segment, "text", "")).strip()
        if not text:
            continue
        start, end = int(segment.start_ms), int(segment.end_ms)
        lines = _lines(text.split(), max_chars, max_lines) or []
        if merged:
            last = merged[-1]
            if (last.caption_lines and _dangles(last.caption_lines[-1]) and start - last.end_ms <= MERGE_GAP_MS
                    and end - last.start_ms <= MERGE_SPAN_MS and lines
                    and len(last.caption_lines) + len(lines) <= max_lines):
                last.translated_text = f"{last.translated_text} {text}"
                last.caption_lines += lines; last.end_ms = end; last.merged = True
                continue
        merged.append(SimpleNamespace(start_ms=start, end_ms=end, translated_text=text, caption_lines=lines,
                                      merged=False))
    return merged


def caption_cues(segment, max_chars: int = 42, max_lines: int = 2) -> list[tuple[int, int, str]]:
    """Split a segment into readable cues while preserving every source token."""
    words = str(getattr(segment, "translated_text", "") or getattr(segment, "text", "")).split()
    if not words: return []
    lines = getattr(segment, "caption_lines", None)
    if getattr(segment, "merged", False) and lines and len(lines) <= max_lines:
        # A merged fragment keeps each spoken cue on its own line.
        return [(int(segment.start_ms), max(int(segment.end_ms), int(segment.start_ms) + 100), "\n".join(lines))]
    def piece_cost(piece: list[str]) -> float | None:
        wrapped = _wrap(piece, max_chars, max_lines)
        return wrapped[1] if wrapped else None
    pieces, _ = _partition(words, piece_cost, spare=1)
    groups = ["\n".join(_lines(piece, max_chars, max_lines)) for piece in pieces]
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
    for segment in merge_fragments(segments):
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
    for segment in merge_fragments(segments, options["max_chars"], options["max_lines"]):
        for start, end, text in caption_cues(segment, options["max_chars"], options["max_lines"]):
            text = text.replace("\n", "\\N")
            rows.append(f"Dialogue: 0,{ass_stamp(start)},{ass_stamp(end)},Default,,0,0,0,,{text}")
    path.write_text(header + "\n".join(rows) + "\n", encoding="utf-8", newline="\n")
    return path
