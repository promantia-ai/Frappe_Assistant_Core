# Data-migration bugs: status

Statuses: Queued, Requirement, Development, Testing, Done, Blocked, Skipped.

## Two lanes, batches

Two developers work at the same time, each in their own lane and on their own files. Bugs that touch
the same files are fixed together as one **batch**: one requirement, one commit, one test round.

| Lane | Benches and sites | Branch | Owns |
|---|---|---|---|
| **1** | `/home/madesh/FAC` (fac.localhost + fac-b), `/home/madesh/FAC16` (fac16.localhost + fac16-b) | `fix/data-migration-bugs` | The dry run and fixes (`import_dry_run.py`, `import_fixes.py`), file intake (`import_session.py`), mapping and run (`import_mapping.py`, `import_run.py`), `import_failures.py`, the import session DocType (`fac_import_session.*`), the tests' shared helpers, and the tools that wrap them (`start_import_session`, `apply_import_preset`, `set_column_mapping`, `dry_run_import`, `start_import`, the failure tools) |
| **2** | `/home/madesh/FAC-L2` (fac-l2.localhost), `/home/madesh/FAC16-L2` (fac16-l2.localhost) | `fix/data-migration-bugs-l2` | `document_batch` and the create/update document tools, plans (`import_plan.py` and the plan tools), the opening engines and their tools, undo, sign-off, the chat UI, skills |

Lane 2 merges into lane 1's branch at the end of each phase, then `full_run.sh both` runs on lane 1.
A lane needing a file the other lane owns stops and asks the orchestrator.

## Baselines (every data-migration test module)

| When | v15 (fac.localhost) | v16 (fac16.localhost) |
|---|---|---|
| Start, da17554 | 1,052 ran, 0 failures, 0 errors | 1,052 ran, 272 failures, 201 errors (first count said 1,029: test_plan_openings prints two summaries, now added up) |
| After #34430, de2df60 | 1,054 ran, 0 failures, 0 errors | 1,054 ran, 277 failures, 43 errors (320 failing tests, was 473; no regressions) |

## Lane 1

| Batch | Bugs | Pri | What | Status | Commit |
|---|---|---|---|---|---|
| — | #34430 | High | Test suite isn't portable to Frappe v16 or to sites with India Compliance | Done | de2df60 |
| — | #34395 | High | Dry run rejects every row on Frappe 16.52, and missing-link fixes never appear | Development | |
| L2-A (moved) | #34400, #34429 | High, High | Session permissions: engine-owned fields guarded in the DocType (engine marks its own writes), for everyone incl. System Managers; plan fields refused on insert; plan picks only its owner's sessions. Requirement in `docs/bugs/L2-A/` | Requirement done | |
| L1-A | #34412 | Normal | Import tests commit fixtures into the site DB, and several can't fail | Queued | |
| L1-B | #34401, #34398, #34407, #34409 | High, High, Normal, Normal | Dry run: side effects (emails, files, Redis, locks); naming-series duplicates; failure grouping and suggestions; tree masters parent-first | Queued | |
| L1-C | #34405, #34403, #34404, #34437 | Normal, High, Normal, Normal | File intake: a session-owned CSV (survives chat retention), header row, UTF-16 and delimiters, file held only while its session is open | Queued | |
| L1-D | #34406 | Normal | File text fenced as untrusted wherever it reaches the model | Queued | |
| L1-E | #34408, #34402, #34397 | Normal, Normal, High | Mapping and run: update imports' required fields and permission; approval tied to what was checked; Frappe v16's import job id | Queued | |

## Lane 2

| Batch | Bugs | Pri | What | Status | Commit |
|---|---|---|---|---|---|
| L2-B | #34396 | High | Batch create/update: refuse customisation and DDL doctypes, real all-or-none (FAC side; AR side is Paul's) | Development | |
| L2-C | #34427, #34431 | High, Normal | Plans: files over 500 rows; approval guard compares real content; no overwrite or double start; stale running plans | Requirement done (waits for L2-B) | |
| L2-D | #34428, #34435 | High, Normal | Opening stock: batch/serial on a default v16 site (+ "Activate Serial / Batch No" tests); submit-time check; rounding in the report | Queued | |
| L2-E | #34433, #34434 | Normal, Normal | Opening balances (balancing line on the card, exact balance); opening invoices (no double posting) | Queued | |
| L2-F | #34432, #34436 | Normal, Normal | Undo with Bins and reposting records; sign-off counts unchanged rows | Queued | |
| L2-G | #34411, #34410 | Normal, High | Batch approval card (table, no hidden fields); migrate-data skill rules (AR side is Paul's) | Queued | |

## After both lanes

| Batch | Bugs | Pri | What | Status | Commit |
|---|---|---|---|---|---|
| Z | #34413, #34438 | Low, Low | Small cleanups across many files: done last, on the merged branch | Queued | |

## Decisions from the user

| Date | Bug | Decision |
|---|---|---|
| 2026-10-22 | #34430 | No CI job in #34430. The CI job (ERPNext + India Compliance on version-16) goes with #34418, and runs the data-migration modules only. |
| 2026-10-22 | #34395 | Also fix: the dry run's stand-in Item (`_stand_in`, utils/import_dry_run.py) is rejected by India Compliance for a missing HSN code; `test_import_plan.test_an_earlier_files_failing_row_still_counts_as_coming` must pass on v16. Found during #34430. |
| 2026-10-22 | #34412 | Also fix: `test_import_guardrails.test_a_validate_only_list_reaches_the_model_fenced` expects v15's email error wording; v16's `validate_email_address` quotes only the parsed address. Found during #34430. |
| 2026-10-22 | #34428 | Also fix: 5 tests on v16 fail with "Cannot enable Has Batch No as Activate Serial / Batch No for Item is disabled" (3 in test_import_opening_stock, 2 in test_import_undo). Found during #34430. |
| 2026-10-22 | all | Work in two lanes with batches of related bugs (user's choice, to save time). |
| 2026-10-22 | L2-A | Moved to lane 1, right after #34395, because the engine's own writes (lane-1 files) must be marked. Lane 2 starts with L2-B. |
| 2026-10-22 | L2-A | The guard on engine-owned session fields applies to everyone, System Managers included. |
| 2026-10-22 | L2-B | All-or-none: Frappe's `_disable_transaction_control` on v16; FAC's own commit/rollback stand-in on v15 (v15 has no flag), same outcome on both. |
| 2026-10-22 | L2-B | A controller doing a full rollback inside a batch fails the whole batch: everything undone, the record named. `utils/audit_trail.py` belongs to lane 2. |
| 2026-10-22 | L2-C | The L2-C developer may make the one declared line in `import_dry_run.py` (the size check honours the plan job's `frappe.local.fac_plan_running`), noted in dev-notes; resolved at the phase merge. |
| 2026-10-22 | L2-C | `hooks.py` scheduler line for the stale-plan sweep belongs to lane 2. |
| 2026-10-22 | L2-A | Browser check: the orchestrator creates a separate non-System-Manager Desk test user on the v16 test site that owns a session (Administrator skips permission checks). |

## Not in the queue

| Bug | Why |
|---|---|
| #34399 | Fixed on da17554 (commits 468a6771, 54a8ecea), verified by Paul |
