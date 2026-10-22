# L2-C: migration plan runs (#34427, #34431)

## Bug

| | #34427 | #34431 |
|---|---|---|
| Title | Migration plan stops at any file with more than 500 rows | Migration plan: approval guard compares stored values, and a running plan can be overwritten or started twice |
| Priority | High | Normal |
| Filed at | da175541 | da175541 |

Neither bug has notes beyond its relations: #34351 (the merged plan design) and epic #33986.

Paul's fix lists:

- **#34427:**
  - Let large files be checked inside a plan job, either through an `in_background` argument kept separate from `commit_progress` (committing progress would release the step savepoint), or through a flag that `_real_size` honours in plan jobs.
  - Add a 501-row plan test and show that it fails before the fix.
- **#34431:**
  - Require `checked_as_is(child)` in `_prepare`, and rewrite the test so it changes the mapping or the file.
  - In `start()`, read the status `for_update` and refuse unless it is Ready to Run.
  - Never let the open-plan paths overwrite Running.
  - Pass `deduplicate=True`.
  - Add a scheduled sweep that marks Stopped, with a reason, any Running plan whose job isn't alive.

The plan design is Rachana's merged one (#34351). A plan has no DocType: it lives on its first file's FAC Import Session, in `plan_settings` plus the shown fields `plan_status` and the others. All line numbers below are from `fix/data-migration-bugs-l2` at de2df60.

## Problem

### #34427: a file over 500 rows stops the plan

1. `_prepare` (`utils/import_plan.py:1143`) re-checks every file just before its import. It calls `import_fixes.check(child.name)` at **:1154**, and again at **:1183** after leave-out.
2. `import_fixes.check` (`utils/import_fixes.py:1009`) defaults to `commit_progress=False` and hands it to `dry_run.run` (`utils/import_dry_run.py:302`).
3. `dry_run.run` calls `_real_size(len(sheet_rows) - 1, in_background=commit_progress)` at **:311**.
4. `_real_size` (`import_dry_run.py:1033-1046`) raises "This file has N rows, not the number this session recorded, so it isn't checked here…" for any file over `BACKGROUND_ROWS` (500, `:56`) when `in_background` is false.
5. `_real_size` runs before the engine branch (`:315-317`). Opening stock, invoices and balances files fail the same way.
6. `_guarded` turns the `DryRunProblem` into "Stopped" with that misleading message. Running the plan again stops at the same file.

Before approval, `plan_questions` sends files over 500 rows to the background check (`_bring_up_to_date`, `import_plan.py:510-512` → `dry_run.enqueue` → `run_job` → `check(..., commit_progress=True)`). So a large file passes the questions and then fails the run. **Paul's claim holds.** No plan test has more than 2–4 rows.

**Can it be fixed from `import_plan.py` alone? No.**

- `_real_size` honours nothing but its argument. No flag the plan sets today, `frappe.local.fac_plan_running` included, reaches it.
- Passing `commit_progress=True` from `_prepare` does give `in_background=True`. It also runs `frappe.db.commit()` every 100 rows (`import_dry_run.py:343-351`, and opening invoices at `import_opening_invoices.py:770`). That releases `STEP_SAVEPOINT` (`import_plan.py:1124`) and commits a half-checked file. Paul rules it out.
- Patching `dry_run.BACKGROUND_ROWS` from the plan would be a process-wide hack. Not acceptable.
- What already exists and can be reused: `_next_file` sets `frappe.local.fac_plan_running = plan_name` (`import_plan.py:1112`) and clears it in `finally` (:1140, and `_guarded` :1055). `_next_file` runs only inside `run_job` and `continue_job`, which are always background jobs (`_enqueue`, :1004-1016; `now` only in tests).
- **Smallest lane-1 change:** `_real_size` (or the call at `run` :311) also treats the check as in the background while `frappe.local.fac_plan_running` is set. That is one line in `import_dry_run.py`. `import_fixes.check`'s signature doesn't change. See "Out of scope / needs lane 1".

### #34431.1: "changed after approval" compares two stored values

