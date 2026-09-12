from __future__ import annotations

import json
import os
import re
import time
import traceback
import uuid
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, select, update

from .asr import ASRSegment, parse_whisper_json, transcribe
from .config import settings
from .db import Artifact, Event, Job, Segment, SessionLocal, Source, StageRun, json_text, now
from .media import MediaError, extract_audio, run_command, sha256_file, validate_video
from .renderer import build_voice_track, render_video
from .translate import NLLBTranslator
from .tts import PiperSynthesizer, fit_to_slot


STAGES = ("preflight", "audio", "asr", "translation", "tts", "render", "verify")
STAGE_TIMEOUTS_SECONDS = {"preflight": 300, "audio": 900, "asr": 1800, "translation": 300,
                          "tts": 120, "render": 3600, "verify": 900}
TRANSLATION_BATCH_SIZE = 4

class PipelinePaused(RuntimeError): pass
class PipelineCancelled(RuntimeError): pass
class LeaseLost(RuntimeError): pass


def _overlap_words(left: str, right: str) -> int:
    """Return the largest suffix/prefix word overlap between two ASR cues."""
    # Use the same whitespace tokenization that trimming uses. Internal
    # punctuation such as the hyphen in ``well-being`` therefore counts as
    # one token rather than two, while punctuation at cue edges and case are
    # ignored for matching.
    normalize = lambda value: [re.sub(r"^[^\wÀ-ỹ]+|[^\wÀ-ỹ]+$", "", token.casefold(), flags=re.UNICODE)
                               for token in value.split()]
    a = [token for token in normalize(left) if token]
    b = [token for token in normalize(right) if token]
    limit = min(len(a), len(b))
    for count in range(limit, 0, -1):
        if a[-count:] == b[:count]:
            return count
    return 0


def _trim_prefix_words(value: str, count: int) -> str:
    """Drop ``count`` whitespace-delimited words while retaining punctuation."""
    words = value.split()
    return " ".join(words[count:]).strip()


def _normalize_chunk_segments(session, job_id: str, duration_ms: int, chunk_ms: int = 60_000) -> None:
    """Fence ASR timestamps and deduplicate only adjacent chunk boundaries.

    whisper.cpp can report a timestamp beyond a ``-t`` window and adjacent
    chunks can repeat words. Existing rows are reconciled after a crash too;
    translated text is retained so immutable editor snapshots remain intact.
    """
    rows = list(session.scalars(select(Segment).where(Segment.job_id == job_id).order_by(Segment.ordinal)))
    fresh = [row for row in rows if row.chunk_index is not None]
    if not fresh:
        return
    previous: Segment | None = None
    previous_chunk: int | None = None
    previous_raw_end: int | None = None
    for row in fresh:
        old_start, old_end = int(row.start_ms), int(row.end_ms)
        text_changed = False
        chunk_start = max(0, int(row.chunk_index or 0) * chunk_ms)
        chunk_end = min(duration_ms, chunk_start + chunk_ms)
        row.start_ms = max(chunk_start, min(duration_ms, int(row.start_ms)))
        row.end_ms = min(chunk_end, duration_ms, int(row.end_ms))
        if row.end_ms <= row.start_ms:
            session.delete(row)
            continue
        # Repetition within one Whisper window is legitimate speech. Compare
        # only when the source crosses into a later chunk.
        raw_temporal_overlap = previous_raw_end is not None and old_start < previous_raw_end
        if previous is not None and previous_chunk is not None and int(row.chunk_index) > previous_chunk and raw_temporal_overlap:
            overlap = _overlap_words(previous.source_text, row.source_text)
            if overlap:
                words = row.source_text.split()
                if overlap >= len(words):
                    if not row.translated_text and row.revision <= 1:
                        session.delete(row)
                        continue
                elif not row.translated_text and row.revision <= 1:
                    row.source_text = " ".join(words[overlap:])
                    text_changed = True
                    if not row.source_text:
                        session.delete(row)
                        continue
                elif row.revision <= 1:
                    # This is an automatically generated row (revision 1),
                    # so remove only the repeated source prefix. A later
                    # editor revision is deliberately left byte-for-byte
                    # intact. Apply the same conservative operation to a
                    # translated/reading prefix only when it also matches the
                    # prior generated cue.
                    source_trimmed = overlap < len(words)
                    if source_trimmed:
                        row.source_text = " ".join(words[overlap:])
                        # Force the translation stage to translate the
                        # shortened source. Keeping the old translation would
                        # speak the repeated English phrase again even though
                        # the clock has been repaired.
                        row.translated_text = ""; row.reading_text = ""; text_changed = True
                    for field in ("translated_text", "reading_text"):
                        current_text = getattr(row, field)
                        previous_text = getattr(previous, field)
                        translated_overlap = _overlap_words(previous_text, current_text) if previous_text and current_text else 0
                        if translated_overlap and translated_overlap < len(current_text.split()):
                            setattr(row, field, _trim_prefix_words(current_text, translated_overlap)); text_changed = True
            if row.start_ms < previous.end_ms:
                row.start_ms = previous.end_ms
                if row.end_ms <= row.start_ms:
                    session.delete(row)
                    continue
        if row.start_ms != old_start or row.end_ms != old_end or text_changed:
            # Fitted audio is tied to the old interval. Regenerate only this
            # segment when a resumed job reaches TTS.
            row.tts_path = None; row.tts_duration_ms = None; row.tempo = None
        previous = row
        previous_chunk = int(row.chunk_index)
        previous_raw_end = old_end


