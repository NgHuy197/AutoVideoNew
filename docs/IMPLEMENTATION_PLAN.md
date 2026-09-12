# Video Auto: accepted implementation contract

The user approved the Vietnamese plan in the parent task. Implementation is assigned to GPT 5.6 Luna (xhigh), followed by independent GPT 6 Astra review. This document preserves the decisions and acceptance checklist; it does not claim they have already passed.

## Product

- Personal Windows application, Vietnamese localhost web UI, React/TypeScript/Vite and FastAPI/Python 3.14.
- Upload local videos (resumable 8 MiB chunks, two concurrent files) or watch configured folders. Default limit 2 GiB / 30 minutes; primary workload clips under 15 minutes.
- Offline pipeline: source -> probe -> extract PCM16 mono 16 kHz -> Whisper.cpp small Q5_1 -> NLLB-200 distilled 600M -> Piper Vietnamese Ban Mai -> timeline audio/subtitles -> validated local export.
- Output modes: subtitles only, dubbing only, both (default). Subtitle modes: burn-in (default), soft mov_text, sidecar SRT. Audio replacement (default) or original mixed at -18 dB. Optional original secondary audio track.
- Preserve video timeline. Accelerate full TTS as necessary (chained atempo factors <=2); never truncate spoken sentences, extend video, or silently fall back to subtitles. Warn over 1.5x / 2x.
- Translation editor creates immutable render revisions, reuses source/ASR and unchanged TTS. Never block unattended processing for review.
- No cloud inference, video downloading, voice cloning, speaker diarization, source separation, lip sync, public/LAN deployment.

## Environment

- Workspace: C:\Users\PC\Documents\ChatGPT\Video Auto (initially empty Git repository).
- CPU Ryzen 7 7840HS 8c/16t; about 27.7 GiB OS-visible RAM; integrated AMD GPU, no CUDA. Default six CPU inference threads, one video at a time.
- Existing model root: C:\Users\PC\Documents\Codex\2026-09-05\ti\work\local-speech. Models: models/whisper/ggml-small-q5_1.bin, models/nllb (complete HF directory), models/piper/banmai.onnx and matching JSON.
- Working Whisper runtime: C:\Users\PC\Downloads\whisper-bin-x64\Release, version 1.9.2. Preserve its DLLs; a lone copied executable does not run.
- Python C:\Python314\python.exe; torch 2.14.0+cpu, transformers 5.16.1, piper-tts 1.8.0. NLLB and Piper real inference verified during planning. Piper is single speaker, sample rate 22050 Hz.
- FFmpeg 8.1 was found inside another application, no standalone FFprobe initially found. Supply a matched standalone pair.
- UTF-8 is mandatory for subprocesses, console/logging, JSON, SRT and ASS; the default Windows console failed on Vietnamese during inspection.

## Architecture and data

- Separate HTTP/UI from durable scheduler/supervisor and per-job child process. Browser closure/API restart must not stop jobs. Do not use request-bound inference or FastAPI BackgroundTasks as the queue.
- SQLAlchemy/Alembic SQLite on local disk, WAL, synchronous FULL, busy timeout, short transactions. Back up using SQLite Backup API.
- Entities: Source, Job, StageRun, Segment, Artifact, WatchFolder, Event, Preset. Jobs hold immutable settings snapshots, revision, priority and attempt counters; segments have stable IDs and integer millisecond timestamps, source/translated text, measured TTS duration and acceleration warnings.
- API /api/v1: uploads/chunks/complete/status; jobs/list/detail/control/priority; segments edits and render revision; artifacts via IDs and HTTP Range; SSE events with replay cursor; settings/presets/watch-folders; live/ready/diagnostics.
- Idempotent upload/job creation, generation/lease fencing, atomic artifact publication. No arbitrary command execution or arbitrary-path downloads.
- Bind 127.0.0.1:8765, serve built frontend. Validate Host/Origin, local session and CSRF on mutations.
- Data in resolved user LocalAppData/VideoAuto; exports in resolved user Videos/Video Auto Exports. Preserve original models and external source videos; copy into managed storage. Store absolute paths for pre-login operation.
- Import model/runtime manifest with hashes; offline local_files_only, HF_HUB_OFFLINE, telemetry disabled. Missing dependencies block the affected work clearly, never auto-download at inference time.

## Pipeline correctness