- At `_prepare` :1148-1153, `child.validated_fingerprint` is compared with `progress.approved[child]`. `start()` stored that value at :989.
- Both are stored values. A real change leaves `validated_fingerprint` alone, so the guard passes: a new file content or `source_file`, sheet, `template_options` mapping, `transformation_rules`, `target_doctype`, `import_type`, `match_key` or `excluded_rows`, made by a REST PUT or a direct DB write.
- The re-check at :1154 then stamps the new content as checked, and :1201 imports it without approval.
- `run_problem` (:954) already uses the real comparison, `import_fixes.checked_as_is` (`import_fixes.py:1027-1032`, which recomputes `dry_run.fingerprint`, `import_dry_run.py:1168-1188`). `_prepare` doesn't.
- `checked_as_is` is only *called*: **no lane-1 change is needed** for this item.
- What `fingerprint()` covers: content hash, sheet, `target_doctype`, `import_type`, `match_key`, `submit_after_import`, the checked `template_options`, `transformation_rules`, `excluded_rows` and the file's imported rows. It does **not** include `created_records`, so `create_planned` → `link_created` (`import_plan.py:284`, `import_fixes.py:956-980`) does not invalidate a file's fingerprint.
- **The impossible-state test:** `tests/test_run_migration_plan.py:274-290`, `test_a_file_changed_after_the_approval_isnt_imported`. Paul's line 272 is now line 281, which runs `frappe.db.set_value(SESSION, items, "validated_fingerprint", "changed")`. Only a re-check writes that field, so the test checks a state production can't reach. It must be rewritten (criterion 5).

### #34431.2: an open-plan path overwrites Running

- `Plan.save()` (`import_plan.py:88-107`) writes `status`, `progress`, `steps` and `masters` from memory. Only `stop_requested` is re-read under the lock (:94).
- These open-plan paths check `plan.status in OPEN` when they load the plan, then save later:
  - `plan_questions` → `open_questions` sets `plan.status = "Ready to Run" / "Planning"` and saves (:630-631) after checks that can take long.
  - `apply_answers` (:705-717).
  - `add_masters` (:245-256).
  - `plan_migration` (`plugins/core/tools/plan_migration.py:142-149`).
- When one of them overlaps a `run_migration_plan`, it writes Planning or Ready to Run, plus its stale `progress` (`approved`, `done`, `current`, `user`), over Running.
- `continue_job` (:1099) and `child_finished` (:1083) then see a status other than Running and return quietly. The plan is half-imported, and with `done` lost it can't re-run cleanly. **Confirmed.**

### #34431.3: two approvals start two runs

- `run_migration_plan` (`plugins/core/tools/run_migration_plan.py:81-103`) checks the status it loaded, without a lock. It then calls `start()` (`import_plan.py:986-1001`), which:
  - resets the stop flag;
  - `_update`s `done=[]`, `approved` and `user`;
  - `_enqueue`s `run_job` under `PLAN_JOB` without `deduplicate`.
- Two concurrent calls both pass and both enqueue, so the first file can be imported twice. **Confirmed.**
- Note: `deduplicate=True` alone doesn't close the race. With `enqueue_after_commit=True`, Frappe checks the duplicate at call time (v15 `background_jobs.py:93-99`, v16 :119-125), before either job is in Redis. **The `for_update` status check in `start()` is the real guard. `deduplicate` is the second line.**

### #34431.4: nothing ends a plan whose worker died

- The only way out is `stop_migration_plan` (`stop_migration_plan.py:60-63`). When `alive(plan)` is false it calls `_end(..., "Stopped")`.
- Nothing scheduled does the same. While the plan is Running, `imported_by_its_plan` (:159-169) keeps refusing its files' own imports (start_import and the three opening tools), and `join_problems` (:211) keeps them out of a new plan.
- `alive()` (:1019-1036) already covers:
  - the plan job;
  - the current file's Data Import job, through `import_run._job_id` (the same id FAC enqueues with, so it's consistent on v15 and v16 whatever #34397 changes);
  - the opening engines' `JOB`.
- `hooks.py` has `scheduler_events` at :148-154, extended at :424, :517-536. No import entry yet.

## Reproduces on

**Both v15 and v16.** All of it is FAC code, synced identically to the v16 mirror. The only Frappe behaviour involved, `enqueue`'s `deduplicate` and `is_job_enqueued`, is the same in shape on both (v15 `background_jobs.py:93-118,566`, v16 :119-134,676).

**Caveat for v16 runs.** On v16 every plan-run test currently fails for #34395: the dry run rejects every row ("'types.SimpleNamespace' object has no attribute '_uses_tree_aliases'"). `logs/fac16.localhost/test_run_migration_plan.log`: 25 of 28 fail. So:

