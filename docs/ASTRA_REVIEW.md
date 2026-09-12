# GPT 6 Astra review — incomplete implementation draft

Reviewed 2026-09-08. This is an independent review of an actively changing draft, not release acceptance. Locations below refer to the files observed during this review and may move as Luna applies fixes. The accepted contract is `docs/IMPLEMENTATION_PLAN.md`.

## Evidence and disposition

**Not ready for unattended use.** Static inspection covered backend, frontend, runtime scripts and dependency declarations. Executed only a lightweight subtitle-function reproduction and inspected the installed Piper API source. No application end-to-end run, boot test or 24-hour soak was performed by this reviewer. Root is running independent QA. Do not interpret model reference smoke success as application pipeline success.

Executed reproduction: `ass_stamp(12345)` returned `0:00:00.34`, expected `0:00:12.34`. A 30-word caption returned only words 0–12. Installed Piper `PiperVoice.synthesize_wav` takes `wave.Wave_write` and calls `setframerate`, `setsampwidth`, `setnchannels`, `writeframes`.

## Prioritized findings

| ID / severity | Verified location | Trigger, impact and required correction |
|---|---|---|
| R01 P1 | backend/app/tts.py:42,69 | Every uncached TTS passes a raw binary file to Piper's WAV-writer API, causing an attribute error. Slot rendering then targets `.wav.partial` without `-f wav`, so FFmpeg cannot infer output format. Use `wave.open`, explicit muxer, validated atomic writes and a real synthesized-slot smoke. |
| R02 P1 | backend/app/subtitles.py:17,28 | All ASS seconds are divided by 1000 twice; cues collapse toward minute starts. Long text is discarded after two wrapped lines. Correct timestamp arithmetic and split long text into multiple timed cues while preserving every word. |
| R03 P1 | backend/app/api.py:213,225; backend/app/pipeline.py:104 | Saving changes mutates original segments without expected revision/status checks. Rerender creates a child with no edited segments; pipeline runs ASR/translation again. The exported video loses user edits, and old revisions no longer describe their output. Create immutable edit snapshots with optimistic concurrency, clone/reference segments into child and skip ASR/translation for render jobs. |
| R04 P1 | backend/app/service.py:35,42,46; backend/app/pipeline.py:156 | Dispatcher capacity counts only in-memory children. Restarting API while a worker survives allows another heavy job; a worker dying shortly after restart stays RUNNING because stale recovery happens only at startup. Old workers are not terminated before reclaim. Stage/status/artifact writes lack atomic lease predicates; the final ownership check is after publication/commit and can read a cached ORM object. Separate supervisor lifecycle, durable singleton/ownership, periodic stale recovery, process-tree reconciliation and atomic generation fencing are needed. |
| R05 P1 | backend/app/service.py:70; backend/app/pipeline.py:162; backend/app/media.py:38 | Normal pipeline exceptions set FAILED, so reaper's RUNNING-only retry branch never retries them. NLLB/Piper execute without deadlines while heartbeats continue. A stuck inference occupies the queue forever. Add persisted bounded per-unit retry and monitored process deadlines; ensure errors in dispatcher/watch processing cannot kill its thread silently. |
| R06 P1 | backend/app/pipeline.py:96; backend/app/media.py:87; backend/app/renderer.py:62 | Audio extraction ignores selected config index and always uses probe default, while original mixing uses config default 0. Audio start offsets are discarded in WAV without compensating silence. Multi-track/delayed-audio sources therefore translate the wrong track or produce early speech/subtitles. Resolve selected audio once and normalize all timelines against a common media origin. |
| R07 P1 | backend/app/api.py:27; backend/app/pipeline.py:130; frontend/src/App.tsx:39 | No subtitles-only job mode exists; all jobs load Piper, construct dubbing and replace/mix audio. UI hardcodes replace+burn. Implement explicit output mode across schema, UI and pipeline so subtitle-only succeeds without Piper and preserves original audio. |
| R08 P1 | backend/app/api.py:134,143 | A zero/short chunk is marked received; preallocated gaps remain zeros and completion accepts them unless optional checksum supplied. Entire request is buffered before size validation. Completed uploads remain writable, repeated completion creates jobs, concurrent completion/write is unprotected. Require exact expected length, bounded streaming/checksum, per-upload serialization/status transitions and stored idempotent completion result. Preserve the original filename rather than upload-ID filename when creating Source. |
| R09 P1 | backend/app/asr.py:33,71; backend/app/pipeline.py:113 | Auto selection omits explicit `-l auto`; parser reads only top-level language rather than Whisper.cpp `result.language`. Missing language falls back silently to English. Explicitly request auto and parse/test actual installed CLI output before routing to NLLB. |
| R10 P1 | backend/app/translate.py:36,43; backend/app/pipeline.py:121 | Unsupported language returns original foreign text with warning, then Vietnamese Piper reads it and job can complete. Oversized units raise instead of using splitting helper; truncated/empty/repetitive generation is not validated. Fail unsupported mappings clearly, split by tokenizer boundaries including punctuation-free CJK, validate EOS/content and retry bounded units. |
| R11 P1 | backend/app/service.py:95; backend/app/api.py:54 | Watcher hashes then separately copies an unlocked, possibly changing source with no post-copy verification; content hash can describe different bytes. It follows file links, accepts managed/output directories, dedups only active jobs regardless of preset and has no immutable source-copy coordination. Validate containment/reparse points, exclude managed outputs, copy stable snapshots and dedup content+settings+pipeline. SQLite returns naive datetimes; `.timestamp()` interprets UTC marker as local time and can bypass 30s stability in Asia/Saigon. Use explicit UTC arithmetic. |
| R12 P1 | backend/app/api.py:302; backend/app/config.py:44; backend/app/db.py:166 | Settings updates change API memory only; worker subprocesses read defaults/environment. Restart loses values, changing data_root does not rebuild derived paths/engine, and changing output_root during work breaks artifact retrieval. Persist validated settings and immutable worker snapshots; use an explicit idle-only migration for managed paths. |
| R13 P1 | scripts/setup.ps1:9,22; requirements.txt | Fresh venv installs no torch/Transformers/Piper despite comment saying setup installs them separately. Script does not check native command exit codes and can announce success after pip/npm failure. Pin/install the verified AI runtime (or explicitly provision a reproducible local runtime), check each exit code, verify all models/tools and emit manifest hashes. Current broad version ranges are not dependency locks. |
| R14 P2 | backend/app/tts.py:63; backend/app/renderer.py:18,37 | Tempo output is blindly trimmed and never measured after transformation; shorter-than-slot output is not padded in accelerated branch. Remaining overlapping ASR slots concatenate with cursor advancement unrelated to actual accumulated audio length, causing drift or clipping. Normalize all overlaps and enforce measured slot sample counts without dropping spoken content. |
| R15 P2 | backend/app/renderer.py:37,77 | Validation only probes streams/duration, never full-decodes or verifies requested subtitle track/ranges. No limiter/loudness normalization, no secondary original track, always reencodes even when copy is possible. Add output-mode-specific validation/full decode and audio normalization. Font is named but not bundled/passed via fontsdir; sizing ignores displayed rotation and frame height. |
| R16 P2 | backend/app/api.py:318 | Origin prefix allows `http://localhost.evil.example` and arbitrary local ports. This alone is not a demonstrated CSRF exploit because double-submit cookie/header is also required, but violates exact same-origin protection. Parse origin and require exact allowed scheme/host/port, add server-bound local session and reject missing/invalid Host. |
| R17 P2 | frontend/src/App.tsx:20,37,38 | UI never renders job-control buttons despite action props, offers no artifact download/player/watch-folder management/preset/audio selection, and selected detail does not refresh due initial-effect stale closure. SSE listens to unnamed messages while backend emits named events; reconnect does not consume Last-Event-ID. Complete real user paths and add browser verification. |
| R18 P2 | scripts/start.ps1:7; scripts/install-task.ps1:7 | Boot task launches API directly with no independent supervisor/restart containment. Start wrapper does not propagate Python exit status reliably; no Job Objects, persistent PID ownership or sleep policy. Implement supervised hidden runtime, qualified account identity and startup diagnostics; keep pre-login/reboot acceptance unverified until executed. |