- Probe container/streams/duration/rotation/start offsets. Select requested audio track, else default then first. Reject unsupported HDR explicitly in v1. Distinguish no audio and no speech.
- Preserve common video/audio origin, leading audio silence and VFR timeline. ASR chunks target 60s near silence; otherwise 1s overlap with time/text deduplication. Do not remove silence from the timeline.
- Whisper transcribes original language (not --translate English); JSON-full and progress. Map supported Whisper language codes explicitly to NLLB. Manual override; Vietnamese source skips NLLB. Per-chunk language changes supported, intra-sentence code switching not guaranteed.
- Sentence-level NLLB, target <=256 tokens, split before 480 incl special tokens; no silent truncation. Batch4, CPU fp32, beam4, direct AutoTokenizer/AutoModelForSeq2SeqLM, force vie_Latn. Detect empty/repetitive/truncated output and split/retry within persisted bounded attempts.
- Merge overlapping ASR translation slots; enforce positive durations. TTS cache keyed by full text/model/config. Keep display and spoken text distinct; no ambiguous number/date rewrites.
- Fit TTS to each absolute slot; pad short audio, accelerate long audio, measure after tempo transform. Resample final audio to 48kHz; do not load full video PCM into RAM.
- SRT UTF8, ASS/libass burn-in, local Noto Sans, <=2 lines and target42 chars/line, proportional cue timing (not claimed word alignment).
- Output MP4 H264 yuv420p CRF20 veryfast and AAC48k192k faststart. Copy compatible video when no burn-in; otherwise encode without forcing30fps, preserve displayed geometry. Normalize/limit final mix.
- Render .partial in target filesystem, probe and full-decode validate before publish. Duration tolerance max(100ms,2 frames), expected streams and subtitle ranges. Include machine-readable transcript and processing report. Warned quality can complete; missing required technical output cannot.

## Unattended reliability

- QUEUED/RUNNING/RETRY_WAIT/PAUSED/WAITING_RESOURCES/FAILED/CANCELLED/COMPLETED/COMPLETED_WITH_WARNINGS, plus explicit skip outcomes. FIFO within priority; errors do not block other jobs.
- Checkpoint source, extracted audio, each ASR chunk, translation batch, TTS unit, composed tracks and validated output. Flush/validate/rename before marking artifact committed; recovery reconciles files and DB. At-least-once work, idempotent publication.
- Heartbeat10s, lease60s with old-owner process handling. Persist two retries (30s/120s). ASR20m per60s chunk, NLLB5m/batch, Piper2m/unit, FFmpeg no-progress5m. Heartbeat is not progress.
- Windows Job Objects terminate process trees, no orphan FFmpeg/Whisper. Pause at unit boundary, cancel process tree, preserve reusable checkpoints.
- Watch folders scan startup/every10s; stable size/mtime30s, ignore partials and managed/output dirs, no junction escape, copy snapshot with metadata verification and Windows writer exclusion where available. Hash+settings+pipeline dedup; explicit rerun permitted.
- One heavy AI stage, six threads. Wait below4GiB free RAM. Reserve20GiB disk plus estimated work; monitor during uploads/render. Never delete external originals or final results to recover space.
- Retain outputs/translations and managed source until explicit deletion; completed intermediate cache7days, failed14days, partial uploads24h, logs30days capped200MB, daily DB backups keep7. Respect live references.
- Task Scheduler boot trigger under installing user S4U, hidden supervisor, absolute paths, unlimited execution time, restart on error, singleton. No network/EFS dependency. Prevent idle sleep on AC only when24/7 enabled; do not claim power-loss or lid-policy immunity.

## Delivery and verification

Deliver implementation, locks, config samples, setup/start/stop/diagnostics and startup registration/removal tools, Vietnamese docs, actual verification report.

Test output modes/mixes; English/Chinese/Japanese/Vi and unsupported language; vertical/VFR/rotation/audio offsets/multiple tracks/Unicode paths; long speech/silence/chunk boundaries/tempo>2; upload interruption/dedup; watcher locks/partial copy; crash during each stage and publish/DB boundary; disk/RAM/model/runtime failures; immutable revisions/cache; offline/loopback/security; boot-before-login and process cleanup.

Run real local-model end-to-end smoke and a24-hour soak with injected failures/reboot when feasible. Never label the24-hour soak or reboot acceptance passed without actually performing it. Record stage latency, real-time factor, RAM/disk peaks and warnings; no promised real-time speed or guaranteed translation accuracy.
