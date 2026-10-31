from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.app.sentences import distribute, group_cues, looks_truncated, split_sentences, strip_sound_tags


def cue(text: str, start: int = 0, end: int | None = None):
    return SimpleNamespace(source_text=text, start_ms=start, end_ms=start + 1000 if end is None else end)


@pytest.mark.parametrize("text, expected", [
    ("(scissors snipping)", ""),
    ("[Music]", ""),
    ("♪ la la la ♪", ""),
    ("(tapping) (tinkling)", ""),
    ("(laughs) Hello there.", "Hello there."),
    ("Hello [applause] there.", "Hello there."),
    ("- ...", ""),
    ("- Ooh.", "Ooh."),
])
def test_strip_sound_tags(text, expected):
    assert strip_sound_tags(text) == expected


def test_split_sentences_handles_fragments_and_abbreviations():
    assert split_sentences("It kept her awake. She searched the house") == ["It kept her awake.", "She searched the house"]
    assert split_sentences("Dr. Smith agreed. Did he? Yes!") == ["Dr. Smith agreed.", "Did he?", "Yes!"]
    assert split_sentences("It cost 3.5 dollars.") == ["It cost 3.5 dollars."]
    assert split_sentences("你好。再见。") == ["你好。", "再见。"]


def test_group_cues_joins_a_sentence_split_across_cues():
    cues = [cue("the game saved his record as a", 0), cue("see-through car.", 1000), cue("He started playing.", 2000)]
    assert group_cues(cues) == [[cues[0], cues[1]], [cues[2]]]


def test_group_cues_breaks_on_silence_skips_sounds_and_caps_length():
    first, sound, later = cue("no punctuation here", 0), cue("(tapping)", 1000), cue("after a pause", 5000)
    assert group_cues([first, sound, later]) == [[first], [later]]
    many = [cue(f"word {index}", index * 1000) for index in range(10)]
    assert [len(group) for group in group_cues(many)] == [8, 2]


@pytest.mark.parametrize("weights", [[1], [10, 10], [30, 10, 5], [0, 40], [5, 5, 5, 5, 5, 5]])
def test_distribute_keeps_every_word_in_order(weights):
    text = "một hai ba, bốn năm sáu bảy tám chín mười mười một mười hai"

    parts = distribute(text, weights)

    assert len(parts) == len(weights)
    assert " ".join(part for part in parts if part).split() == text.split()


def test_distribute_prefers_natural_pauses_and_fills_each_cue():
    assert distribute("một hai ba, bốn năm sáu", [10, 10]) == ["một hai ba,", "bốn năm sáu"]
    assert all(distribute("một hai ba", [1, 1, 1]))


def test_looks_truncated_flags_dropped_sentences():
    source = "It kept her awake at night. She searched her house because the sound"
    assert looks_truncated(source, "Nó giữ cho cô ấy tỉnh táo.")
    assert not looks_truncated(source, "Nó khiến cô ấy thức trắng đêm. Cô lục soát khắp nhà vì âm thanh đó")
    assert not looks_truncated("Yes.", "Có.")


def test_unpunctuated_chinese_cues_stay_separate():
    cues = [SimpleNamespace(source_text=text, start_ms=index * 1000, end_ms=index * 1000 + 900)
            for index, text in enumerate(["你的工资和一个职员", "哦真的呀", "如果说不具备财务思维"])]

    assert [len(group) for group in group_cues(cues)] == [1, 1, 1]
