# Remaining data-migration bugs: list and resolution

Epic #33986, Redmine project "Frappe Assistant Core". All found by Paul on `feature/data-migration`.
Paths are on that branch (the import helpers are in `frappe_assistant_core/utils/`; tools in `plugins/core/tools/`).

**Done:**
- #34430: pushed as `feature/34430-test-portability`, commit 6314701.
- #34399: already fixed on da17554 (verified by Paul).

**For every fix:**
1. Write a test that fails on the bug first.
2. Fix the code.
3. The test must pass on **v15 and v16** (v16 = Frappe/ERPNext 16.52 + India Compliance).
4. Make one commit per bug or batch, with the message `fix(import): <what> (#<id>)`.

Decisions you already made are marked **Decided**.

Order: Phase 0 first (it makes the tests trustworthy on v16), then the rest. On v16, 312 of the remaining
320 failing tests are #34395, so fix it first.

---

## Phase 0: tests you can trust

### #34395 (High): the dry run rejects every row on Frappe 16.52
*Requirement: `34395/requirement.md`. WIP: `wip/34395-dry-run-v16`.*

**Cause (v16 only):**
- (a) `import_dry_run.py` passes a `SimpleNamespace` as `self` to `Importer.insert_record` / `update_record`.
  v16 reads `self._uses_tree_aliases`, which only `Importer.before_import` sets.
- (b) v16 reports bad Link and Select values once per column (`col.warnings`, type `value_mapping`, with the
  full lists in `col.invalid_value_items`), not per row. So `_parse_problems` never produces missing-link or
  bad-option groups.
- (c) The dry run skips `process_doc`, so it misses the `data_import_providers` hook (ERPNext 16's Party
  provider for Customer and Supplier) and tree parents named by alias (Account, Cost Center, Warehouse, Employee).
- (d) `_stand_in` inserts a bare Item, which India Compliance refuses for a missing HSN code (**Decided**: in scope).