Pause/cancel work was being added during review (`Pipeline._ensure_owned`). Recheck at each unit and before publication; merely checking between whole ASR/translation stages does not satisfy bounded pause/cancel or stale-owner fencing. Likewise, fixes to the above must be verified against final files before any finding is marked closed.

## Contract coverage at review snapshot

| Contract area | Coverage | Evidence / remaining work |
|---|---|---|
| FastAPI + React local scaffold | Partial | Code exists; build/browser not executed by reviewer. |
| SQLite WAL/FULL/busy timeout | Implemented in code | No crash test; migrations/foreign-key model constraints absent. |
| Durable entities | Partial | Basic entities exist; Preset and migration lifecycle missing. |
| Upload resume/integrity/idempotency | Partial | Chunk/status routes; integrity, concurrency, idempotency and browser resume absent. |
| Three output modes and original track | Missing/partial | Dub+subtitle branches exist; subtitles-only and secondary original absent. |
| ASR chunking/offsets/per-chunk language | Missing | Whole WAV to one invocation; no 60s checkpoints. |
| NLLB local | Partial | Direct local model loading exists; splitting/retry/output validation absent. |
| Piper local and slot fit | Partial/broken | API writer and muxer blockers R01; no validated no-truncation fit. |
| Subtitle preservation/cue timing | Partial/broken | R02 data loss/timestamps. |
| Render validation/atomic publication | Partial | MP4 partial rename/probe; no full decode, manifest transaction or idempotent publish. |
| Immutable editor/rerender cache | Missing/broken | R03. |
| Supervisor/leases/recovery/cancel | Partial | Child isolation/heartbeat/CAS claim exist; R04/R05. |
| Per-unit durable checkpoints | Missing | StageRun records do not gate reuse; pipeline restarts from extraction and deletes segments. |
| RAM/disk waits and resource monitoring | Missing | Config thresholds declared but unenforced. |
| Watch folders | Partial/unsafe | R11; no UI. |
| Retention, bounded logs, daily DB backups | Missing | Directories only. |
| Offline inference | Partial | NLLB local_files_only; HF offline/telemetry policy not configured; dependency checks absent. |
| Local Host/Origin/CSRF/path download | Partial | Explicit argv and artifact containment positive; R16. |
| Runtime import/hash manifests/locks | Missing | Setup references original external resources, no import/hashes. |
| Task Scheduler bootstrap | Partial/unverified | Script exists; no boot, logout, restart or permission test. |
| Vietnamese docs/config examples | Missing | Plan only at review snapshot. |
| Full app E2E / 24h soak / reboot | Unverified | No reviewer claim of these checks. |

## Required re-review gate

First establish real Whisper→NLLB→Piper→MP4 smoke after R01/R02/R09, then test subtitle-only without Piper, immutable edits, delayed/multiple audio tracks, chunk corruption/idempotency and process-kill recovery. Run browser controls/download flows. Review final diff and test evidence again; retain explicit limitations for soak/reboot until measured.

## Baseline execution evidence added 2026-09-09

Root executed the API/media probes recorded in `.runtime/root-api-probes.json`; this reviewer read that artifact rather than rerunning its requests. These results describe the baseline draft and must be rerun after fixes:

| Finding | Recorded result | Interpretation |
|---|---|---|
| R08 chunk integrity | `empty_chunk=200`, `empty_complete=200` | A zero-length chunk was accepted and its upload was accepted as complete. |
| R08 completion idempotency | First job `b9db3fe5-a57f-49f7-b433-e62f0c64b258`; repeated completion job `f5d21413-e357-40ec-a4ee-3b5cdb9be8bd`, both HTTP 200 | Repeated completion creates distinct jobs for the same source. |
| R16 Origin validation | `spoofed_origin_read=200` | The spoofed-Origin read probe was accepted. This is evidence of Origin validation failure, not proof of a complete browser CSRF exploit or cross-origin response access. |
| R06 delayed audio | Extracted audio 11.072 seconds for a 13-second video | The delayed-audio fixture loses its leading timeline offset during extraction; common-origin alignment needs explicit compensation. |
| API availability | `health=200` | API starts in the root QA environment; this does not verify AI dependencies, readiness or durable scheduling. |

The initial subtitle reproductions and installed Piper API inspection above remain reviewer-executed evidence. All R01–R18 are baseline findings pending a separate final re-review against Luna's fixes. No 24-hour soak, reboot-before-login, full application E2E or release acceptance is claimed here.

## Scoped follow-up: queue, publication and revisions (2026-09-09)

This is **not final review**. Luna's current changes fix several baseline issues and root reports successful render-mode, rotation/offset, long concat-list and real-model checks. Those are root-reported results, not re-executed here. Baseline R01–R18 should not be read as a statement that every original defect remains in the changing tree.

Reviewer executed lightweight database/API-function probes with `.runtime/qa-venv/Scripts/python.exe`, isolated data/output root `.runtime/astra-queue-probes`, and embedded queue disabled. No worker or heavy inference was launched. Observed outputs:

```json
{"stale_owner_progress":96.0,"current_lease":"new"}
{"cancel_response":"CANCELLED"}
{"cancel_keeps_lease":"new"}
{"new_dispatcher_children":0,"claim_while_other_running":true}
```

### Current actionable findings

1. **Q01 P1 — Fencing reads stale ORM identity state.** `backend/app/pipeline.py:41`, `:48`, `:59`, `:179`. Reproduction: create RUNNING Job with lease `old`, retain Job in session A; session B changes lease to `new`; call `Pipeline(id, 'old')._progress(A, 'render', 96)`. It commits progress under the new lease without raising. The retained `job` and `expire_on_commit=False` keep the stale identity live. Fresh `_ensure_owned()` at some boundaries does not make subsequent writes atomic. Minimal correction: use short write transactions with an atomic `UPDATE ... WHERE id AND lease_id AND allowed_status` gate; gate stage/event/segment/artifact changes inside that same transaction. Handle zero rows as ownership loss. Add this exact two-session regression, plus cancellation between final check and commit.

