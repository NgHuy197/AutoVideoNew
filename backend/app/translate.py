from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from urllib.request import ProxyHandler, Request, build_opener

from .config import settings
from .media import MediaError


NLLB_CODES = {"en": "eng_Latn", "zh": "zho_Hans", "ja": "jpn_Jpan", "ko": "kor_Hang", "fr": "fra_Latn",
              "de": "deu_Latn", "es": "spa_Latn", "it": "ita_Latn", "pt": "por_Latn", "ru": "rus_Cyrl",
              "th": "tha_Thai", "vi": "vie_Latn", "vie": "vie_Latn"}
DEFAULT_BATCH_SIZE = 4


@dataclass
class Translation:
    text: str
    source_code: str
    warning: str | None = None


class NLLBTranslator:
    def __init__(self, model_path: Path, device: str = "cpu", *, threads: int | None = None):
        if not model_path.exists(): raise MediaError(f"NLLB model not found: {model_path}")
        try:
            from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
        except ImportError as exc: raise MediaError("transformers is not installed") from exc
        self._torch = __import__("torch")
        self.threads = max(1, int(threads or settings.cpu_threads))
        # The pipeline uses this value to bound durable translation work units.
        # Keep it public so a different local translator can advertise its
        # preferred inference batch without changing the pipeline contract.
        self.batch_size = DEFAULT_BATCH_SIZE
        try:
            self._torch.set_num_threads(self.threads)
            self._torch.set_num_interop_threads(1)
        except RuntimeError:
            # Torch disallows changing inter-op settings after work has begun;
            # the process-level setting from worker startup still applies.
            pass
        self.tokenizer = AutoTokenizer.from_pretrained(str(model_path), local_files_only=True, src_lang="eng_Latn")
        self.model = AutoModelForSeq2SeqLM.from_pretrained(str(model_path), local_files_only=True)
        self.model.to(device)
        self.model.eval()

    def translate_batch(self, texts: list[str], source_code: str, *, batch_size: int = DEFAULT_BATCH_SIZE,
                        timeout_seconds: float | None = 300) -> list[Translation]:
        batch_size = max(1, int(batch_size))
        src = NLLB_CODES.get(source_code.lower(), source_code if "_" in source_code else None)
        if not src: raise MediaError(f"source language is not supported by NLLB mapping: {source_code}")
        if src == "vie_Latn": return [Translation(t, src) for t in texts]
        self.tokenizer.src_lang = src
        output: list[Translation] = []
        expanded: list[list[str]] = [split_for_translation(text, self.tokenizer, max_tokens=480) or [text] for text in texts]
        flat = [piece for pieces in expanded for piece in pieces]
        translated_flat: list[str] = []
        started = time.monotonic()
        for start in range(0, len(flat), batch_size):
            if timeout_seconds is not None and time.monotonic() - started > timeout_seconds:
                raise MediaError("NLLB translation batch deadline exceeded")
            batch = flat[start:start + batch_size]
            encoded = self.tokenizer(batch, return_tensors="pt", padding=True, truncation=False, max_length=480)
            if encoded["input_ids"].shape[1] > 480:
                # Caller normally splits first; never silently truncate.
                raise MediaError("translation unit exceeds 480 tokens")
            with self._torch.inference_mode():
                generated = self.model.generate(**encoded, forced_bos_token_id=self.tokenizer.convert_tokens_to_ids("vie_Latn"),
                                                 num_beams=4, max_new_tokens=512, no_repeat_ngram_size=3)
            if timeout_seconds is not None and time.monotonic() - started > timeout_seconds:
                raise MediaError("NLLB translation batch deadline exceeded")
            texts_out = self.tokenizer.batch_decode(generated, skip_special_tokens=True)
            if any(not x.strip() for x in texts_out): raise MediaError("NLLB returned an empty translation")
            if generated.shape[1] >= 511: raise MediaError("NLLB output reached generation limit")
            translated_flat.extend(x.strip() for x in texts_out)
        cursor = 0
        for pieces in expanded:
            # NLLB outputs Vietnamese words even when the source CJK has no spaces;
            # separate translated chunks so their boundaries remain readable.
            joined = " ".join(translated_flat[cursor:cursor + len(pieces)]).strip(); cursor += len(pieces)
            output.append(Translation(joined, src))
        return output