**Fix:**
1. Build a real, unsaved Data Import and `Importer`.
2. Run Frappe's own setup (the `before_import` equivalent) and `process_doc` for each row, inside the existing savepoint.
3. Read `import_file.get_warnings()`, column warnings included. Turn `value_mapping` column warnings into the
   missing-link and bad-option groups, with every row (not v16's cut-off of 6).
4. On v15, don't double-count the column warnings that repeat its row warnings.
5. Give the stand-in Item an HSN code when the site validates HSN.
6. Restore `in_import` / `mute_emails`, and save no Data Import or Data Import Log record.

**Must not change:** the dry run writes nothing; `held_back`; tree levels; cross-file planned names; v15's results.

**Done when:**
- v15 is all green.
- v16 has only 7 failures left: the 5 for #34428, 1 for #34412, and 1 pre-existing `KeyError: None` in
  `test_import_undo.test_accounts_frozen_till_falls_back_to_accounts_settings_for_every_company`.

### #34412 (Normal): import tests commit to the site DB, and several can't fail
**Fix:**
- Stop `run_job` commits in tests: patch the commit, or use `enqueue(now=True)` inside the test transaction.
- Rewrite `test_nothing_is_written` (it must prove a row reached the insert, and check jobs, files and Redis).
- Rewrite `test_held_back_*`: insert a doc whose controller does a bound-import `enqueue` and check the RQ queue length.
- Build test files through `upload_message_file`, not `File.insert()`.
- Fix `test_mapping_fixes_..._later_turn`, `test_tool_is_classified_read_only`, and `test_import_run.py`'s job-id assertion.
- **Decided, also in scope:** `test_import_guardrails.test_a_validate_only_list_reaches_the_model_fenced`. v16's
  `validate_email_address` quotes only the parsed address, so assert what both versions return.
- Optional: `test_import_opening_balances` takes 7.5 of the 19 minutes a full run takes. Share its setup (`setUpClass`).

---

## Session permissions and security

### #34400 (High) + #34429 (High): an owner can edit safety fields; any user can join someone else's plan
*Requirement: `L2-A/requirement.md` (15 criteria). Reproduces on v15 and v16.*

**Fix (option b; Decided: guard in the DocType, for everyone including System Managers):**
- In `fac_import_session.py` `before_save` / `validate`, refuse changes to engine-owned fields
  (`status`, `validated_fingerprint`, `excluded_rows`, `template_options`, `transformation_rules`,
  `data_imports`, `created_records`, the plan fields) unless a flag set only by the engine is on.
- Mark the engine's own writes with that flag: `import_mapping.py` (`load_session`/save), `import_dry_run.py`,
  `import_run.py`, `import_fixes.py`, and the session inserts in `start_import_session.py` and `apply_import_preset.py`.
- Refuse plan fields on insert unless the engine sets them (`fac_import_session.py:41-45` only restores them on update today).
- `import_plan.children()`: filter on the plan owner's `user`.
- `import_run.result_of()`: permission-check the Data Import (`resume()` already does).

**Tests:**
- The owner saves `status` / `validated_fingerprint` with `get_doc().save()` and `frappe.client.set_value`:
  refused, and `start_import` still refuses.
- Another user's session inserted into a plan is never imported.

**Browser check:**
- Create a non-System-Manager Desk test user who owns a session (Administrator skips permission checks).
- In Desk, the guarded fields can't be changed.

### #34396 (High): batch create/update skips the customisation approval and isn't all-or-none
*Requirement: `L2-B/requirement.md` (11 criteria). WIP: `wip/L2-B-34396`.*

**Fix (FAC side; the AR side is Paul's):**
- In batch mode, refuse up front every doctype in `CUSTOMISATION_DOCTYPES`, plus Inventory Dimension,
  Accounting Dimension and Service Level Agreement. The message tells the model to do those one at a time.
- Add a runtime guard: any DDL inside a batch stops it, and everything is undone.
- **Decided:** use `_disable_transaction_control` on v16, and FAC's own commit/rollback stand-in on v15.
- **Decided:** a controller calling `frappe.db.rollback()` mid-batch fails the whole batch and names the record.
- `audit_trail.py`: record the batch's target doctypes, not None.
- Single-record create/update stays unchanged.

**Tests:**
- A real exception mid-insert (not a patched `execute`).
- A DDL doctype in a batch.
- A customisation doctype in a batch.
- Swap the two permission tests that use Role for a non-customisation doctype.

### #34437 (Normal): a file used once is refused forever, across users
**Fix:**
- `import_session.holding_session`: hold a file only while its session is open (not Completed, Undone or Failed).
- Match on the File record name plus the session's `user`, not `file_url` (identical bytes from different users share a URL).
- Never show another user's session id.
- `conversation_files.py:68` uses the same check.

### #34406 (Normal): spreadsheet text reaches the model without the untrusted wrapper
**Fix:**
- Wrap every file-derived string in `wrap_untrusted` (`chat/api/_untrusted.py`):
  - `start_import_session`: columns, samples, file and sheet names;
  - `set_column_mapping`: error headers;
  - `dry_run_import`: examples, group value and message, master names.
- Cap the cell length.
- Test: an injection string in a header and in a cell comes back fenced.

### #34402 (Normal): the approval isn't tied to what was checked; the mapping can change while importing
**Fix:**
- `start_import` takes `validated_fingerprint`, shows it on the card, and refuses unless it equals the session's current fingerprint.
- Show the file name and "will be submitted" on the card.
- Refuse `set_column_mapping` / `dry_run_import` while the session is Importing, and after Completed.

### #34431 (Normal): plan guard compares stored values; a plan can be overwritten or started twice
*Requirement: `L2-C/requirement.md`, together with #34427.*

**Fix:**
- `_prepare` uses `checked_as_is(child)`.
- In `start()`, read the status `for_update` and refuse unless it's Ready to Run.
- The open-plan paths never overwrite Running.
- Pass `deduplicate=True` (the lock is the real guard).
- Add a stale-plan sweep (scheduler): Running with no live job becomes Stopped with a reason.
  **Decided:** the `hooks.py` line belongs with this fix.

**Tests:**
- Rewrite `test_a_file_changed_after_the_approval_isnt_imported` (it writes an impossible state)
  to change the mapping, the file or `transformation_rules` instead.

---

## Dry run and import correctness

### #34401 (High): the dry run still has side effects
**Fix, in `import_dry_run.held_back`:**
- Catch enqueues at the RQ `Queue` level, or force `enqueue_after_commit`, so modules that did
  `from ... import enqueue` (HRMS salary slip) are covered.
- Keep the `after_rollback` callbacks and run them after each savepoint rollback (files on disk, Redis `clear_in_redis`).
- End the transaction every N rows so naming-series `FOR UPDATE` locks are released.
- Hold back `frappe.db.rollback`.

**Test:** a doc whose controller does a bound-import `enqueue`; the RQ queue length is unchanged.

### #34398 (High): naming-series doctypes report valid rows as duplicates
**Fix:**
- `_key_field`: a field is the duplicate key only if it's the record name or marked `unique`.
- Otherwise a repeated value is a warning that never blocks the row.
- `_map` in `import_fixes` must allow it.

**Test:** Sales Orders sharing a customer.

### #34397 (High): the import job id doesn't match Frappe v16's
**Fix:**
- Use Frappe's own id (v15 `data_import::{name}`, v16 `data_import||{name}`), or call `DataImport.start_import()`.
- Pass `deduplicate=True`.
- Test against Frappe's id, not FAC's constant (`import_run.py`).

### #34407 (Normal): failure groups split by value, aren't capped, and suggestions miss naming-series masters
**Fix:**
- Replace values in messages with placeholders; group by (cause, field, normalised message) and keep the values as a list.
- Cap the groups at about 15, then "N other causes".
- `closest_names`: search the title and `search_fields` too.
- Decide on background vs request by estimated cost, not just row count.

### #34409 (Normal): missing tree masters aren't created parent-first
**Fix:**
- Allow `is_group=1` for a planned master that another planned master names as its parent.
- Sort the batch by depth.
- Show the tree on the card.

**Test:** a two-level missing chain ("Retail > Online").

### #34408 (Normal): update imports check the wrong required fields and the wrong permission
**Fix:**
- `describe_mapping`: branch on `import_type`. For updates, require the id or match key only.
- Evaluate `mandatory_depends_on` against the samples.
- `import_target_problem`: check `write` for updates.
- Apply the hidden / read-only checks to child fields too.

---

## File intake: one change fixes four bugs

### #34405 (Normal) + #34403 (High) + #34404 (Normal) (+ part of #34413)
**Fix:** at session start (a background job for large files), convert the chosen sheet **once** into a private
CSV attached to the session. Every later step reads that file. At conversion:
- **#34403:** find the header row (the first row whose non-empty cell count matches the data rows). Add
  `header_row` to `start_import_session`, and return the first 5 raw rows when unsure.
- **#34404:** decode by BOM (UTF-16 LE/BE, UTF-8), then fall back. Sniff the delimiter (comma, semicolon, tab, pipe).
  Check `uploads.py:125` by decoding a sample, not only the BOM.
- **#34405:** the session no longer depends on the chat file, which retention deletes. Drop "Chat Attachment" mode,
  so the model never has to read every row. Make headers unique.

**Tests:** a title row; UTF-16 tab-separated; semicolon; the chat file deleted mid-migration.

---

## Plans and opening data

### #34427 (High): a plan stops at any file over 500 rows
**Fix:**
- In `import_dry_run._real_size` (or its call in `run`), treat the check as background while the plan job's
  existing `frappe.local.fac_plan_running` is set. **Decided:** a one-line change allowed with the plan fix.
- Leave `commit_progress` alone (it would release the step savepoint).

**Test:** a 501-row plan file, shown failing first (on v15).

### #34428 (High): batch or serial opening stock fails on a default v16 site
**Fix:**
- `import_opening_stock.build_document`: set `use_serial_batch_fields: 1` on every row with `batch_no` / `serial_no`.
- Regex: `Row\s*#\s*(\d+)`.
- **Decided, also in scope:** the 5 v16 tests failing with "Cannot enable Has Batch No as Activate Serial / Batch No
  for Item is disabled" (3 in `test_import_opening_stock`, 2 in `test_import_undo`).

**Test:** with the Stock Setting left at 0.

### #34435 (Normal): opening stock misses submit-time refusals, and the report hides rounding
**Fix:**
- Call `doc._submit()` inside the rolled-back savepoint, as the balances module does.
- Store each row's file amount.
- Make the tolerance relative to currency precision, and flag drift per warehouse.

### #34433 (Normal): the opening balances card doesn't show the balancing line, and an imbalance is accepted
**Fix:**
- Put the balancing amount and the covered/excluded totals on the card, and match them in `check_card`.
- Round each row to currency precision, then require exact equality.

**Optional Low items:** cost centre and dimensions; several Temporary accounts; a second cut-off date; customer advances.

### #34434 (Normal): opening invoices without a number can be posted twice
**Fix:**
- Key on resolved values: party name, ISO date, and amount rounded to precision.
- Count the session's own invoices.

**Test:** partial import, then add a value map, then import again; no duplicate.

### #34432 (Normal): undo can't remove Items and Warehouses after opening stock was undone
**Fix:**
- In `import_undo.link_blockers`, treat zero-quantity Bins of a set Item or Warehouse as inside the set.
- Remove the Repost Item Valuation records the undo itself created.

**Test:** import Items and Warehouses, then opening stock, then undo both.

### #34436 (Normal): sign-off never reaches "ready" for an update with unchanged rows
**Fix:**
- `import_signoff._current_file_outcomes`: count `unchanged` as its own status, which doesn't block sign-off.
- When every row is unchanged, the session becomes Completed with "nothing to change".

---

## UI, skill and cleanups

### #34411 (Normal): the batch approval card hides fields and is unreadable for many records
**Fix (`chat/frontend/.../helpers.js`, `ApprovalCard.vue`):**
- Never drop `_`-prefixed keys the server will write; refuse them server-side instead.
- Show a compact table per doctype in a scroll container, with a header like "Create 21: 6 Customer Groups, …".
- Make child rows and long values expandable.
- Add `aria-expanded`.
- Spec tests for 100 records and for long values. Browser check.

### #34410 (High): FACO falls back to one record at a time and changes values silently
**Fix (FAC side):**
- `docs/skills/migrate_data.md`: import requests always go through the import-session tools; never fall back to
  `create_document` record by record; never substitute a user's value without asking; stop and report a tool failure.

**AR side (Paul):**
- A system-prompt rule.
- Migration not delegable in Auto mode.
- An eval.

### #34413 (Low): small cleanups
- Wrap the untranslated strings in `_()` / `__()`.
- Prefix CSV formula cells with `'` (`import_failures.py:190`).
- Add a `search_index` on `FAC Import Session.user`.
- Exclude the JSON fields from `track_changes`.
- Use the site's Max File Size.
- `_column_position`: position before text.
- Reuse the open session for the same chat and file.
- Share the message-parsing helper.
- Give the real import the same date formats as the dry run.
- Point the model at `start_import_session` for import files.
- Document the inline dev-mode import.

### #34438 (Low): smaller defects
- **Plans:** fence only the file-derived sub-fields; report partial `apply_answers` writes honestly.
- **Undo:** write the deleted labels with each batch commit; take the user from the Data Import; test without `docstatus=2` on Customer.
- **Update:** parse match keys by field type.
- **Tally XML:** clear every finished element and cap the element count.
- **Sheets:** download timeout around 60 s, or a background job.

---

## Then

**#34418:**
- Open a PR from `feature/data-migration` to `develop`.
- **Decided:** add a CI job on Frappe `version-16` + ERPNext + India Compliance that runs the data-migration modules only.