2. **Q02 P1 — Publication and stale cleanup share the next worker's paths.** `backend/app/pipeline.py:165–179`; `backend/app/renderer.py:110`. Renderer renames `.partial` to the stable job output before ownership validation. Two generations use identical intermediate/final names. If A loses its lease while rendering, B can publish; A subsequently fails `_ensure_owned()` and unlinks every Path in its result, including B's file. This is a concrete interleaving, not an executed race in this review. Use generation-specific job/staging paths, validate there, then publish only via fenced transaction/protocol; old generation cleanup must only touch its private namespace. Do not remove a shared published path upon LeaseLost. Reconcile crash-after-file-publication with deterministic artifact uniqueness rather than inserting duplicate Artifact records on every retry.

3. **Q03 P1 — Cancel no longer reaches the actual independent supervisor.** `backend/app/api.py:211`; `backend/app/service.py:106`; `backend/supervisor.py:32`. API imports a different in-process `supervisor` object from the queue-owning process. Its `_children` is empty, `cancel()` returns False, and API only sets CANCELLED without invalidating the lease. Reproduced status CANCELLED with the old lease retained. NLLB/TTS/render may continue until whole-stage boundary; a hung model consumes the sole queue slot for up to the two-hour watchdog. Minimal correction: persist a cancellation request, have the owning dispatcher poll it and terminate its owned process tree promptly, then acknowledge cancellation and release lease. Expose requested versus stopped states, or ensure UI does not claim stopped before termination. Per-unit cancellation checks and deadlines are still needed.

4. **Q04 P1 — Restart capacity ignores live workers; surviving hung workers lose watchdog coverage.** `backend/app/service.py:38`, `:51`, `:69`, `:87`. New supervisor has empty `_children` and `_started_at`; an old worker remains alive by design. Reproduced `_claim_next()` claiming another queued job while a fresh RUNNING lease exists. Singleton protects supervisor instances, not surviving children. A surviving hung inference keeps heartbeat fresh and has no entry in the new supervisor's watchdog, so it can remain RUNNING forever. Minimal correction: persist process identity/start token with lease, reconcile survivors on startup (adopt or terminate before reclaim), and count all live durable owners for capacity. Do not treat heartbeat alone as useful progress. Test kill supervisor while worker runs, restart, then cancel and trigger timeout.

5. **Q05 P1 — ASR resume loses detected language.** `backend/app/pipeline.py:113–127`. After ASR writes segments, pause or crash before translation of auto-detected Chinese/Japanese. Resume takes `existing` branch, sets detected_language only from manual config (None), and falls back to `en`. It reuses source text but supplies the wrong NLLB language code. Persist detected language alongside checkpoint or reload it from validated Whisper JSON; keep per-segment/chunk language for multi-language input. Test ASR complete → crash → resumed translator receives `zh`/`ja`, never fallback English.

6. **Q06 P1 — Paused/queued jobs remain editable while their worker owns a snapshot.** `backend/app/api.py:232–237`; `backend/app/pipeline.py:140`. PAUSED is set immediately while worker continues until a boundary. Editor can change segments meanwhile; long worker session may later overwrite them. QUEUED can race the scheduler claim. Optional expected revision and read-then-write checks do not enforce optimistic concurrency across two requests. Restrict edits to DRAFT without an active lease; atomically freeze DRAFT to QUEUED. Require expected revision and conditional updates in the transaction. Preserve explicit reading_text: pipeline currently overwrites every saved pronunciation-specific reading with translated_text.

Other concrete reliability concern: pause then immediate resume can restore QUEUED before the old worker observes PAUSED; the worker does not reject QUEUED in `_ensure_owned()`, so it continues rather than actually stopping. Introduce requested pause/acknowledged pause, release ownership at acknowledgment, and only resume acknowledged state. This is part of Q03/Q06 state-transition remediation.

### Re-review acceptance for this scope

Re-run the two-session fencing reproduction and assert no old-owner updates. Fault-inject generation handover around publication and assert a stale worker cannot delete or overwrite the newer artifact. Restart supervisor with a live worker and show one heavy job, cancellation and timeout still work. Resume auto-language checkpoints accurately. Concurrent editor save/queue transition must produce one winning revision or HTTP 409, never worker mutation of a draft. These tests should precede any unattended-runtime acceptance; 24-hour soak and reboot remain unverified here.

## Q01–Q06 follow-up, 2026-09-10 (not full/final review)

Executed `.venv/Scripts/python.exe -m pytest .runtime/astra_queue_regressions.py -q`: the original nine tests now pass. Two new adverse-condition regressions fail, giving **9 passed / 2 failed** at this snapshot. No real model or OS process is launched by this harness. Root reports separate real E2E and API-listener-restart/cancellation success; those do not exercise the races below.

- **Q01 partially resolved:** stale progress writes are fenced by SQL UPDATE and the original reproduction passes. However ASR chunk DELETE (`pipeline.py:183`), chunk segment/config commits (`:203`, `:212`), translation commit (`:231`) and TTS commit (`:249`) are outside a lease-guarded transaction. The later `_stage` guard cannot undo a prior commit. Fence every checkpoint mutation before committing; progress-only regression does not establish checkpoint ownership.
- **Q02 remains P1 for cancellation/publication:** `_lease_guard` (`pipeline.py:34`) and final UPDATE (`:279`) require the lease only. API cancellation intentionally keeps that lease while recording CANCELLING. If cancellation commits after the final `_ensure_owned` but before publication, completion can still overwrite CANCELLING and publish artifacts. New `test_publication_guard_rejects_accepted_cancel` reproduces guard acceptance. Require RUNNING and no pause/cancel request in the atomic publication predicate and its same-transaction artifact writes. Pause has the analogous race. Generation suffix improves final-path isolation, but only uses eight suffix characters; use the full generation identity/hash rather than truncating uniqueness.
- **Q03 substantially improved:** durable cancellation/control acknowledgment and pause-before-resume regressions pass. Root's real cancellation test is additional positive evidence. Failure-path cancellation/publication remains covered by Q02.
- **Q04 remains P1 on failed termination:** persistent capacity, PID and stage/worker deadline metadata improve restart handling. Yet `_recover_stale` (`service.py:52–61`) ignores `_terminate_job_process=False` and releases/requeues the owner anyway. New `test_recovery_does_not_release_owner_if_termination_failed` reproduces this with a stubbed failed termination, without touching a process. It can start a second generation while the old one still executes. Distinguish confirmed exited/terminated from uncertain/failed; retain ownership/capacity or explicit blocked recovery until confirmed. Shared `jobs/{job_id}` source/ASR/TTS/voice files (`pipeline.py:140`) then make the duplicate-generation race a corruption risk even with distinct final MP4 names. Use private generation write paths and validated immutable checkpoint references. Recovery and reaping must also condition updates on the observed generation, rather than modifying a row selected before process termination.
- **Q05 improved in code:** detected_language now persists and reloads, and chunk checkpoints were introduced. This reviewer has not run a real pause/resume language test. Chunk implementation is actively changing; per-chunk language and overlap fidelity are outside this bounded re-review.
- **Q06 partially resolved:** mandatory expected revision, unowned-DRAFT editing and freeze-after-queue tests pass sequentially; explicit reading_text is preserved. `api.py:322–343` still uses read/check/ORM-write rather than atomic conditional revision/status writes. Two concurrent requests may both observe a DRAFT revision and succeed, or a save can race queue transition. Need concurrent barrier tests and transactional CAS before closing Q06; nine sequential passes are insufficient.