- The red evidence for criteria 1, 5, 7 and 8 must be shown failing **on v15**, for the reason the bug names.
- v16 can only be green for `test_run_migration_plan` once lane 1's #34395 has merged. Until then the v16 verdict for that module compares against its current failures, and the new tests must fail on v16 only with the #34395 message.

## Acceptance criteria

"Plan job" means `run_job` / `continue_job` → `_next_file`, run as tests run them (`now=True`).

### #34427: large files in a plan

1. **A 501-row file runs through the plan.**
   - Given a plan whose one file (a simple master, no links) has 501 ready rows, checked and approved (`plan_questions` ready, `run_migration_plan` with its approval),
   - when the plan runs,
   - then `_prepare` does not stop it, the plan ends Completed, and all 501 records exist.
   - The plan's step log has no "isn't checked here" message.
2. **The re-check after leave-out works for a large file too.**
   - Given criterion 1's plan where some rows now fail at `_prepare` (failure rule `leave_out`),
   - when the plan runs,
   - then the failing rows are left out, the second check (:1183) doesn't raise for size, and the plan ends Partially Completed with the left-out count.
3. **Opening files over 500 rows aren't refused for their size in a plan.**
   - Given an opening stock, invoices or balances file of more than 500 rows in a running plan,
   - when `_prepare` re-checks it,
   - then no size refusal is raised.
   - The engine's own run may be patched to keep the test fast: what's under test is that the size gate before the engine branch lets it through.
4. **Outside a plan job, nothing changes.**
   - Given a session whose file has more than 500 rows,
   - when `import_fixes.check(name)` runs in a request (no plan job, `commit_progress=False`),
   - then it still raises the "isn't checked here" refusal.
   - And Max Rows per Import (`row_limit_problem`) still refuses an over-limit file inside a plan job.
   - And the plan's re-check commits nothing: it still runs with `commit_progress=False`, so `STEP_SAVEPOINT` is intact. A failure later in the same step rolls the file back, as `test_an_error_between_files_ends_the_plan_instead_of_leaving_it_running` shows today.

### #34431.1: approval tied to what is really there

5. **A real change after approval stops the plan before that file.**
   - Given an approved two-file plan (groups, items),
   - when, after groups is imported and before items is prepared, the items session changes without a re-check (one `subTest` each: its `template_options` mapping, written directly to the DB; its file, with `source_file` pointing to a File of different content; and its `transformation_rules`), with `validated_fingerprint` left as it was,
   - then the plan ends Stopped with "items.csv changed after the plan was approved", no Item from items.csv exists, and the items session isn't re-stamped (its `validated_fingerprint` is still the approved one).
6. **An unchanged file still runs, including one whose session gained masters.**
   - Given an approved plan whose masters `create_planned` creates and lists on a file's session (`created_records`),
   - when the plan runs,
   - then that file passes `_prepare` and is imported.
   - And the existing happy-path and leave-out tests still pass: `test_one_approval_imports_every_file_in_order`, `test_rows_whose_parent_failed_are_left_out_and_the_plan_is_partial`, `test_plan_openings`.

### #34431.2: open-plan paths never overwrite a running or ended plan

7. **`plan_questions` that overlaps the start leaves the run alone.**
   - Given a plan that is Ready to Run when `plan_questions` loads it,
   - when the plan becomes Running (through `start()`) while `plan_questions` is still working (simulated by a patch inside `open_questions`),
   - then after `plan_questions` returns:
     - the stored plan status is still Running;
     - `progress.approved`, `done`, `current` and `user` are what `start()` wrote;
     - the step log keeps `start()`'s "Approved" step;
     - the tool reply is `success: False` with a message naming the status.
8. **The same holds for the other open paths.**
   - Given the same overlap,
   - when `apply_answers` (a failure-rule answer), `add_masters` or `plan_migration` (adding a file, or `order`/`cut_off`) saves the plan,
   - then the plan's stored status, progress and steps are unchanged, and no new session is linked to the running plan.
   - The same holds when the stored status has meanwhile become one the run ends with (Stopped, Completed, Partially Completed, Failed): an open path never reopens it.
9. **The run's own writes still work.**
   - Given a Running plan,
   - when the run writes through `_update` / `_end` (`current`, `done`, `left_out`, the final status), and `stop_migration_plan` sets the stop flag,
   - then those writes land as they do now. The existing `test_stopping_*` and `test_an_error_between_files_*` tests pass unchanged.

