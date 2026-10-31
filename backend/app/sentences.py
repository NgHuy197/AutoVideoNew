"""Turn timed speech cues into whole sentences for translation and back."""
from __future__ import annotations

import re

# Whisper writes non-speech as "(scissors snipping)", "[music]" or "♪ ... ♪".
_SOUND_TAG = re.compile(r"\([^()]*\)|\[[^\[\]]*\]|[♪♫]+[^♪♫]*[♪♫]+|[♪♫]+")
_HAS_WORD = re.compile(r"\w")
_TERMINAL = "。！？"
_SENTENCE_END = re.compile(rf"[.!?…{_TERMINAL}][\"'”’)\]]*$")
_BOUNDARY = re.compile(rf"(?<=[.!?…])[\"'”’)\]]*\s+(?=[^a-zà-ỹ\s])|(?<=[{_TERMINAL}])\s*")
_ABBREVIATION = re.compile(r"(?:^|\s)(?:Mr|Mrs|Ms|Dr|Prof|St|Sr|Jr|vs|etc|No)\.$")
_CLAUSE_END = re.compile(r"[,;:.!?…]$")
# Whisper leaves Chinese and Japanese without punctuation, so cues cannot be
# joined into sentences, and a joined run mixes speakers. Keep those cues whole.
_UNSPACED_SCRIPT = re.compile(r"[぀-ヿ㐀-鿿]")
MAX_GROUP_CUES = 8
MAX_GAP_MS = 2000
SNAP_WORDS = 3


def strip_sound_tags(text: str) -> str:
    """Remove non-speech markers; a cue with no words left has nothing to say."""
    cleaned = " ".join(_SOUND_TAG.sub(" ", text or "").split()).removeprefix("- ")
    return cleaned if _HAS_WORD.search(cleaned) else ""


def split_sentences(text: str) -> list[str]:
    sentences: list[str] = []
    current = ""
    for piece in re.split(f"({_BOUNDARY.pattern})", text.strip()):
        if piece is None or not piece.strip():
            continue
        current = f"{current} {piece.strip()}".strip()
        if _SENTENCE_END.search(current) and not _ABBREVIATION.search(current):
            sentences.append(current); current = ""
    if current:
        sentences.append(current)
    return sentences


def group_cues(segments: list) -> list[list]:
    """Join consecutive cues until one ends a sentence.

    Silent gaps end a group too, so a transcript without punctuation cannot
    merge into a single enormous unit. Cues with only sound tags are skipped.
    """
    groups: list[list] = []
    current: list = []
    for segment in segments:
        text = strip_sound_tags(segment.source_text)
        if not text:
            continue
        if current and int(segment.start_ms) - int(current[-1].end_ms) >= MAX_GAP_MS:
            groups.append(current); current = []
        current.append(segment)
        if _SENTENCE_END.search(text) or _UNSPACED_SCRIPT.search(text) or len(current) >= MAX_GROUP_CUES:
            groups.append(current); current = []
    if current:
        groups.append(current)
    return groups


def distribute(translated: str, weights: list[int]) -> list[str]:
    """Split a translation across cues by source length, losing no words."""
    words = translated.split()
    count = len(weights)
    if count == 1:
        return [" ".join(words)]
    total = sum(max(1, weight) for weight in weights)
    parts: list[str] = []
    cursor = 0
    consumed = 0
    for index, weight in enumerate(weights[:-1]):
        consumed += max(1, weight)
        target = round(len(words) * consumed / total)
        remaining_cues = count - index - 1
        # Each cue keeps at least a word when there are enough to go round.
        low = min(cursor + 1, len(words))
        high = max(low, len(words) - remaining_cues)
        target = max(low, min(target, high))
        # Prefer ending a cue where the translation pauses naturally.
        near = sorted(range(max(low, target - SNAP_WORDS), min(high, target + SNAP_WORDS) + 1),
                      key=lambda value: abs(value - target))
        target = next((value for value in near if _CLAUSE_END.search(words[value - 1])), target)
        parts.append(" ".join(words[cursor:target]))
        cursor = target
    parts.append(" ".join(words[cursor:]))
    return parts


def looks_truncated(source: str, translated: str) -> bool:
    """Flag a translation far shorter than its source (a dropped sentence)."""
    source_words = len(source.split())
    return source_words >= 6 and len(translated.split()) < source_words * 0.6