These findings were sent to Luna and root with minimal fixes. Final acceptance remains pending; no soak/reboot claim is made.

### Deterministic Q06 regressions added (2026-09-10)

Latest harness run: **11 passed / 2 failed**. Luna's intervening edits make the publication-control and failed-termination checks pass. The two new failures are `test_draft_write_rechecks_database_after_concurrent_commit[save]` and `[queue]`.

Each test loads actual Job/Segment rows into request B's Session with strong references, commits request A's save or queue operation using the ordinary handler, then resumes B's patch handler with that retained session. Both stale writes succeed instead of returning 409. This deterministically models the real gap between B's SELECT and UPDATE: no database values or application decisions are mocked; only the session factory is redirected to the already-reading request. The current SQLite configuration allows A's commit during that gap. A database conditional revision/status update must reject B even if its ORM identities are stale.

This supersedes the previous numeric harness count only; it does not establish whole-app or complete Q01–Q06 acceptance.

## Candidate-wide checkpoint review (2026-09-10)

**Release verdict: remaining correctness fixes and acceptance work; not approved for unattended 24/7 use yet.** Reviewed the stable source/script/README/test checkpoint after Luna paused. Latest reviewer execution remains the isolated 13-case harness (11 pass, Q06 save/save and save/queue fail). No model, port 8765, startup registration, reboot or soak was run during this pass. Root-reported mode/media/real-model/lifecycle checks are useful positive evidence, not substitutes for the outstanding fault cases.

### New or sharpened candidate findings

**C01 P1 — Chunked-ASR editor children discard edits.** `api.py:create_draft/rerender` copies `chunk_index` and `asr_completed_chunks` into child but does not copy StageRun. `pipeline.py:185–191` requires child StageRun COMPLETED to skip a chunk; it therefore deletes the copied segments and re-ASRs. The resulting render loses the user's edited translations for all new chunked jobs. Separately, draft creation accepts RUNNING parents; legacy rows with `chunk_index=None` and only partial ASR can become `legacy_snapshot` and skip remaining recognition. Fix with an explicit immutable, complete ASR snapshot for editor children, bypassing source inference; reject draft/rerender snapshotting until the parent has a complete consistent transcript. Add child-render tests with at least two chunks and a distinctive edited sentence.

**C02 P1 — Recovery checkpoints still mutable without lease fencing.** Q01 checkpoint commits and shared per-job WAV/ASR/TTS intermediate files remain as previously detailed. Stage guarding after a commit does not fence that commit. Old worker handover/failure must not delete or overwrite new-generation source segments or config. Retain private write paths and hash-validated immutable checkpoints; fault-inject handover during each commit.

**C03 P1 — Incomplete upload finalization recovery.** `api.py:complete_upload` persists COMPLETING before copy/job creation, then commits Source/Job separately before recording completed IDs. Process death leaves upload stuck COMPLETING until 24h cleanup, possibly with an already queued job; retry cannot retrieve the accepted job. A concurrent chunk request can pass its UPLOADING check before completion claim and mutate while hashing/copying. Serialize chunk writes with finalization, record durable upload→source/job identity in a recoverable transaction, reconcile COMPLETING at startup. Sequential idempotence tests do not cover this window.

**C04 P2 — Cache/runtime contract incomplete.** `backend/manifest.py` hashes only direct files, skipping NLLB directory contents, Whisper DLL dependencies and font directory. `scripts/setup.ps1` points into Downloads and the unrelated source model project rather than importing full runtime. `tts.py:synthesize` keys cache by model path+mtime+text, omitting config/content hash, and writes cache directly; changed Piper config or crash during cache write can produce wrong/stuck cache. Implement manifest of every model/runtime input and atomic validated cache keyed by it.

**C05 P2 — Continuous operation fills disk and hides diagnostics.** Maintenance only expires uploads/backups. No intermediate/cache 7/14-day cleanup, bounded process logs or persisted watchdog errors; supervisor routes stdout/stderr to DEVNULL and catches loop exceptions silently. Disk check only occurs before claim against data root, not upload/output growth, and returns queued indefinitely without WAITING_RESOURCES explanation. Include output filesystem reserve and ongoing checks; add inspectable failures and reference-aware cache retention. Never delete retained final outputs to meet reserve.

**C06 P2 — Runtime settings do not mean complete job snapshots.** Six-thread control reaches Whisper only: NLLB never calls torch.set_num_threads and Piper session threads are default. Offline defaults exist, local_files_only used (positive). Persisted global settings can still change worker behavior for queued jobs because Job config contains modes rather than full resource/model snapshot. Changing output_root can make old artifacts inaccessible because download confinement uses only current root; PAUSING/CANCELLING are omitted from active-path-change guard. Use artifact-specific trusted roots and complete job snapshots.

### Superseding status table

`Closed` below closes the named original defect only, not the entire feature. `Retest` means inspection suggests correction or root reports success but reviewer acceptance is incomplete.

| Baseline | Current status | Evidence / remaining gate |
|---|---|---|
| R01 Piper writer/muxer | Closed | wave.open + explicit wav muxer; root real-model/E2E pass. |
| R02 subtitle seconds/text loss | Closed | Correct clock/preservation; committed unit tests and root rendering evidence. |
| R03 immutable editor | Remaining | C01 and Q06; edited chunked child not protected. |
| R04 leases/restart | Remaining | Q01 checkpoint writes/C02; original stale progress regression now passes. |
| R05 retry/watchdog | Retest/remaining | Persisted deadlines/retries added; fault races and process-tree cleanup need gates. |
| R06 track/offset | Retest | Root direct render/extraction fixtures pass; common nonzero/negative origins not exhaustively tested. |
| R07 output modes | Closed for basic modes | include_dubbing and mode UI present, root 8-mode pass; optional original secondary track missing. |
| R08 upload | Remaining | Empty chunks/repeated completion repaired; C03 crash/concurrent finalization. |
| R09 Whisper language | Closed | Explicit auto and result.language parser test; resume language separately Q05. |
| R10 translation | Retest/partial | Unsupported mapping fails, CJK splitting/empty/length checks added; repetition/split-retry/per-chunk language incomplete. |
| R11 watcher | Retest/partial | Stable UTC, hash/copy checks and preset change handling improved; reparse/lock races need tests. |
| R12 settings | Remaining | Persistence added; C06 snapshots/old output access. |
| R13 install/runtime | Remaining | AI install/exit checks added; C04 full managed manifest and clean-checkout reproducibility. |
| R14 slot/timing | Retest | Measured duration added; accelerated branch still trims without padding, no full no-word-loss evidence. |
| R15 output validation | Retest/partial | Full decode command/soft stream check added; verify FFmpeg error exit behavior (`-xerror` absent), original secondary track and copy optimization missing. |
| R16 Origin | Closed for prefix bug | Parsed host/port replaces startswith; root spoofed-Origin rejection should be retained. |
| R17 UI | Retest | Controls/download/watch UI improved; browser E2E still required. |
| R18 Windows runtime | Retest/partial | Independent supervisor present; root listener restart/cancel pass. No Job Objects, pre-login/reboot or sleep policy acceptance. |
| Q01 stale writes | Remaining | Progress passes; checkpoint writes C02. |
| Q02 publication/control | Retest | New accepted-cancel guard regression passes after fix; private intermediates/crash reconciliation still outstanding. |
| Q03 control acknowledgment | Retest | Sequential tests and root actual cancellation pass; publication/pause fault races not exhausted. |
| Q04 surviving owners | Retest | Durable capacity/deadlines/PID checks and failed-termination test pass; real supervisor death/restart+timeout cleanup needed (API listener restart differs). |
| Q05 resumed language | Retest | Persisted detected_language added; resume/chunk-specific model language not executed here. |
| Q06 revision concurrency | Remaining | Both deterministic concurrent interleavings fail 409 expectation. |

