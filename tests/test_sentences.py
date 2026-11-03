from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.app.sentences import break_cost, distribute, group_cues, looks_truncated, split_sentences, strip_sound_tags
from backend.app.subtitles import caption_cues, merge_fragments


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


def test_break_cost_prefers_clause_seams_over_compound_words():
    assert break_cost("chết,", "nhưng") < break_cost("đọc", "báo") < break_cost("báo", "cáo")
    assert break_cost("ty", "trong") < break_cost("của", "các")
    assert break_cost("kiếm", "được") > break_cost("đọc", "báo")


def test_distribute_avoids_cutting_a_compound_word():
    assert distribute("Công ty kiếm được nhiều lợi nhuận trong năm nay", [10, 10])[0].split()[-1] != "lợi"


def test_captions_break_beside_function_words_and_keep_compounds():
    text = ("Cuối cùng đã đến video hướng dẫn bạn đọc báo cáo tài chính "
            "của các công ty trong TOP 500 thế giới rồi.")
    cues = caption_cues(SimpleNamespace(start_ms=0, end_ms=3400, translated_text=text))
    lines = [line for _, _, caption in cues for line in caption.split("\n")]
    assert " ".join(lines).split() == text.split()
    for first, second in zip(lines, lines[1:]):
        assert (first.split()[-1], second.split()[0]) not in {("báo", "cáo"), ("hướng", "dẫn"), ("tài", "chính")}
    # No caption is left holding a lone trailing word.
    assert all(len(caption.split()) > 2 for _, _, caption in cues)


def test_tight_two_line_fit_takes_another_cue_instead_of_splitting_a_word():
    text = "cả hai đều sẽ chết, nhưng có thể phẫu thuật tách rời, cho phép một bé sống sót."
    cues = caption_cues(SimpleNamespace(start_ms=0, end_ms=7000, translated_text=text))
    assert all("phẫu\nthuật" not in caption for _, _, caption in cues)
    assert " ".join(caption for _, _, caption in cues).split() == text.split()


def test_dangling_fragment_shares_the_screen_with_the_next_cue():
    rows = [SimpleNamespace(start_ms=0, end_ms=3000, translated_text="Lợi nhuận kinh doanh của nó thua lỗ"),
            SimpleNamespace(start_ms=3000, end_ms=3600, translated_text="Nhưng"),
            SimpleNamespace(start_ms=3600, end_ms=6300, translated_text="Nó nhận được trợ cấp từ chính phủ")]
    merged = merge_fragments(rows)
    assert [row.translated_text for row in merged] == [rows[0].translated_text,
                                                       "Nhưng Nó nhận được trợ cấp từ chính phủ"]
    assert caption_cues(merged[1]) == [(3000, 6300, "Nhưng\nNó nhận được trợ cấp từ chính phủ")]


def test_complete_unpunctuated_cues_are_not_run_together():
    rows = [SimpleNamespace(start_ms=0, end_ms=1300, translated_text="Nó không có tính liên tục"),
            SimpleNamespace(start_ms=1300, end_ms=2200, translated_text="Nó là sự kiện ngẫu nhiên")]
    assert len(merge_fragments(rows)) == 2