### #34431.3: one approval, one run

10. **`start()` refuses a plan that isn't Ready to Run in the database.**
    - Given a plan object loaded while Ready to Run, whose stored status has since become Running (as a first `start()` leaves it),
    - when `start()` is called with that stale object,
    - then it raises `PlanProblem`, and nothing of the plan changes: no `done` reset, stop flag, `approved`, `user` or new step.
    - No job is enqueued (`_enqueue` / `frappe.enqueue` not called).
    - The status is read with `for_update=True`, so a concurrent second start waits for the first to commit and then refuses.
11. **A second `run_migration_plan` doesn't start a second run.**
    - Given a plan that one `run_migration_plan` call has started,
    - when a second call arrives that loaded the plan before the first committed (simulated as in 10),
    - then the reply is `success: False` and says the plan is already running, and the first file is imported once.
12. **The plan job is enqueued with deduplication.**
    - Given `start()` on a Ready to Run plan,
    - when it enqueues `run_job`,
    - then `frappe.enqueue` gets `deduplicate=True` with `job_id=PLAN_JOB.format(plan)`.
    - `continue_job`'s enqueue from `child_finished` must never be silently dropped: a dropped continue leaves the plan Running with nothing to carry it on. If deduplication is applied there too, `dev-notes.md` says why a still-STARTED `run_job` can't swallow it. `test_a_finished_files_job_hands_the_plan_to_a_job_of_its_own` keeps passing.

### #34431.4: stale running plans end

13. **The sweep ends a dead running plan.**
    - Given a Running plan for which `alive(plan)` is false,
    - when the sweep runs (as the scheduler runs it: Administrator, no request),
    - then the plan is Stopped, with a step that says no job was running it and what is imported stays.
    - Afterwards its files can join a new plan (`plan_migration`), and `start_import` no longer refuses them with `imported_by_its_plan`.
14. **The sweep leaves live and non-running plans alone.**
    - Given a Running plan whose job is alive (`alive` true), and plans in Planning, Ready to Run, Stopped and Completed,
    - when the sweep runs,
    - then none of them changes (status, progress, steps).
    - The sweep must not end a plan in the hand-off between a file's job ending and `continue_job` being queued (status committed, after-commit enqueue not yet in Redis). For example, it re-checks `alive` under the plan's row lock before ending it. Say in `dev-notes.md` how this is handled.
15. **One broken plan doesn't stop the sweep.**
    - Given two dead Running plans where ending the first raises,
    - when the sweep runs,
    - then the second is still Stopped, and the error is logged (`frappe.log_error`).
16. **The sweep is scheduled.**
    - Given the app's hooks,
    - when `frappe.get_hooks("scheduler_events")` is read,
    - then the sweep's dotted path is in it.

## Tests to add

`frappe_assistant_core/tests/test_run_migration_plan.py` (lane 2):

| Criterion | Test |
|---|---|
| 1 | `test_a_plan_imports_a_file_of_more_than_500_rows` |
| 2 | `test_a_large_files_failing_rows_are_left_out_in_the_plan` |
| 3 | `test_a_large_opening_file_isnt_refused_for_its_size_in_a_plan` |
| 5 | **Rewrite** `test_a_file_changed_after_the_approval_isnt_imported` (:274-290): change the mapping, the file and the `transformation_rules` (one `subTest` each), never `validated_fingerprint`. Name: `test_a_file_changed_after_the_approval_isnt_imported` (kept) |
| 6 | `test_a_file_whose_session_lists_the_plans_masters_still_runs` |
| 7 | `test_plan_questions_overlapping_the_start_leaves_the_run_alone` |
| 8 | `test_no_open_plan_path_overwrites_a_running_plan` (one `subTest` per path), `test_an_open_plan_path_never_reopens_an_ended_plan` |
| 10 | `test_start_refuses_a_plan_already_running_in_the_database` |
| 11 | `test_a_second_approval_doesnt_start_a_second_run` |
| 12 | `test_the_plan_job_is_enqueued_once` |
| 13 | `test_the_sweep_stops_a_running_plan_nothing_runs` |
| 14 | `test_the_sweep_leaves_live_and_idle_plans_alone` |
| 15 | `test_one_broken_plan_doesnt_stop_the_sweep` |
| 16 | `test_the_sweep_is_scheduled` |