### Remaining contract/acceptance gates

Before claiming plan completion: fix C01/C02/C03/Q06, run edited multi-chunk child end-to-end and crash/recovery tests, import/manifest runtime completely, and preserve deterministic regressions in tracked tests rather than only ignored `.runtime`. Current tracked suite has four small tests; it is not the planned fault/media/API/browser acceptance matrix.

Still unimplemented or partial by inspection: silence-boundary ASR cuts with overlap/dedup (current cuts fixed 60s), per-chunk language routing, unit NLLB/Piper deadlines and durable unit checkpoints, Preset CRUD/priority endpoint, two-concurrent resumable browser uploads, original secondary audio track, output transcript/report artifact, reference-aware retention and bounded logs, full CPU/resource enforcement, AC anti-sleep, complete runtime locks/import. These require implementation or explicit user-approved scope changes; do not silently equate a working short-clip smoke with the whole accepted contract.

README correctly says no soak/reboot claim, but says setup installs a tested Python version (it chooses an existing interpreter) and says workers are safely reclaimed without qualifying remaining gates. `stop.ps1` taskkills the supervisor tree but reports workers remain; correct that message. Pre-login Task Scheduler permissions/account identity and clean setup need the root's runtime checks. 24-hour soak and actual reboot/pre-login remain **unverified**.

## Bounded C01/C02/Q06 verification after Luna fixes

Reviewer ran `.venv/Scripts/python.exe -m pytest .runtime/astra_queue_regressions.py tests/test_editor_concurrency.py -q`: original 13 Astra cases plus 2 tracked editor tests **all pass**. Added a partial-parent snapshot regression; resulting total **15 pass / 1 fail**. No models or application listener were started.

- **Q06 closed for reported save/save and save/queue races.** Segment UPDATE now includes current database revision and eligible unowned-DRAFT subquery, with rollback if any member fails. Draft queue transition uses conditional UPDATE. Both deterministic retained-session reproductions and tracked tests pass.
- **C02 code correction verified; fault retest remains.** ASR delete/config/segment commits and translation/TTS commits now invoke a lease guard in the write transaction. Working media is separated under generation directories. This addresses the concrete previously unguarded writes/shared intermediate paths. Real process death/handover during these commits has not been executed by this reviewer; global TTS cache atomicity remains C04 outside this scope.
- **C01 partially resolved.** Child ASR StageRun records are copied, so a complete chunked transcript child can skip recognition and preserve edits; root is exercising real edited-child output separately. **Still remaining P1:** `create_draft` and non-DRAFT `rerender` accept RUNNING/incomplete parents without a completeness/status guard. New `test_running_partial_parent_cannot_be_snapshotted_as_editor_draft` fails (creation succeeds instead of 409). Legacy partial rows can take the legacy-snapshot shortcut and omit remaining speech. A chunked partial snapshot later completes ASR but if any translation is blank, the translation stage rewrites every row, including edited ones. Reject active/incomplete parent snapshotting atomically or implement an explicit complete immutable snapshot protocol before exposing editing. This is not a demand that every RUNNING status always be invalid; a design that atomically proves complete immutable transcript ownership could also satisfy it. Current code has no such proof.

Latest result supersedes prior Q06 failures; it does not close C03/upload, packaging, broader contract or soak/reboot acceptance.

## C02 deterministic transaction handover evidence

Added and executed five tests using the actual Pipeline lease guard and actual SQLite/SQLAlchemy sessions, with no model or worker process:

` .venv/Scripts/python.exe -m pytest .runtime/astra_queue_regressions.py -q -k checkpoint ` → **5 passed**.

Four parameterized cases retain the old Job/Segment identity, hand the lease to another session, then attempt the checkpoint transaction for ASR deletion, ASR segment/config insertion, translation dirty-state flush and TTS dirty-state flush. The guard raises LeaseLost, rolls back pending writes, and preserves the new lease and original rows. The fifth acquires the guard, attempts generation handover through an independent SQLite connection with zero busy timeout, verifies it is blocked by the writer lock, commits the guarded checkpoint, and confirms handover can then succeed. This tests both sides of the handover boundary rather than only cached ORM lookup behavior.

Inspection of current pipeline confirms guards precede ASR delete, ASR insert/config commit, chunk normalization, translation and TTS commits (`pipeline.py:246,258,284,312,330` at this read). The dirty translation/TTS state is safe against automatic premature flush because SessionLocal is configured autoflush=False; guard success holds the writer transaction until commit. These regressions exercise the actual transaction primitive and representative dirty state; they do not run the entire inference pipeline or prove that a future new call site uses the primitive correctly.

**C02 status: reported stale-checkpoint transaction defect closed by inspection plus deterministic transaction tests.** Generation-private media workspaces address the reported shared intermediate writes by inspection. Unexecuted acceptance paths remain real process death during file write/rename, on-disk corruption/hash reconciliation, global TTS cache atomicity, and actual supervisor kill/restart while a media process survives. No conclusion about those broader paths, C01 or C03/C04 is implied by these five passes.

## C03 upload crash/lock follow-up

Executed `.venv/Scripts/python.exe -m pytest .runtime/astra_queue_regressions.py -q -k upload_ --tb=no`: **2 pass / 3 fail**. Each database is isolated; fake media bytes are used only for source bookkeeping (no FFmpeg or AI). A second short-lived Python process tests the actual OS lock, not a mocked threading lock. No API listener is opened.

Positive evidence: (1) second process waits while parent owns upload lock and acquires after release; (2) upload older than 15 minutes, already renamed with temp_path committed and Source/Job created, successfully reuses exactly one Job by upload_id.

Remaining failures:

