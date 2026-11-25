from __future__ import annotations
import json

from backend.app import pipeline, translate
from backend.app.translate import OllamaTranslator, Translation


WORDS = {"ignoring": "Phớt lờ.", "overtake": "vượt qua"}


class FakeNllb:
    def __init__(self):
        self.calls: list[list[str]] = []

    def translate_batch(self, texts, _source, *, timeout_seconds=None):
        self.calls.append(list(texts))
        return [Translation(WORDS.get(text, f"nllb:{text}"), "eng_Latn") for text in texts]


def translator(replies: list, fallback: FakeNllb | None = None) -> tuple[OllamaTranslator, list[str]]:
    fallback = fallback or FakeNllb()
    item = OllamaTranslator("test-model", lambda: fallback)
    prompts: list[str] = []

    def chat(prompt, _timeout, _temperature=0.2):
        prompts.append(prompt)
        reply = replies.pop(0)
        if isinstance(reply, Exception): raise reply
        return reply

    item._chat = chat
    return item, prompts


def test_numbered_lines_map_back_to_sentences():
    item, _ = translator(["2. Hai\n1. Một"])

    rows = item.translate_batch(["One", "Two"], "en")

    assert [row.text for row in rows] == ["Một", "Hai"]
    assert all(row.warning is None for row in rows)


def test_chinese_punctuation_becomes_plain_without_a_retry():
    item, prompts = translator(["1. Nó chính hãng。 Bây giờ，rất đắt！"])

    rows = item.translate_batch(["It is authentic. Now, very expensive!"], "en")

    assert rows[0].text == "Nó chính hãng. Bây giờ, rất đắt!"
    assert len(prompts) == 1


def test_chinese_leak_is_retried_alone():
    item, prompts = translator(["1. Tốt\n2. âm thanh 低调的嗡鸣声", "1. Âm thanh trầm"])

    rows = item.translate_batch(["Good", "A low hum"], "en")

    assert [row.text for row in rows] == ["Tốt", "Âm thanh trầm"]
    assert "1. A low hum" in prompts[1] and "Good" not in prompts[1] and "Mọi từ đều phải dịch" in prompts[1]


def test_english_leak_is_retried_but_names_are_kept():
    item, prompts = translator(["1. Cô không thể ignoring nó\n2. Beijing rất đông"])

    rows = item.translate_batch(["She could not ignore it", "Beijing is crowded"], "en")

    assert [row.text for row in rows] == ["Cô không thể phớt lờ nó", "Beijing rất đông"]
    assert len(prompts) == 1


def test_word_copied_from_source_is_retried():
    item, _ = translator(["1. Anh cuối cùng cũng overtake được bố"])

    rows = item.translate_batch(["He could finally overtake him"], "en")

    assert rows[0].text == "Anh cuối cùng cũng vượt qua được bố"


def test_leak_detection_uses_vietnamese_syllable_shape():
    for sentence in ("cô không thể ignores nó", "âm thanh rumbles sâu", "barely noticeable", "the sound"):
        assert translate._leaked_words(sentence), sentence
    for sentence in ("trong nhanh thanh chung nghiêng khuya", "tin nhắn gửi Beijing qua email Google", "lúc 7 giờ, 3 km", "robot chơi game video"):
        assert not translate._leaked_words(sentence), sentence


def test_unrepairable_word_keeps_the_sentence_with_a_warning():
    item, _ = translator(["1. Anh kept the zorgling lại"])

    row = item.translate_batch(["He kept the zorgling"], "en")[0]

    assert row.text.startswith("Anh") and row.warning == "Còn từ chưa dịch trong câu này"


def test_persistent_failure_falls_back_to_nllb_with_a_warning():
    fallback = FakeNllb()
    item, _ = translator(["1. Tốt", "không có số thứ tự"], fallback)

    rows = item.translate_batch(["Good", "Bad"], "en")

    assert rows[0].text == "Tốt" and rows[0].warning is None
    assert rows[1].text == "nllb:Bad" and "NLLB" in rows[1].warning
    assert fallback.calls == [["Bad"]]


def test_dropped_server_is_restarted_before_falling_back(monkeypatch):
    monkeypatch.setattr(translate, "ollama_ready", lambda _model: True)
    item, _ = translator([ConnectionResetError("closed"), "1. Một\n2. Hai"])

    rows = item.translate_batch(["One", "Two"], "en")

    assert [row.text for row in rows] == ["Một", "Hai"]
    assert all(row.warning is None for row in rows)


def test_server_error_falls_back_for_the_whole_batch(monkeypatch):
    monkeypatch.setattr(translate, "ollama_ready", lambda _model: False)
    item, _ = translator([OSError("refused")])

    assert [row.text for row in item.translate_batch(["One", "Two"], "en")] == ["nllb:One", "nllb:Two"]


