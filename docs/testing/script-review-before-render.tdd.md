# Script review before render — TDD evidence

## Source and user journeys

No plan file was provided. The journeys were derived from the approved implementation plan:

- As a user, I can keep the default fully automatic workflow.
- As a user, I can opt in to review and edit translated script segments before TTS and video rendering.
- As a user, I can approve exactly the revision I reviewed, including deliberately blank lines, and processing resumes once.
- As a user of an existing watch-folder preset, upgrading does not enqueue a duplicate merely because the new option defaults to false.

## RED/GREEN report

| Behavior | RED evidence | GREEN evidence |
|---|---|---|
| Preset normalization, review checkpoint, and approval marker | `pytest tests/test_script_review.py`: 9 failed, 1 passed before production changes | Targeted suite passed after implementation |
| Blank-line preservation, pause race, and legacy watch deduplication | Focused suite: 5 failed, 11 passed before fixes | Covered by final full suite |
| Approval revision belongs to the editor's own write | Focused race test returned revision 3 instead of 2 before `UPDATE ... RETURNING` | Focused race test passed after fix |
| Frontend option and review editor compile | Not available before UI implementation | `npm run build --prefix frontend` passed |

## Test specification

| # | What is guaranteed | Test target | Type | Result |
|---|---|---|---|---|
| 1 | Review is opt-in and defaults to fully automatic | `test_normalize_preset_defaults_script_review_to_fully_automatic` | Unit | PASS |
| 2 | Non-boolean review values are rejected | `test_normalize_preset_rejects_non_boolean_script_review` | Unit | PASS |
| 3 | The pipeline stops after translation and before TTS/render | `test_pipeline_stops_after_translation_for_script_review` | Integration | PASS |
| 4 | Script edits remain revision-protected while awaiting review | `test_review_checkpoint_segments_remain_editable` | Integration | PASS |
| 5 | Approval queues the same job and completes the durable review marker | `test_render_approves_review_checkpoint_and_requeues_same_job` | Integration | PASS |
| 6 | Stale or missing approval revisions cannot freeze a draft | `test_review_approval_rejects_stale_job_revision` and missing-revision coverage | API integration | PASS |
| 7 | PATCH returns the revision written by its own transaction | `test_segment_patch_returns_revision_from_its_own_write` | Concurrency integration | PASS |
| 8 | Deliberately blank reviewed lines remain blank and skip TTS | `test_approved_blank_segment_stays_blank_and_skips_tts` | Pipeline integration | PASS |
| 9 | A concurrent pause is not overwritten by the review checkpoint | pause/review race coverage in `tests/test_script_review.py` | Concurrency integration | PASS |
| 10 | Legacy watch presets missing the new false-valued key still deduplicate | `test_watch_dedup_treats_missing_legacy_review_flag_as_false` | Integration | PASS |

## Final verification

- `\.venv\Scripts\python.exe -m pytest -q --basetemp=.pytest-basetemp-script-review-final-2`: **66 passed**; two upstream TestClient/httpx deprecation warnings.
- `npm run build --prefix frontend`: **PASS** (`tsc -b` and Vite production build).
- `git diff --check`: **PASS**; only Git line-ending conversion notices were printed.

## Coverage and known gaps

The environment does not include the Python `coverage` package, so a numeric coverage percentage could not be produced without adding a dependency. All repository tests passed. Browser E2E automation is not configured in this repository; the frontend was validated by TypeScript and the production build.

Security review found no shell/path injection in this flow and confirmed the CSRF and revision fences. Low-priority pre-existing hardening opportunities remain for aggregate script-size limits and escaping ASS override syntax.

## Merge evidence

No checkpoint commits were created because the orchestration workflow requires user approval before committing. The RED/GREEN evidence above preserves the TDD sequence for a later conventional commit or squash description.