- **U01 P1: rename-before-metadata crash cannot recover.** Fixture physically renames `{id}.partial` to `{id}.mp4`, leaves Upload.temp_path on old name and COMPLETING older than 15 minutes, then calls ordinary complete_upload. It raises HTTP 422 `upload temporary file is unavailable`. Retrying repeats indefinitely because code checks old path existence before checking deterministic rename destination. Recover under OS lock by reconciling the expected destination with persisted content identity/size/checksum; do not require the old name after a committed rename.
- **U02 P2: conflicting retry preset is silently ignored.** Fixture has existing soft-subtitle Job from interrupted finalization; retry requests burn. It succeeds but returns old soft Job. Preserve normalized preset/checksum in durable completion claim and either return the original accepted contract explicitly or reject changed completion parameters with 409. Current API silently accepts settings it does not apply. Regression asserts rejection; an explicitly documented idempotent-original response is an alternative if UI displays that contract.
- **U03 P2: dead-finalizer recovery delayed 15 minutes unnecessarily.** Fixture has a durable upload Job, free OS lock and recent COMPLETING timestamp. Retry returns409 until age15min. A live finalizer cannot coexist inside the same exclusive lock, so after acquisition the timestamp alone is not evidence of live work. Reconcile existing Job immediately; use persisted claim/files to recover other interrupted windows. No automatic startup reconciliation was found in this snapshot; unattended clients may abandon this accepted video unless they retry later.

The lock implementation rechecks chunk status while holding the same ID lock (positive). Its Windows `LK_LOCK` has finite retry behavior; the brief cross-process test does not prove long hashing/copy contention returns a controlled API response. Long lock wait inside async put_chunk also blocks that event-loop thread; move blocking lock/file work off the event loop or return retryable busy response. Lock files opened `a+b` append a byte on every acquisition, so they grow with chunk/retry count; not the current P1 blocker.

Tests live in `.runtime/astra_queue_regressions.py`. Findings concern this partial C03 snapshot, not Luna's concurrently changing timeline/C01 or packaging. No source edits were made.

## Timeline/C01/C03/R15 bounded re-review

Reviewer execution: Astra harness **24 passed / 3 failed**. Tracked timeline tests **3 passed** when run with workspace `--basetemp=.runtime/astra-timeline-qa-20260910`; default pytest temporary directory returned Windows AccessDenied, an environment issue rather than app failure. No media/model/listener run in this review.

- **C03 U01/U02/U03 closed for reported reproductions:** all five upload regressions pass, covering rename-before-temp_path commit, durable-job reuse, conflicting preset rejection, recent-dead-finalizer recovery and real cross-process OS locking. Recovery is triggered by completion retry; no automatic startup sweep was proven. Long-contention/event-loop behavior remains untested.
- **C01 still open on second endpoint:** create_draft now rejects active parents; non-DRAFT `rerender` still accepts RUNNING and creates partial child (`api.py:425–448` at inspection). New test `test_running_parent_cannot_be_rerendered_as_partial_snapshot` fails. Apply the completed-unowned-parent gate consistently to both snapshot entrypoints.
- **T01 P1, proven legitimate word loss:** normalization deduplicates text across disjoint fixed 60-second windows without requiring overlapping timestamps or overlapping source audio. New test with first cue `yes` ending at60000 and second `yes please` starting at60000 loses the second spoken `yes`. Real speakers can repeat across a boundary; text equality alone cannot establish duplication. Preserve words unless supported by actual overlapping audio/timestamps; clamping impossible timestamps can remain independent of text deletion.
- **T02 P1, proven token-index mismatch:** `_overlap_words` counts regex word tokens, while trimming/deletion counts whitespace tokens. `well-being` matches two regex tokens; following `well-being matters` has two whitespace tokens, so normalization deletes that entire cue, including unrelated `matters`. New parameterized normalization regression fails. Use consistent token spans for comparison and removal, preserving unmatched source bytes. Even after fixing indices, T01 evidence requirement remains.
- **R15 inspection improved:** full-decode validation includes `-xerror -err_detect explode`. Root reports corruption rejection; reviewer did not rerun media corruption here.

Root reports the actual66s recovery test passed in42.312s with0 ASR calls, one changed translation input, one Piper load, two retained chunks and valid MP4 (`.runtime/root-long-resume-result.json`). That is useful evidence for timestamp-clamp/reuse behavior; it does not invalidate adversarial legitimate repetition tests. Edited-child real test was still running when this note was written.

## Confirmation of immediate timeline/C01 fixes

Reviewer reran Astra harness: **27/27 passed**. The previously failing active-parent rerender, disjoint-chunk repeated-word and hyphen-token regressions now pass. Inspection confirms dedup requires raw timestamp overlap across chunks and comparison uses whitespace tokens with edge punctuation normalization; the reported `yes` and `well-being matters` losses are closed. This does not prove arbitrary ASR timestamp overrun is reliable evidence of duplicate speech; overlapping-window quality remains an acceptance consideration.

C01's active-parent entrypoint gap is closed for both create_draft and rerender regressions. Root's actual edited-child result was read from `.runtime/root-long-edit-result.json`: root reports9.011s, zero ASR calls, zero translation calls, one Piper load, unchanged parent snapshot and valid66s MP4. It provides real-path evidence that complete edited child snapshots are retained and reused. The execution itself belongs to root, not this reviewer.

C03's five previously added upload regressions remain included in the27 passing tests. No new model/listener run occurred here. This closes the reported three immediate failures, not the broader24-hour/reboot acceptance or unrelated pending cache/packaging work.

## Managed runtime and upload UI checkpoint review

Reviewer ran `tests/test_packaging.py` with workspace basetemp: **3 passed**. Inspected runtime_import/manifest/setup/dependency lock and frontend UploadButton/upload helpers/App lifecycle. No browser, model or8765 listener was started. Root reports repeat managed setup and65 SHA checks with original-model baseline intact; root browser tests verify two-file25MiB reload/resume, server ACK skipping, reused IDs and full hash matching.

Positive implementation evidence: incremental full-file SHA256 with8MiB reads; persisted fingerprint includes full content digest and preset; resumed upload fetches server ACK state; per-component queue caps workers at2; server filename/size comparison rejects mismatched sessions. Managed importer hashes copied files and complete directory trees, uses fsync/temp rename, rechecks source tree, supports interrupted directory swaps, and manifest includes NLLB, fonts and Whisper DLL tree. This addresses the original incomplete-file-only manifest finding.

**UI01 P2 — Component navigation breaks global concurrency/cancel ownership.** `App.tsx` conditionally mounts UploadButton only on queue view. UploadButton unmount cleanup only removes storage listener; it does not abort requests or pending hashes/queue. Switching to Settings while two files upload leaves those tasks running invisibly. Returning mounts new refs with runningWorkers=0, permitting two more files and losing access to cancellation of old rows. Keep a long-lived upload controller mounted across views (preferred for continued background work) or explicitly stop/reconcile on unmount. This is a deterministic lifecycle path established by inspection, not browser-executed in this review.

**UI02 P2 — Cancel during completion is ambiguous.** AbortController stops waiting for HTTP completion, not the synchronous server finalization. UI can report cancelled while a job was already enqueued. Persist/reconcile session after abort and show pending confirmation; if completed, display accepted job and allow explicit job cancel. Do not equate cancelled fetch with cancelled processing. The existing note that server session remains is helpful but does not reveal already-created job.

