from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time

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
                                                 num_beams=4, max_new_tokens=512)
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