`frappe_assistant_core/tests/test_import_plan.py` (lane 2):

| Criterion | Test |
|---|---|
| 4 | `test_a_large_file_is_still_refused_in_a_request_outside_a_plan` |

Criterion 9 has no new test: it is the existing stop and error tests, unchanged.

Notes for the developer:

- **The 501-row test (criterion 1) is the one Paul asked for.** It must use 501 real rows, not a patched `BACKGROUND_ROWS`.
  - Before approval, `plan_questions` hands a file over 500 rows to `dry_run.enqueue`, which in tests would push a real job to Redis.
  - So patch `dry_run.enqueue` and run `import_dry_run.run_job(session, user)` inline, as `test_import_fixes.py:343-361` does, under `_each_row_commits()` so its commits become savepoints. Then call `plan_questions` again until it is ready.
  - Pick a target DocType with no links and a cheap insert to keep the run short.
- **Red check.** Criteria 1, 2, 3, 5 (the rewritten test), 7, 8, 10, 11, 12, 13, 15 and 16 must fail on de2df60. Show them **on v15**, with the bug's own message: for 1, the plan Stopped with "isn't checked here"; for 5, the items imported.
  - Criteria 4, 6, 9 and 14 are regression guards and may pass already. Say so in `dev-notes.md`.
  - On v16 the plan-run tests fail first on #34395 (see "Reproduces on").

## Browser check

**No.** Nothing a user sees changes. The plan's status, step log and the refusals reach FACO as tool replies. The behaviour is server-side timing and job handling that only tests can drive.

## Out of scope / needs lane 1

**Needed from lane 1 for #34427 (one file):** `utils/import_dry_run.py`. While a plan job is running (`getattr(frappe.local, "fac_plan_running", None)` is set; `import_plan._next_file` sets it), the size gate in `_real_size` (:1033-1046) must count the check as in the background.

- Either: inside `_real_size`, `in_background = in_background or bool(getattr(frappe.local, "fac_plan_running", None))`;
- or the same at the call site, `run` :311.
- No change to `import_fixes.check` or to `commit_progress` behaviour.
- `row_limit_problem` stays as it is.
- Lane 1 is changing this file now (#34395). Who makes the edit, and when, is Open question 1.
- Paul's alternative needs **two** lane-1 files: a separate `in_background` parameter through `import_fixes.check` → `dry_run.run`, which `_prepare` passes. The flag needs one line in one file.

**Not needed from lane 1:**

- `import_fixes.checked_as_is` is only called.
- No change to the FAC Import Session DocType: the sweep finds Running plans by the existing `plan_status` field, and everything else is in `plan_settings`.

**`hooks.py`:** one `scheduler_events` entry for the sweep, for example under `cron` `"*/10 * * * *"`. The frequency is the developer's choice; say it in `dev-notes.md`.

- The file is in neither lane's list. **Recommendation: lane 2 owns this one line.** The sweep is a plan function in `import_plan.py`, and no queued lane-1 batch touches `hooks.py`. The orchestrator decides (Open question 2).
- A hooks change needs `bench --site <lane-2 sites> migrate`, so Scheduled Job Type picks it up.
- `affected.sh` then selects every data-migration module.

**Not in this batch:**

- `children()` taking only the plan owner's sessions: #34429 (L2-A, now in lane 1).
- Guarding engine-owned session fields against REST writes: #34400 (L2-A). Criterion 5 writes with `frappe.db.set_value`, which stands in for any bypass, so it stays valid after L2-A.
- Frappe v16's Data Import job id: #34397 (L1-E). The sweep must use `alive()`, and so `import_run._job_id`, not a copied id string, so it follows whatever #34397 does.
- A file session left in Importing after its own Data Import worker died: that is `import_run`'s state (lane 1). The sweep ends the plan only.
- The v16 dry-run failures that block the plan-run tests on v16: #34395 (lane 1).

## Open questions

1. **Who changes `import_dry_run.py` for #34427, and when.** Choose one:
   - lane 1 adds the one line in its #34395 commit, or right after it;
   - the L2-C developer adds it once lane 1 has committed #34395;
   - L2-C lands without criteria 1–3 and they follow at the phase merge.
   Until the line lands, criteria 1–3 can't pass in lane 2.
2. **`hooks.py` ownership** for the sweep's one scheduler line: lane 2 (recommended), or lane 1 on L2-C's behalf.