def test_previous_lines_carry_pronouns_into_the_next_batch():
    item, prompts = translator(["1. Cậu bé chơi game.", "1. Cậu bé dừng lại."])

    item.translate_batch(["He played."], "en")
    item.translate_batch(["He stopped."], "en")

    assert "Cậu bé chơi game." in prompts[1] and "giữ nguyên cách xưng hô" in prompts[1]


def test_vietnamese_source_is_returned_unchanged():
    item, prompts = translator([])

    assert item.translate_batch(["Xin chào"], "vi")[0].text == "Xin chào"
    assert prompts == []


def test_pipeline_uses_nllb_when_llm_is_disabled_or_missing(monkeypatch):
    monkeypatch.setattr(pipeline, "_load_nllb", lambda: "nllb")
    monkeypatch.setenv("VIDEOAUTO_TRANSLATION_MODEL", "")
    assert pipeline._make_translator() == "nllb"
    monkeypatch.setenv("VIDEOAUTO_TRANSLATION_MODEL", "test-model")
    monkeypatch.setattr(pipeline, "ollama_ready", lambda _model: False)
    assert pipeline._make_translator() == "nllb"
    monkeypatch.setattr(pipeline, "ollama_ready", lambda _model: True)
    assert isinstance(pipeline._make_translator(), OllamaTranslator)


def test_ollama_ready_needs_the_exact_model(monkeypatch):
    monkeypatch.setattr(translate, "_ollama_models", lambda: ["qwen2.5:7b"])
    assert translate.ollama_ready("qwen2.5:7b")
    assert not translate.ollama_ready("other:1b")


def test_ollama_ready_without_server_or_binary(monkeypatch):
    def down(): raise OSError("refused")
    monkeypatch.setattr(translate, "_ollama_models", down)
    monkeypatch.setattr(translate, "_find_ollama", lambda: None)
    assert not translate.ollama_ready("qwen2.5:7b")


def test_pipeline_uses_external_api_when_chosen(monkeypatch):
    from backend.app.config import settings
    from backend.app.translate import ApiTranslator
    monkeypatch.setattr(pipeline, "_load_nllb", lambda: "nllb")
    monkeypatch.setattr(settings, "translation_provider", "api")
    monkeypatch.setattr(settings, "api_model", "gpt-test")
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setenv("VIDEOAUTO_TRANSLATION_MODEL", "")
    assert pipeline._make_translator() == "nllb"  # no key: stay local
    monkeypatch.setattr(settings, "api_key", "sk-test")
    item = pipeline._make_translator()
    assert isinstance(item, ApiTranslator) and item.model == "gpt-test"


def test_api_translator_sends_openai_request_and_falls_back(monkeypatch):
    import io
    from backend.app.translate import ApiTranslator
    sent = []

    class Opener:
        def open(self, request, timeout):
            sent.append(request)
            if len(sent) == 1: raise OSError("rate limited")
            return io.BytesIO(json.dumps({"choices": [{"message": {"content": "1. Xin chào."}}]}).encode())

    monkeypatch.setattr(translate, "build_opener", lambda *a: Opener())
    item = ApiTranslator("https://api.example.test/v1/", "sk-test", "gpt-test", lambda: FakeNllb())
    rows = item.translate_batch(["Hello."], "en")
    assert rows[0].text == "Xin chào."
    assert sent[1].full_url == "https://api.example.test/v1/chat/completions"
    assert sent[1].get_header("Authorization") == "Bearer sk-test"
    assert json.loads(sent[1].data)["model"] == "gpt-test"


def test_settings_keep_api_key_server_side(monkeypatch):
    import pytest
    from fastapi import HTTPException
    from backend.app import api
    from backend.app.db import init_db
    init_db()
    for key in ("translation_provider", "api_base_url", "api_key", "api_model"):
        monkeypatch.setattr(api.settings, key, getattr(api.settings, key))
    monkeypatch.setattr(api.settings, "api_key", "")
    with pytest.raises(HTTPException):
        api.patch_settings(api.SettingsPatch(translation_provider="api"), None)
    result = api.patch_settings(api.SettingsPatch(translation_provider="api", api_key=" sk-secret ", api_model="gpt-test"), None)
    assert result["api_key_set"] and "sk-secret" not in json.dumps(result)
    # An empty key field on a later save keeps the stored key.
    api.patch_settings(api.SettingsPatch(api_key="", api_model="gpt-other"), None)
    assert api.settings.api_key == "sk-secret" and api.settings.api_model == "gpt-other"
    api.patch_settings(api.SettingsPatch(translation_provider="local"), None)