OLLAMA_URL = "http://127.0.0.1:11434"
LLM_SYSTEM_PROMPT = """Bạn là biên dịch viên lồng tiếng chuyên nghiệp. Dịch lời kể sang tiếng Việt nói tự nhiên, trôi chảy.
Quy tắc:
- Đọc toàn bộ trước khi dịch để hiểu mạch truyện và nhân vật.
- Mỗi nhân vật chỉ dùng MỘT cách gọi xuyên suốt, chọn theo tuổi và vai vế (trẻ nhỏ: "cậu bé"/"cô bé"; người lớn: "anh ấy"/"cô ấy"; người già: "ông ấy"/"bà ấy"). Không đổi giữa chừng.
- Người kể nói với khán giả thì gọi khán giả là "bạn". Câu đệm như "You see" dịch theo nghĩa.
- Dịch theo nghĩa trong ngữ cảnh, không dịch từng chữ; giữ nguyên tên riêng và số liệu.
- Không thêm hay bỏ ý. Chỉ viết bằng tiếng Việt, tuyệt đối không dùng chữ Hán hay ngôn ngữ khác.
- Giữ đúng số dòng và số thứ tự, mỗi dòng dạng "số. bản dịch". Chỉ trả về các dòng dịch."""
_NUMBERED_LINE = re.compile(r"^\s*(\d+)[.)]\s*(.+?)\s*$", re.M)
# Qwen occasionally slips into Chinese mid-sentence; Vietnamese never uses these.
_FOREIGN_SCRIPT = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\u0400-\u04ff\u0e00-\u0e7f]")
# Its Chinese punctuation is harmless to the sentence, so swap it instead of retrying.
_CJK_PUNCTUATION = str.maketrans({"。": ". ", "，": ", ", "！": "! ", "？": "? ", "；": "; ", "：": ": ",
                                  "（": "(", "）": ")"})
# Every Vietnamese syllable fits onset + vowels + coda. Lowercase ASCII words
# that do not are untranslated foreign words; capitalized names stay allowed.
_VIETNAMESE_SYLLABLE = re.compile(
    r"(?:b|ch?|d|gh?|gi|h|kh?|l|m|ngh?|nh?|ph?|qu|r|s|th?|tr|v|x)?"
    r"[aeiouy]{1,3}(?:ch|c|m|ng?|nh|p|t)?")
_COMMON_ENGLISH = {"the", "and", "with", "that", "this", "was", "were", "for", "his", "her", "him", "she", "you"}
# Loanwords normal in Vietnamese text, which the syllable shape would reject.
_LOANWORDS = {"album", "axit", "bar", "blog", "boxing", "bunker", "burger", "camera", "email", "feet", "film", "game",
              "gas", "gel", "golf", "guitar", "internet", "laptop", "logic", "marketing", "online", "oxy", "piano",
              "pizza", "play", "radio", "robot", "show", "silicone", "snap", "soda", "spam", "taxi", "tennis", "vali",
              "video", "virus", "vitamin", "web", "website", "wifi"}
CONTEXT_LINES = 5
# With no request around it, a numbered Chinese list comes back copied, not translated.
LINES_HEADER = "Dịch từng dòng sau sang tiếng Việt, tuyệt đối không chép lại nguyên văn:\n"
LINES_FOOTER = "\n\nBản dịch tiếng Việt:"
RETRY_HINT = "Lưu ý: lần trước còn sót từ không phải tiếng Việt. Mọi từ đều phải dịch sang tiếng Việt.\n"
MAX_WORD_REPLY = 4