**UI03 P2 — Stored sessions lack structural validation.** readUploadSessions only validates Array; malformed/stale localStorage entries can throw during presetKey/fingerprint matching. Server filename/size check uses persisted session metadata, and server status omits expected hash; successful completion hash protects ordinary resume, but corrupted session metadata should be rejected/removed before use. Add schema checks for ID, hex digest, positive finite chunk size, matching file size/fingerprint and known preset values. Cross-tab localStorage read-modify-write can lose another tab's session; per-component concurrency limit likewise is not cross-tab global. These are local resilience issues, not demonstrated remote vulnerabilities.

**PK01 P2 — Import has no mutual exclusion or idle-runtime gate.** _cleanup_staging_directories deletes every matching staging tree, so two simultaneous setup/import runs can delete each other's active staging. Per-file atomicity is sound but full runtime update is multiple swaps; a running inference can observe mismatched model/config versions during setup. Acquire importer lock, and stop/wait for runtime ownership before replacing assets. Tests cover repeat single-import behavior, not simultaneous update or active inference.

**PK02 P2 — Claimed reparse rejection is incomplete.** _require_directory/_require_file resolve before checking link status, losing the original root link; destination containment is lexical and does not reject junctions in existing ancestors. This may redirect managed writes outside intended physical root if local directories were relinked. Validate original path and every existing ancestor under canonical root without breaking documented Windows redirection. No malicious-link exploit executed here.

Whole-runtime SHA evidence and exact dependency versions are positive; do not conflate version pinning with wheel content hash locking. The source/model promotion race is under another agent's active repair and was not re-reviewed here. Pipeline/service and cache were intentionally outside this checkpoint scope. No24h/reboot acceptance is implied.

## 2026-09-11 independent checkpoint: TTS, deadlines, uploads, UI and importer

This is a bounded review of files changing during implementation. Application files were not edited by this reviewer. New adversarial harnesses and reports are under `.runtime/astra*`; this document records the actual observed results rather than assuming subsequent fixes pass. No model or API listener was started. Two tiny owned Python stall processes were started and terminated specifically to exercise the real Windows watchdog path.

### Executed baseline and positive evidence

Existing suites, run in separate Python processes with distinct workspace `--basetemp`, passed: core/timeline/TTS **12**, editor **3**, concurrent same-blob upload **1**, packaging **3**, existing Astra queue/fencing harness **27**. XML reports are `.runtime/astra-current-{core,editor,upload,packaging,regression}.xml`. Do not collect the entire ignored `.runtime` tree as one suite: it contains independent harnesses with import-time database configuration. The old Astra harness now explicitly creates its temporary database below workspace `.runtime`, avoiding global Windows TEMP redirection. This changes only the review harness.

- **TTS cache C04 correction verified for the tested paths.** Full text, model SHA-256 and config SHA-256 now form cache identity; synthesis and cache copies use sibling partial files and atomic replacement. Existing cache tests verify config/model changes, valid cache reuse without voice invocation, corrupt short-cache regeneration and absence of leftover partials. NLLB applies Torch intra-op threads and one inter-op thread; Piper creates an ONNX Runtime session with the configured intra-op threads, one inter-op thread and sequential CPU execution. Those CPU settings were inspected, not profiled for total process thread count.
- **TTS unit durability passes.** A fake-inference Pipeline run successfully writes unit0, injects failure in unit1, and then reads the real database. Unit0 retains its existing WAV and COMPLETED `StageRun`; unit1 has no TTS checkpoint. The pre-existing stale-lease transaction tests remain part of the27-case baseline.
- **Actual watchdog mechanism passes a bounded Windows process test.** For both `translation` and `tts`, a real owned Python process stalls with job/lease identifiers in its command line. An expired persisted stage deadline causes `QueueSupervisor._recover_stale()` to call its actual process termination path, waitable process exit occurs, and the job is returned to QUEUED with retry_count1 and the appropriate deadline error. This establishes supervisor-driven termination, not a hard interruption inside model-library calls: NLLB's own timer checks only before/after synchronous `generate`, and Piper has no internal deadline. Continuous supervisor availability remains a dependency. No model hang, child-process tree, supervisor crash or reboot was exercised by this reviewer.
- **Source promotion locking passes the tracked real two-process test.** Two independent uploads of one content hash produce one Source and two Jobs. An added same-hash/same-name case also retains distinct explicit Jobs.
- **UI01 navigation correction is present by inspection.** App keeps one UploadButton mounted across queue/settings views and switches its visible UI. Hidden-view progress/cancel controls retain the component's queue ownership. Browser navigation/cancel testing belongs to root; this reviewer did not run a browser.
- **Full-speech fitting improved by inspection and existing test.** Oversize input is tempo-rendered without `atrim`, measured, and rerendered at higher tempo if needed. Trimming is applied only after measured transformed speech fits, when padding is generated. Root's `.runtime/root-tts-managed-probe.json` reports real Piper1939ms, cache hit without loading voice, and measured300/1000/5000ms slots; `.runtime/root-production-managed-runtime-result.json` reports a41.475s managed-runtime application completion. These are root-executed results, not additional reviewer executions.

### New adversarial failures and remaining findings

The first `.runtime/astra_checkpoint_current.py` run produced **4 passed / 4 failed** (`.runtime/astra-current-adversarial.xml`). The four failures are:

1. **N01 P1 — Successful NLLB batches are not durable and the five-minute budget covers all pending translation.** Eight segments invoke the real `NLLBTranslator.translate_batch` with fake tokenizer/model dependencies. The first four outputs succeed; the second generate raises. Running the actual Pipeline leaves all eight translations blank. `pipeline.py` passes every pending segment into one translator call and commits only after it returns, with one `translation/all` deadline set before model load. It therefore repeats successful work after failure, cannot pause at a completed translation batch, and can repeatedly time out a long video even when every individual batch meets the accepted five-minute budget. Persist each completed bounded batch under the lease guard and refresh its unit deadline; separate model-load time from the per-batch contract. This is a reproducible checkpoint defect, not a claim about translation accuracy.
2. **U04 P2 — COMPLETED upload retry bypasses preset validation.** A completed soft-subtitle upload retried with burn returns the old job without409. The initial `COMPLETED` return executes before normalization/comparison; the repaired interrupted-COMPLETING path has the comparison. Move the same immutable-contract check before the completed early return, or explicitly return/display the accepted original contract. This is a distinct window from the five earlier C03 regressions.
3. **U05 P2 — Same-hash uploads lose the later filename identity.** Two Jobs created with `First name.mp4` and `Tên thứ hai.mp4` share the correct Source, but both API Job dictionaries report the first filename; output naming also reads shared Source.original_name at the reviewed snapshot. Store the per-job submitted filename while preserving shared blob deduplication, and inherit it into editor revisions. Same-name repetition and same-blob source integrity pass separately.
4. **T03 P2 — Acceleration warnings use the initial factor rather than applied factor.** A1490ms/1000ms input begins at1.49x; a measured retry raises actual tempo above1.5x but returns no warning. Root's real probe also reports applied6.7553x with warning6.46x and applied1.9675x with warning1.94x. Calculate threshold warnings after convergence from the returned tempo, including thresholds crossed only during retries.