class Pipeline:
    def __init__(self, job_id: str, lease_id: str | None = None): self.job_id = job_id; self.lease_id = lease_id; self._stop_heartbeat = __import__("threading").Event()

    def _lease_guard(self, session) -> None:
        """Acquire the SQLite writer lock only while asserting this generation owns the job."""
        if not self.lease_id: return
        result = session.execute(update(Job).where(Job.id == self.job_id, Job.lease_id == self.lease_id,
                                                   Job.status.in_(["RUNNING", "PAUSING"]), Job.cancel_requested.is_(False)).values(updated_at=now()))
        if result.rowcount != 1:
            session.rollback(); raise LeaseLost()

    def _heartbeat(self) -> None:
        while not self._stop_heartbeat.wait(10):
            with SessionLocal() as session:
                if self.lease_id:
                    result = session.execute(update(Job).where(Job.id == self.job_id, Job.lease_id == self.lease_id,
                                                               Job.status.in_(["RUNNING", "PAUSING"]), Job.cancel_requested.is_(False)).values(lease_at=now(), updated_at=now()))
                    if result.rowcount != 1:
                        session.rollback(); self._stop_heartbeat.set(); return
                    session.commit()
                else:
                    job = session.get(Job, self.job_id)
                    if not job:
                        session.rollback(); self._stop_heartbeat.set(); return
                    job.lease_at = now(); session.commit()

    def _event(self, session, kind: str, payload: dict) -> None:
        self._lease_guard(session)
        session.add(Event(job_id=self.job_id, kind=kind, payload_json=json_text(payload)))
        session.commit()

    def _stage(self, session, stage: str, status: str = "RUNNING", unit_key: str = "all", error: str | None = None) -> StageRun:
        self._lease_guard(session)
        run = session.scalar(select(StageRun).where(StageRun.job_id == self.job_id, StageRun.stage == stage, StageRun.unit_key == unit_key))
        if not run:
            run = StageRun(job_id=self.job_id, stage=stage, unit_key=unit_key, status=status, attempts=0)
            session.add(run)
        run.status = status; run.attempts += 1 if status == "RUNNING" else 0; run.error = error
        if status == "RUNNING": run.started_at = now()
        if status in {"COMPLETED", "FAILED"}: run.finished_at = now()
        if self.lease_id:
            deadline = now() + timedelta(seconds=STAGE_TIMEOUTS_SECONDS.get(stage, 1800)) if status == "RUNNING" else None
            result = session.execute(update(Job).where(Job.id == self.job_id, Job.lease_id == self.lease_id).values(
                stage=stage, stage_deadline_at=deadline, updated_at=now()))
            if result.rowcount != 1:
                session.rollback(); raise LeaseLost()
        session.commit(); return run

    def _progress(self, session, stage: str, value: float, message: str = "") -> None:
        bounded = min(100.0, max(0.0, value))
        if self.lease_id:
            result = session.execute(update(Job).where(Job.id == self.job_id, Job.lease_id == self.lease_id,
                                                       Job.status.in_(["RUNNING", "PAUSING"]), Job.cancel_requested.is_(False)).values(stage=stage, progress=bounded, updated_at=now()))
            if result.rowcount != 1: session.rollback(); raise LeaseLost()
        else:
            result = session.execute(update(Job).where(Job.id == self.job_id).values(stage=stage, progress=bounded))
            if result.rowcount != 1: raise LeaseLost()
        session.commit(); self._event(session, "progress", {"stage": stage, "progress": bounded, "message": message})

    def _load_config(self, job: Job) -> dict:
        config = job.config
        config.setdefault("audio_mode", job.mode.get("audio_mode", "replace"))
        config.setdefault("subtitle_mode", job.mode.get("subtitle_mode", "burn"))
        config.setdefault("source_language", job.mode.get("source_language"))
        # Missing config means the container's default disposition, including
        # legacy jobs created before audio_index was persisted.
        config.setdefault("audio_index", job.mode.get("audio_index"))
        return config

    def _clear_stage_deadline(self, session) -> None:
        """Suspend the per-batch watchdog while a model is being loaded.

        Model construction can take longer than one inference batch and has no
        useful cancellation boundary.  The worker heartbeat and the overall
        worker watchdog still apply; the translation deadline is restored by
        ``_stage(..., status="RUNNING")`` immediately before each batch.
        """
        if not self.lease_id:
            return
        self._lease_guard(session)
        result = session.execute(update(Job).where(Job.id == self.job_id, Job.lease_id == self.lease_id,
                                                   Job.status.in_(["RUNNING", "PAUSING"]),
                                                   Job.cancel_requested.is_(False)).values(
                                                       stage_deadline_at=None, updated_at=now()))
        if result.rowcount != 1:
            session.rollback()
            raise LeaseLost()
        session.commit()

    @staticmethod
    def _translator_batch_size(translator) -> int:
        """Read a translator's advertised batch size with a safe fallback."""
        try:
            value = int(getattr(translator, "batch_size", TRANSLATION_BATCH_SIZE))
        except (TypeError, ValueError):
            value = TRANSLATION_BATCH_SIZE
        return max(1, value)

    def _translate_segments_in_batches(self, session, segments: list[Segment], translator,
                                       language: str) -> None:
        """Translate missing cues in bounded, durable units.

        Batches are formed from the complete ordered timeline rather than the
        currently missing rows.  This keeps ``batch:N`` stable across a retry;
        already translated rows are simply omitted from the next call.  A
        successful unit commits its segment text and its StageRun status in
        one lease-fenced transaction, so a later failed unit cannot discard
        earlier progress.
        """
        batch_size = self._translator_batch_size(translator)
        batches = [segments[start:start + batch_size] for start in range(0, len(segments), batch_size)]
        for batch_index, batch in enumerate(batches):
            unit_key = f"batch:{batch_index}"
            pending = [item for item in batch if not item.translated_text]
            batch_run = session.scalar(select(StageRun).where(StageRun.job_id == self.job_id,
                                                              StageRun.stage == "translation",
                                                              StageRun.unit_key == unit_key))
            if not pending:
                # Existing translations may come from an older run that did
                # not persist per-batch checkpoints.  Record the checkpoint
                # without invoking the model; this is also idempotent.
                if not batch_run or batch_run.status != "COMPLETED":
                    self._stage(session, "translation", "COMPLETED", unit_key=unit_key)
                continue

            self._ensure_owned()
            # This both marks the unit in progress and refreshes the 300s
            # deadline immediately before its external model call.
            self._stage(session, "translation", "RUNNING", unit_key=unit_key)
            outputs = translator.translate_batch([item.source_text for item in pending], language,
                                                  batch_size=batch_size,
                                                  timeout_seconds=STAGE_TIMEOUTS_SECONDS["translation"])
            if len(outputs) != len(pending):
                raise MediaError(f"NLLB returned {len(outputs)} translations for {len(pending)} segments")
            for item, translated in zip(pending, outputs):
                item.translated_text = translated.text
                if translated.warning:
                    item.warning_json = json_text(item.warnings + [translated.warning])
                if not item.reading_text:
                    item.reading_text = item.translated_text
            # _stage(COMPLETED) performs the lease assertion and commits both
            # segment changes and this unit's durable checkpoint atomically.
            self._stage(session, "translation", "COMPLETED", unit_key=unit_key)
            self._progress(session, "translation", 42 + 16 * (batch_index + 1) / len(batches))

    def _ensure_owned(self) -> None:
        with SessionLocal() as session:
            row = session.execute(select(Job.status, Job.lease_id, Job.pause_requested, Job.cancel_requested).where(Job.id == self.job_id)).one_or_none()
            if not row or (self.lease_id and row.lease_id != self.lease_id): raise LeaseLost()
            if row.pause_requested:
                predicate = [Job.id == self.job_id]
                if self.lease_id: predicate.append(Job.lease_id == self.lease_id)
                result = session.execute(update(Job).where(*predicate).values(status="PAUSED", pause_requested=False,
                    pause_acknowledged=True, lease_id=None, lease_at=None, worker_pid=None, worker_started_at=None))
                if result.rowcount != 1: session.rollback(); raise LeaseLost()
                session.commit(); raise PipelinePaused()
            if row.status == "PAUSED": raise PipelinePaused()
            if row.cancel_requested or row.status in {"CANCELLED", "CANCELLING"}: raise PipelineCancelled()

    def _terminal(self, session, status: str, error: str) -> None:
        predicate = [Job.id == self.job_id]
        if self.lease_id: predicate.append(Job.lease_id == self.lease_id)
        predicate += [Job.status.in_(["RUNNING", "PAUSING"]), Job.cancel_requested.is_(False)]
        result = session.execute(update(Job).where(*predicate).values(status=status, stage="preflight" if status == "SKIPPED_NO_AUDIO" else "asr", progress=100, error=error,
                                           lease_id=None, lease_at=None, worker_pid=None, worker_started_at=None))
        if result.rowcount != 1: session.rollback(); raise LeaseLost()
        session.commit()

    def run(self) -> None:
        session = SessionLocal()
        try:
            job = session.get(Job, self.job_id)
            if not job: return
            source = session.get(Source, job.source_id)
            if not source: raise MediaError("source record missing")
            predicate = [Job.id == self.job_id, Job.cancel_requested.is_(False)]
            if self.lease_id:
                predicate += [Job.status == "RUNNING", Job.lease_id == self.lease_id]
            else:
                predicate.append(Job.status.in_(["QUEUED", "RUNNING"]))
            claimed = session.execute(update(Job).where(*predicate).values(status="RUNNING", lease_at=now(), updated_at=now()))
            if claimed.rowcount != 1: session.rollback(); return
            session.commit()
            heartbeat = __import__("threading").Thread(target=self._heartbeat, name=f"heartbeat-{self.job_id[:8]}", daemon=True); heartbeat.start()
            config = self._load_config(job); source_path = Path(source.path)
            self._stage(session, "preflight"); self._progress(session, "preflight", 2, "Đang kiểm tra video")
            media = validate_video(source_path)
            duration_ms = round(media["duration"] * 1000)
            self._stage(session, "preflight", "COMPLETED"); self._progress(session, "preflight", 8)
            self._ensure_owned()

            if not media["audio"]:
                self._terminal(session, "SKIPPED_NO_AUDIO", "Video không có audio"); return
            generation = (self.lease_id or "manual").replace("-", "")[-24:]
            job_dir = settings.jobs_root / self.job_id / f"generation-{generation}"; job_dir.mkdir(parents=True, exist_ok=True)
            audio_path = job_dir / "source-16k.wav"
            audio_run = session.scalar(select(StageRun).where(StageRun.job_id == self.job_id, StageRun.stage == "audio", StageRun.unit_key == "all"))
            audio_checkpoint = bool(audio_run and audio_run.status == "COMPLETED" and audio_path.is_file())
            self._stage(session, "audio"); self._progress(session, "audio", 10, "Đang trích xuất audio")
            configured_audio_index = config.get("audio_index")
            selected_audio_index = int(media.get("audio_index", 0) if configured_audio_index is None else configured_audio_index)
            if selected_audio_index < 0 or selected_audio_index >= len(media["audio"]): raise MediaError(f"audio track index {selected_audio_index} is unavailable")
            selected_audio = media["audio"][selected_audio_index]
            audio_offset_ms = max(0, round(float(selected_audio.get("start_time") or 0) * 1000))
            if not audio_checkpoint:
                extract_audio(source_path, audio_path, selected_audio_index, offset_ms=audio_offset_ms, duration_seconds=media["duration"])
            self._stage(session, "audio", "COMPLETED"); self._progress(session, "audio", 18)
            self._ensure_owned()

            existing = list(session.scalars(select(Segment).where(Segment.job_id == self.job_id).order_by(Segment.ordinal)))
            self._stage(session, "asr"); self._progress(session, "asr", 20, "Whisper local đang nhận dạng")
            # Editor children already contain an immutable ASR snapshot. Legacy
            # jobs without chunk metadata are also safe to reuse. New jobs use
            # 60-second units so a process restart only repeats one unit.
            legacy_snapshot = bool(existing and all(x.source_text for x in existing) and all(x.chunk_index is None for x in existing))
            completed_chunks = {int(x) for x in config.get("asr_completed_chunks", [])}
            detected_language = config.get("detected_language") or config.get("source_language")
            if legacy_snapshot and not completed_chunks:
                asr_segments = [ASRSegment(x.start_ms, x.end_ms, x.source_text, x.confidence) for x in existing]
                asr_path = job_dir / "whisper.json"
                if not detected_language and asr_path.is_file():
                    _, detected_language = parse_whisper_json(asr_path)
            else:
                chunk_ms = 60_000
                chunk_count = max(1, (round(media["duration"] * 1000) + chunk_ms - 1) // chunk_ms)
                for chunk_index in range(chunk_count):
                    self._ensure_owned()
                    start_ms = chunk_index * chunk_ms; length_ms = min(chunk_ms, duration_ms - start_ms)
                    unit_key = f"chunk:{chunk_index}"
                    chunk_run = session.scalar(select(StageRun).where(StageRun.job_id == self.job_id, StageRun.stage == "asr", StageRun.unit_key == unit_key))
                    chunk_rows = list(session.scalars(select(Segment).where(Segment.job_id == self.job_id, Segment.chunk_index == chunk_index)))
                    if chunk_index in completed_chunks and (chunk_run is None or chunk_run.status == "COMPLETED") and chunk_rows:
                        if not detected_language:
                            chunk_json = job_dir / "asr" / f"{chunk_index:06d}.json"
                            if chunk_json.is_file(): _, detected_language = parse_whisper_json(chunk_json)
                        continue
                    # A retry of a partially written unit replaces only that unit.
                    self._lease_guard(session)
                    session.execute(delete(Segment).where(Segment.job_id == self.job_id, Segment.chunk_index == chunk_index)); session.commit()
                    self._stage(session, "asr", "RUNNING", unit_key=unit_key)
                    chunk_dir = job_dir / "asr"; chunk_dir.mkdir(parents=True, exist_ok=True)
                    chunk_audio = chunk_dir / f"{chunk_index:06d}.wav"; chunk_partial = chunk_audio.with_suffix(".wav.partial")
                    if chunk_partial.exists(): chunk_partial.unlink()
                    run_command([str(settings.ffmpeg), "-y", "-ss", f"{start_ms / 1000:.3f}", "-t", f"{max(0, length_ms) / 1000:.3f}",
                                 "-i", str(audio_path), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", "-f", "wav", str(chunk_partial)], timeout=300)
                    chunk_partial.replace(chunk_audio)
                    asr_json = chunk_dir / f"{chunk_index:06d}.json"
                    chunk_segments, chunk_language = transcribe(chunk_audio, asr_json, language=config.get("source_language"),
                                                                  threads=settings.cpu_threads, offset_ms=start_ms)
                    self._lease_guard(session)
                    if chunk_language and not detected_language: detected_language = chunk_language
                    next_ordinal = max((x.ordinal for x in session.scalars(select(Segment).where(Segment.job_id == self.job_id))), default=-1) + 1
                    chunk_end_ms = min(duration_ms, start_ms + max(0, length_ms))
                    added = 0
                    for item in chunk_segments:
                        # Whisper can report a timestamp a little beyond the
                        # requested chunk.  Clamp to the actual source window
                        # before it reaches the durable timeline.
                        item_start = max(start_ms, min(chunk_end_ms, int(item.start_ms)))
                        item_end = max(start_ms, min(chunk_end_ms, int(item.end_ms)))
                        if item_end <= item_start:
                            continue
                        session.add(Segment(job_id=self.job_id, ordinal=next_ordinal + added, chunk_index=chunk_index,
                                            start_ms=item_start, end_ms=item_end, source_text=item.text,
                                            translated_text="", reading_text="", confidence=item.confidence))
                        added += 1
                    completed_chunks.add(chunk_index)
                    config["asr_completed_chunks"] = sorted(completed_chunks)
                    if detected_language: config["detected_language"] = detected_language
                    job.config_json = json_text(config); session.commit()
                    self._stage(session, "asr", "COMPLETED", unit_key=unit_key)
                    self._progress(session, "asr", 20 + 20 * (chunk_index + 1) / chunk_count)
                # Reconcile rows from both newly completed and previously
                # checkpointed chunks.  This is idempotent and keeps a retry
                # from reintroducing a boundary overflow.
                self._lease_guard(session)
                _normalize_chunk_segments(session, self.job_id, duration_ms)
                session.commit()
                existing = list(session.scalars(select(Segment).where(Segment.job_id == self.job_id).order_by(Segment.ordinal)))
                asr_segments = [ASRSegment(x.start_ms, x.end_ms, x.source_text, x.confidence) for x in existing]
            if not asr_segments:
                self._terminal(session, "SKIPPED_NO_SPEECH", "Không phát hiện lời nói"); return
            self._lease_guard(session); session.commit(); self._stage(session, "asr", "COMPLETED"); self._progress(session, "asr", 40)
            if detected_language and config.get("detected_language") != detected_language:
                config["detected_language"] = detected_language; self._lease_guard(session); job.config_json = json_text(config); session.commit()
            self._ensure_owned()

            segments = list(session.scalars(select(Segment).where(Segment.job_id == self.job_id).order_by(Segment.ordinal)))
            language = config.get("source_language") or detected_language or "en"
            self._stage(session, "translation"); self._progress(session, "translation", 42, "NLLB local đang dịch sang tiếng Việt")
            if language.lower() in {"vi", "vie", "vie_latn"}:
                for item in segments:
                    if not item.translated_text: item.translated_text = item.source_text
            elif not all(x.translated_text for x in segments):
                if not settings.nllb_model: raise MediaError("NLLB model path is not configured")
                # Loading tokenizer/weights is outside each bounded inference
                # deadline.  Once loaded, every batch gets a fresh 300s stage
                # deadline immediately before ``translate_batch``.
                self._clear_stage_deadline(session)
                translator = NLLBTranslator(settings.nllb_model, threads=settings.cpu_threads)
                self._translate_segments_in_batches(session, segments, translator, language)
            for item in segments:
                if not item.reading_text: item.reading_text = item.translated_text
            self._lease_guard(session); session.commit(); self._stage(session, "translation", "COMPLETED"); self._progress(session, "translation", 58)
            self._ensure_owned()

            include_dubbing = bool(config.get("include_dubbing", True))
            if include_dubbing:
                if not settings.piper_model: raise MediaError("Piper model path is not configured")
                synthesizer = PiperSynthesizer(settings.piper_model, settings.piper_config if settings.piper_config and settings.piper_config.exists() else None,
                                               threads=settings.cpu_threads)
                for index, item in enumerate(segments):
                    self._ensure_owned()
                    unit_key = f"segment:{item.ordinal}"
                    self._stage(session, "tts", "RUNNING", unit_key=unit_key)
                    path = job_dir / "tts" / f"{item.ordinal:06d}.wav"
                    fitted = job_dir / "tts" / f"{item.ordinal:06d}-slot.wav"
                    if item.tts_path and Path(item.tts_path).is_file() and fitted == Path(item.tts_path):
                        warnings = item.warnings; tempo = item.tempo or 1.0; raw_ms = item.tts_duration_ms or round((item.end_ms - item.start_ms))
                    else:
                        _, raw_ms = synthesizer.synthesize(item.reading_text, path, cache_root=settings.cache_root)
                        _, _, tempo, warnings = fit_to_slot(path, fitted, max(100, item.end_ms - item.start_ms))
                    item.tts_path = str(fitted); item.tts_duration_ms = raw_ms; item.tempo = tempo
                    item.warning_json = json_text(item.warnings + warnings)
                    self._lease_guard(session); session.commit()
                    self._stage(session, "tts", "COMPLETED", unit_key=unit_key)
                    # Commit a checkpoint after each sentence so a crash loses
                    # at most one local TTS unit and the queue can resume.
                    self._progress(session, "tts", 60 + 15 * (index + 1) / len(segments))
            self._lease_guard(session); session.commit(); self._stage(session, "tts", "COMPLETED"); self._progress(session, "tts", 75)
            self._ensure_owned()

            self._stage(session, "render"); self._progress(session, "render", 78, "Đang ghép audio và phụ đề")
            self._ensure_owned()
            voice_track = build_voice_track(segments, duration_ms, job_dir / "voice-track.wav") if include_dubbing else None
            # Sources are content-addressed and can be shared by uploads with
            # different filenames. Use the immutable job name for output
            # presentation and collision-free stems.
            stem = Path(job.display_name or job.original_name or source.original_name).stem[:100]
            output_dir = settings.output_root / stem; output_dir.mkdir(parents=True, exist_ok=True)
            lease_suffix = (self.lease_id or "manual")[-8:]
            output = output_dir / f"{stem}.vi.{self.job_id[:8]}.{lease_suffix}.mp4"
            result = render_video(source_path, voice_track, segments, duration_ms, output,
                                  audio_mode=config.get("audio_mode", "replace") if include_dubbing else "original", subtitle_mode=config.get("subtitle_mode", "burn"),
                                  audio_index=selected_audio_index, width=int(media["video"].get("width") or 1920), height=int(media["video"].get("height") or 1080),
                                  include_original_track=bool(config.get("include_original_track", False)))
            try:
                self._ensure_owned()
            except LeaseLost:
                for value in result.values():
                    if isinstance(value, Path) and value.is_file(): value.unlink(missing_ok=True)
                raise
            self._stage(session, "render", "COMPLETED"); self._progress(session, "render", 96)
            self._ensure_owned()
            # Verify is recorded before publication. The final transaction then
            # fences every artifact/event insert with the current lease and clears
            # the lease atomically; a stale worker cannot publish into the queue.
            self._stage(session, "verify", "COMPLETED")
            self._lease_guard(session)
            for kind, artifact_path in (("video", result.get("video")), ("srt", result.get("srt")),
                                        ("transcript", result.get("transcript")), ("report", result.get("report"))):
                if artifact_path and Path(artifact_path).exists():
                    p = Path(artifact_path); session.add(Artifact(job_id=self.job_id, kind=kind, path=str(p),
                        trusted_root=str(settings.output_root.resolve()), sha256=sha256_file(p), size_bytes=p.stat().st_size,
                        revision=job.revision, valid=True))
            final_status = "COMPLETED_WITH_WARNINGS" if any(s.warnings for s in segments) else "COMPLETED"
            predicates = [Job.id == self.job_id, Job.status.in_(["RUNNING", "PAUSING"]), Job.cancel_requested.is_(False)]
            if self.lease_id: predicates.append(Job.lease_id == self.lease_id)
            result_update = session.execute(update(Job).where(*predicates).values(status=final_status, stage="verify", progress=100, completed_at=now(),
                                                               lease_id=None, lease_at=None, worker_pid=None, worker_started_at=None, updated_at=now()))
            if result_update.rowcount != 1: raise LeaseLost()
            session.add(Event(job_id=self.job_id, kind="completed", payload_json=json_text({"status": final_status})))
            session.commit()
        except (PipelinePaused, PipelineCancelled, LeaseLost):
            # The next dispatcher run resumes from the last durable checkpoint.
            pass
        except Exception as exc:
            session.rollback()
            predicate = [Job.id == self.job_id, Job.status.in_(["RUNNING", "PAUSING"]), Job.cancel_requested.is_(False)]
            if self.lease_id: predicate.append(Job.lease_id == self.lease_id)
            current_warning = session.scalar(select(Job.warning_json).where(*predicate))
            if current_warning is not None:
                warnings = json.loads(current_warning or "[]") + [traceback.format_exc()[-2000:]]
                result = session.execute(update(Job).where(*predicate).values(status="FAILED", error=str(exc), warning_json=json_text(warnings), updated_at=now()))
                if result.rowcount:
                    session.commit()
                    try: self._event(session, "failed", {"error": str(exc)})
                    except LeaseLost: pass
        finally:
            self._stop_heartbeat.set()
            session.close()


def run_job(job_id: str, lease_id: str | None = None) -> None:
    Pipeline(job_id, lease_id).run()