def _plain_punctuation(text: str) -> str:
    return re.sub(r" {2,}", " ", text.translate(_CJK_PUNCTUATION)).strip()


def _leaked_words(translation: str) -> set[str]:
    """Lowercase words that cannot be Vietnamese, so were left untranslated."""
    words = set(re.findall(r"(?<![\w-])[a-z]{3,}(?![\w-])", translation))
    return {word for word in words if word not in _LOANWORDS
            and (word in _COMMON_ENGLISH or not _VIETNAMESE_SYLLABLE.fullmatch(word))}


def _opener():
    # Never send loopback calls through a user-configured proxy.
    return build_opener(ProxyHandler({}))


def _find_ollama() -> str | None:
    bundled = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
    return shutil.which("ollama") or (str(bundled) if bundled.is_file() else None)


def _ollama_models() -> list[str]:
    with _opener().open(f"{OLLAMA_URL}/api/tags", timeout=3) as response:
        return [str(item.get("name")) for item in json.load(response).get("models", [])]


def ollama_ready(model: str, *, wait_seconds: float = 15) -> bool:
    """Return whether ``model`` can be used, starting the local server if needed."""
    try:
        return model in _ollama_models()
    except (OSError, ValueError):
        pass
    executable = _find_ollama()
    if not executable:
        return False
    try:
        subprocess.Popen([executable, "serve"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        return False
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        try:
            return model in _ollama_models()
        except (OSError, ValueError):
            time.sleep(0.5)
    return False


class OllamaTranslator:
    """Context-aware translation through a local Ollama model.

    It keeps the ``translate_batch`` contract, so durable batches work the same
    way. Lines it cannot translate cleanly go to the NLLB fallback, so a model
    failure costs quality on those lines rather than failing the job.
    """

    batch_size = 16

    def __init__(self, model: str, load_fallback: Callable[[], NLLBTranslator]):
        self.model = model
        self._load_fallback = load_fallback
        self._fallback: NLLBTranslator | None = None
        self._recent: list[str] = []

    def _chat(self, prompt: str, timeout_seconds: float, temperature: float = 0.2) -> str:
        body = json.dumps({"model": self.model, "stream": False, "keep_alive": "2m",
                           "options": {"temperature": temperature, "num_ctx": 8192},
                           "messages": [{"role": "system", "content": LLM_SYSTEM_PROMPT},
                                        {"role": "user", "content": prompt}]}).encode("utf-8")
        request = Request(f"{OLLAMA_URL}/api/chat", body, {"Content-Type": "application/json"})
        with _opener().open(request, timeout=timeout_seconds) as response:
            return str(json.load(response)["message"]["content"])

    def _nllb(self) -> NLLBTranslator:
        if self._fallback is None: self._fallback = self._load_fallback()
        return self._fallback

    def _repair(self, translation: str, source_code: str, timeout_seconds: float | None) -> tuple[str, str | None]:
        """Replace leaked words through NLLB, keeping an otherwise good sentence.

        Retrying the line repeats the leak whatever the prompt, and asking this
        model for a word's meaning returns Chinese. One word that stays English
        must not cost the sentence its context, so that case only warns.
        """
        for word in sorted(_leaked_words(translation)):
            reply = self._nllb().translate_batch([word], source_code, timeout_seconds=timeout_seconds)[0].text
            reply = reply.strip().strip('".,;:!?').lower()
            if reply and len(reply.split()) <= MAX_WORD_REPLY and not _FOREIGN_SCRIPT.search(reply) and not _leaked_words(reply):
                translation = re.sub(rf"\b{re.escape(word)}\b", reply, translation)
        return translation, "Còn từ chưa dịch trong câu này" if _leaked_words(translation) else None

    def _attempt(self, texts: list[str], timeout_seconds: float, *, retry: bool = False) -> dict[int, str]:
        context = ""
        if self._recent:
            joined = "\n".join(self._recent[-CONTEXT_LINES:])
            context = f"Đoạn đã dịch ngay trước đó, giữ nguyên cách xưng hô:\n{joined}\n\nDịch tiếp:\n"
        numbered = "\n".join(f"{index}. {text}" for index, text in enumerate(texts, 1))
        # The same prompt at a low temperature repeats the same mistake.
        prompt = (RETRY_HINT if retry else "") + context + LINES_HEADER + numbered + LINES_FOOTER
        lines = _NUMBERED_LINE.findall(self._chat(prompt, timeout_seconds, 0.6 if retry else 0.2))
        return {int(number) - 1: _plain_punctuation(text) for number, text in lines
                if 0 < int(number) <= len(texts) and not _FOREIGN_SCRIPT.search(text)}

    def translate_batch(self, texts: list[str], source_code: str, *, batch_size: int = 16,
                        timeout_seconds: float | None = 300) -> list[Translation]:
        src = NLLB_CODES.get(source_code.lower(), source_code)
        if src == "vie_Latn": return [Translation(text, src) for text in texts]
        # Two tries plus the fallback must fit inside one stage deadline.
        per_call = (timeout_seconds or 600) * 0.4
        done: dict[int, str] = {}
        missing = list(range(len(texts)))
        for attempt in range(2):
            if not missing: break
            try:
                found = self._attempt([texts[index] for index in missing], per_call, retry=attempt > 0)
            except (OSError, ValueError, KeyError, TypeError):
                break
            done.update({missing[index]: text for index, text in found.items()})
            missing = [index for index in missing if index not in done]
        rows: list[Translation | None] = [None] * len(texts)
        for index, text in done.items():
            repaired, warning = self._repair(text, source_code, timeout_seconds)
            rows[index] = Translation(repaired, src, warning)
        if missing:
            fallback = self._nllb().translate_batch([texts[index] for index in missing], source_code,
                                                    timeout_seconds=timeout_seconds)
            for index, row in zip(missing, fallback):
                rows[index] = Translation(row.text, row.source_code, "Câu này dịch bằng NLLB dự phòng")
        self._recent = (self._recent + [row.text for row in rows])[-CONTEXT_LINES:]
        return rows

    def close(self) -> None:
        """Free the model's RAM before TTS and render."""
        body = json.dumps({"model": self.model, "keep_alive": 0}).encode("utf-8")
        try:
            _opener().open(Request(f"{OLLAMA_URL}/api/generate", body, {"Content-Type": "application/json"}), timeout=10).close()
        except OSError:
            pass


def split_for_translation(text: str, tokenizer, max_tokens: int = 480) -> list[str]:
    pieces = [x.strip() for x in __import__("re").split(r"(?<=[.!?。！？])\s*", text) if x.strip()]
    if not pieces: return []
    result: list[str] = []; current = ""
    for piece in pieces:
        candidate = f"{current} {piece}".strip()
        if len(tokenizer(candidate, add_special_tokens=True)["input_ids"]) > max_tokens and current:
            result.append(current); current = piece
        else: current = candidate
    if current: result.append(current)
    # A single punctuation-free sentence can still be overlong; split by words.
    final: list[str] = []
    for item in result:
        ids = tokenizer(item, add_special_tokens=True)["input_ids"]
        if len(ids) <= max_tokens: final.append(item); continue
        words = item.split()
        if len(words) > 1:
            current = ""
            for word in words:
                candidate = f"{current} {word}".strip()
                if len(tokenizer(candidate, add_special_tokens=True)["input_ids"]) > max_tokens and current:
                    final.append(current); current = word
                else: current = candidate
            if current: final.append(current)
        else:
            # CJK and punctuation-free transcripts have no whitespace boundaries.
            current = ""
            for character in item:
                candidate = current + character
                if len(tokenizer(candidate, add_special_tokens=True)["input_ids"]) > max_tokens and current:
                    final.append(current); current = character
                else: current = candidate
            if current: final.append(current)
    return final