**UI02 remains partial by inspection.** `cancelItem` now marks finalizationPending and launches reconciliation, but the original aborted `uploadItem` catch still unconditionally writes status=`cancelled`. That catch can overwrite the pending-verification state while the server remains COMPLETING. Ensure the finalization reconciliation owns the row state and the generic abort handler cannot claim completion/cancellation prematurely. The new five-poll reconciliation itself was not browser-executed here.

**UI03 remains reproduced.** `node --test .runtime/astra_upload_sessions.mjs` produced **0 passed / 9 failed**. The current helper retains null/missing records, null preset, zero chunk size, invalid digest, negative/fractional/out-of-range received indices and a fingerprint that disagrees with file size. These are pure Node calls to the real TypeScript helper with mocked localStorage, not browser tests. Reject malformed sessions before component code dereferences them; preserve valid entries.

**PK01/PK02 now have executable failure evidence.** `.runtime/astra_packaging_current.py` produced **0 passed / 3 failed** (`.runtime/astra-current-packaging-adversarial.xml`). The public importer is called twice with an event-controlled interleaving after the first file copy; the second import deletes the first import's active staging and the first raises hash-validation failure. Actual Windows junction fixtures show a source-root junction is accepted after resolution and an existing `managed/runtime` junction redirects all ten fake runtime assets into a sibling outside managed root. All fixtures and redirected targets remain inside reviewer basetemp; no user models were involved. Serialize complete public imports, validate original source paths and existing destination ancestors, and gate replacement against active runtime ownership. A concurrent-import lock alone does not establish that a running worker cannot observe a mixed model/config update.

### Acceptance limits

Do not treat this checkpoint as release approval. Real NLLB/Piper stalls, old-worker/new-generation file publication collisions, power loss during cache/DB rename, full checkpoint corruption/recovery, browser finalization races, cross-tab queue/session ownership, startup/pre-login/reboot,24-hour soak, peak resource enforcement and the remaining accepted-contract features have not been verified by these tests. An import-time `api.py` indentation error appeared during a later concurrent-edit rerun; that rerun is not counted as an application regression until a stable snapshot is available. Later implementation changes require targeted reruns before closing the named findings.

### Final targeted rerun within this checkpoint

After the API edit stabilized, `.runtime/astra_queue_regressions.py` passed **27/27** again with the workspace TEMP correction. The expanded `.runtime/astra_checkpoint_current.py` passed **9/11**; only **N01** (translation batch durability) and **T03** (final applied tempo warning) still failed. Reports: `.runtime/astra-current-regression-final.xml` and `.runtime/astra-current-adversarial-final.xml`. The temporary indentation error is no longer present.

- **U04 closed for the completed-upload preset reproduction.** The corrected completed branch rejects a changed preset; ordinary prior upload-recovery regressions remain passing.
- **U05 API filename correction passes; output naming remains partial.** Two same-hash Jobs now report their own submitted names through `_job_dict`, and same-name explicit submissions still retain distinct Jobs. The reviewed Pipeline still derives the export stem from `source.original_name`, so the second job's output directory/basename still uses the first source filename. This remaining renderer naming use was identified by inspection, not exercised through media rendering here.
- **Additional cache collision and content-identity cases pass.** Two simultaneous same-key synthesis writers leave one valid cache WAV, valid independent output WAVs and no partial files. Replacing both model/config bytes while preserving their sizes and nanosecond mtimes changes both cache digests. These use actual WAV I/O with fake voice generation and wave-based duration measurement; they do not infer real Piper waveform quality.
- **No-trim failure behavior passes.** Four deliberately nonconvergent tempo measurements produce a clear MediaError; no destination is published and none of the speech-bearing transforms includes `atrim`.

UI02/UI03 and PK01/PK02 retain the findings and executed results above; no later fixes to those files were available for this bounded review. End of checkpoint: no application files edited, no release/soak/reboot approval issued.

## N01/T03 pipeline follow-up: independent verification

After Luna's pipeline/TTS checkpoint, reviewer reran `.runtime/astra_checkpoint_current.py`: **11/11 passed**, including the previously failing successful-first-batch/failed-second-batch and1.5x-convergence-warning cases. Separately executed tracked `tests/test_pipeline_translation.py`: **2/2 passed**; `tests/test_tts_cache.py`: **4/4 passed**. Each command used a distinct workspace basetemp. Initial reports are `.runtime/astra-n01-t03.xml`, `.runtime/astra-n01-tracked.xml` and `.runtime/astra-t03-tracked.xml`.

Reviewer then added five independent fault/boundary cases to the ignored Astra harness; expanded result is **16/16 passed** (`.runtime/astra-n01-atomic.xml`):

- Lease replacement immediately after generation returns preserves the new owner and commits neither stale translation/reading text nor a COMPLETED batch checkpoint.
- Cancellation in the same position preserves CANCELLING and likewise blocks text/checkpoint publication.
- A pause requested during the first batch allows that batch's text and COMPLETED checkpoint to commit, acknowledges PAUSED, and makes no second model call.
- A fault injected after actual SQLite segment UPDATE SQL, before the StageRun UPDATE, rolls back both text and checkpoint state. The prior durable RUNNING batch remains; no partial successful text escapes the transaction.
- A partially translated eight-row timeline invokes only missing rows1/3 then5/7, preserves existing even-row translations, retains stable `batch:0`/`batch:1`, and refreshes each deadline to300s. Advancing the simulated clock250s per call proves the second batch gets its own deadline rather than inheriting the first batch's remaining50s.

**N01 closed for the reported durable-batch defect.** Inspection confirms batch grouping uses the complete ordered timeline, successful results call `_stage(..., COMPLETED)` with pending text still uncommitted, and `_stage` acquires the lease writer guard before flushing/committing text and StageRun together. SessionLocal still uses autoflush=False. Failed batches resume from missing text; earlier committed batches remain intact. The independent fault tests exercise this actual code with the real database, not a replacement transaction helper.

**T03 closed.** `_tempo_warnings` now runs after successful tempo convergence and final duration validation. Both a factor crossing1.5x only during retry and the tracked case crossing2x generate warnings using the final returned factor. The existing no-trim/nonconvergence and same-key cache-collision cases still pass. No real voice was generated in this reviewer follow-up.

Two scope details remain explicit: model loading now clears the translation stage deadline and relies on the overall worker watchdog (currently two hours), rather than a separate short model-load deadline. Also, a durable unit is currently up to four source segments; tokenizer splitting can expand that unit into multiple internal generate calls sharing its300s limit. The original long-video issue of one deadline/commit across all segments is fixed; no claim is made here that every expanded token batch has a separate durable checkpoint. Model-load hangs and expanded-long-segment timing were not injected in this pass.

The Pipeline's export stem now uses `job.display_name or job.original_name` before the shared Source fallback, addressing the inspected remaining U05 output-naming call site. This was inspected only; a media-render filename test was not added. UI/importer/feature/reliability changes outside the pipeline/TTS snapshot were not re-reviewed in this follow-up. Hashes of these three reviewed source files are saved in `.runtime/astra-n01-t03-source-hashes.json`. No application source edits, model/listener starts, or release/24-hour/reboot approval occurred.
