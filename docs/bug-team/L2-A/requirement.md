# L2-A: import session permissions (#34400, #34429)

## Bug

| | #34400 | #34429 |
|---|---|---|
| Title | Import session's safety fields can be edited by its owner in Desk or over REST | Any Desk user can put a session into someone else's migration plan |
| Priority | High (security) | High (security) |
| Filed at | 00ebb857 | da175541 |

Paul's notes:

- **#34400, note of 07:51 ("Partly fixed at da175541"):** `resume()` now calls `own_import(session, name, "write")` before it loads the Data Import (935af8d4). Still open:
  - Desk User has create and write.
  - `has_permission` ignores `permission_type`.
  - `status`, `validated_fingerprint`, `excluded_rows`, `template_options`, `data_imports` and `created_records` can still be written over REST.
  - The owner can recompute the fingerprint and set Ready to Import.
- **#34400, attachment 8859:** a screenshot of the owner's session form in Desk with an active Save button and an editable User field.
- **#34429:** has the same root cause as #34400. Paul's fix list for it:
  - Refuse plan fields on insert unless the engine sets a flag.
  - Filter `children()` on the plan owner's `user`.
  - Give Desk User read-only access (#34400).
  - Add a test that another user's session put into a plan is never imported.

## Problem

### The session's fields can be written by its owner (#34400)

FAC Import Session is the engine's state:

- the dry-run verdict (`status`, `validated_fingerprint`, `validation_summary`, `excluded_rows`)
- the mapping (`template_options`, `transformation_rules`, `target_doctype`, `import_type`, `match_key`)
- the file (`source_file`, `sheets` and related fields)
- the plan (`PLAN_FIELDS`)
- the Data Import links and the undo list (child tables `data_imports` and `created_records`)
- the step log (`steps`)

Anyone with write permission can change any of it.

- `fac_import_session.json:433-439`: Desk User has `create`, `read`, `report` and `write`.
- `fac_import_session.py:77-78`: `has_permission` returns `_doc_owner_or_admin` for every `permission_type`. A has_permission hook can only take away a permission the role grants, so this alone does not grant delete. The owner keeps write and create.
- Every field in the JSON is `read_only: 1` (the User field included at HEAD). Frappe does not enforce `read_only` on save:
  - `frappe/model/document.py` checks no field's `read_only` in the save path, on v15 or v16.
  - Only permlevel > 0 fields are reset (`validate_higher_perm_levels`: v15 line 783, v16 line 1025).
  - So `frappe.client.save`, `frappe.client.set_value`, `PUT /api/resource`, Desk `savedocs` and FAC's own `update_document` tool all save whatever the owner sends.
- What a forged write does now:
  - **`status = Ready to Import` plus a recomputed `validated_fingerprint`** passes the dry-run guard in `start()` (`utils/import_run.py:120`). The fingerprint is computed by `fingerprint()` at `utils/import_dry_run.py:1168` (HEAD), from the session's own fields.
  - **`template_options`** with any fieldname skips `resolve_mapping`.
  - **`data_imports`**: `resume()` is fixed (`utils/import_run.py:179`, `own_import(..., "write")`). `result_of()` (`utils/import_run.py:454-457`) still does `frappe.get_doc("Data Import", ...)` with no ownership check. Today its callers pass a name that was already checked or newly created: `start_import.py:120` after `resume`/`start`, and `record_result` at `import_run.py:433` from the job. So this is defense-in-depth only, and it is in a lane-1 file.
  - **`created_records`** becomes undo's deletion list (`utils/import_undo.py:151`).
- Commit 82869d20 guarded only reads of `source_file` (`readable_session_file`).

### Another user's session can join a plan (#34429)

- `FACImportSession.before_save` (`fac_import_session.py:41-45`) puts `PLAN_FIELDS` back from the database only when `not self.is_new()`. On an insert, `migration_plan`, `plan_position` and the other plan fields are kept as sent.
  - B cannot insert a session with `user` = A: `has_permission` refuses it.
  - B can insert their own session with `migration_plan` = A's plan, `status = Ready to Import` and a recomputed fingerprint.
- `import_plan.children()` (`utils/import_plan.py:180-191`) selects sessions by `migration_plan` alone and never compares `user` with the plan session's `user`.
  - The plan is the first file's session itself: `get_plan`, `utils/import_plan.py:126-132`.
  - So when A runs the plan, B's file is imported under A's approval and as A.

### Why the obvious fixes need lane-1 files

The engine writes the session with a plain `session.save()` / `session.insert()` as the user, without `ignore_permissions`, in about 40 places in both lanes.

- **Fix (a): Desk User read-only.** It also breaks `load_session()` (`utils/import_mapping.py:305-321`), which requires `write`, and `load_plan()` (`utils/import_plan.py:172-177`). Every save site would need `ignore_permissions`, and most are in lane-1 files. **It does not fit the lane constraint.**
- **Permlevel 1.** `validate_higher_perm_levels` silently resets permlevel-1 fields on every save without `ignore_permissions`, including the engine's own saves. It needs the same call-site changes as (a). **It does not fit.**
- **Fix (b): a guard in the DocType controller.** Refuse changes to engine-owned fields unless the engine marked the write. **This is the fit:**
  - The rule lives in `fac_import_session.py`, which lane 2 owns.
  - The Desk User role keeps write and create, so `load_session` and the tools' inserts keep working.
  - The only lane-1 change is marking the engine's own writes at the few places where the engine loads or inserts a session. The full list is in "Out of scope / needs lane 1".
  - In-process, the engine's `session.save()` and an attacker's `frappe.get_doc(...).save()` are the same call. **No guard can tell them apart unless the engine marks its writes.** So L2-A cannot be merged and stay green unless the engine marks its writes in the lane-1 files too (see Open question 1).

## Reproduces on

**Both v15 and v16.**

- The DocType JSON, the hook and the plan code are the same on both, because the v16 mirror is synced from v15.
- Neither Frappe version enforces `read_only` on save.
- On both, permlevel resets apply only to permlevel > 0 fields (v15 `document.py:783`, v16 `document.py:1025`).

## Acceptance criteria

Terms used below:

- **Engine-owned fields:** every data field of FAC Import Session (all are `read_only` in the JSON) and every row of its child tables `steps`, `data_imports` and `created_records`.
- **Engine mark:** whatever the developer adds so that the engine's own writes pass the guard. It must not be settable from a request body, `frappe.client`, the REST API or the FAC document tools.
- **The owner:** a non-System-Manager user with the Desk User role whose `user` is on the session.

### Writes to an existing session

1. **Forged dry-run verdict is refused.**
   - Given the owner's session in status Mapped or Dry Run Failed,
   - when the owner sets `status = "Ready to Import"` and `validated_fingerprint` to the value `fingerprint()` returns, then calls `frappe.get_doc(...).save()`,
   - then the save raises (`frappe.PermissionError` or `frappe.ValidationError`), and the database still has the old `status` and `validated_fingerprint`.
2. **Start still refuses after the attempt (Paul's test).**
   - Given criterion 1's session after the refused save,
   - when the owner calls `start_import`,
   - then it refuses with the dry-run message, and no Data Import is created.
3. **Every engine-owned field is guarded.**
   - Given the owner's session,
   - when the owner changes any one engine-owned field and saves, without the engine mark,
   - then the save is refused and the stored value is unchanged.
   - This includes at least: `excluded_rows`, `template_options`, `transformation_rules`, `target_doctype`, `import_type`, `match_key`, `submit_after_import`, `validation_summary`, `validation_results`, `source_file`, `file_source`, `source_url`, `sheet`, `sheets`, `file_name`, `parent_session`, `chat_session_id`, `progress` and `user`.
4. **Child rows are guarded.**
   - Given the owner's session,
   - when the owner appends, edits or removes a row of `data_imports`, `created_records` or `steps` and saves, without the engine mark,
   - then the save is refused and the child rows in the database are unchanged.
5. **REST-style paths go through the same guard.**
   - Given the owner's session,
   - when the owner calls `frappe.client.set_value("FAC Import Session", name, "status", "Ready to Import")` and `frappe.client.save(doc_dict)` with a changed `created_records`,
   - then both are refused and the database is unchanged.
6. **FAC's document tools go through the same guard.**
   - Given the owner's session,
   - when the owner runs the `update_document` tool on it with `status` or `validated_fingerprint`,
   - then the tool reports failure and the database is unchanged.
7. **A save that changes nothing is allowed.**
   - Given the owner's session,
   - when it is saved without the engine mark and without changing any value (what Desk's Save does on an untouched form),
   - then the save does not raise.

### Inserts

8. **Plan fields are refused on insert (#34429).**
   - Given user B and user A's plan (A's first session with `plan_settings`, and `migration_plan` = itself),
   - when B calls `frappe.client.insert({"doctype": "FAC Import Session", "user": B, "migration_plan": <A's plan>, "plan_position": 1, "status": "Ready to Import", "validated_fingerprint": ...})`,
   - then the insert is refused and no session row with that `migration_plan` exists for B.
   - The same holds for each field in `PLAN_FIELDS` on its own.
9. **Other engine-owned fields are refused on insert.**
   - Given a user,
   - when they insert a session without the engine mark that sets any engine-owned field other than `user` (for example `status`, `validated_fingerprint`, `template_options`, or a `data_imports` or `created_records` row),
   - then the insert is refused.
10. **A session can't be inserted for another user.** This is already true; the test keeps it true.
    - Given user B,
    - when B inserts a session with `user` = A,
    - then the insert is refused.

### Plans and the engine

11. **A plan takes only its owner's sessions (#34429).**
    - Given A's plan and B's session whose `migration_plan` is set to A's plan directly in the database (`frappe.db.set_value`, standing in for any bypass),
    - when `import_plan.children(plan)` runs as A,
    - then B's session is not in the result. A's own sessions of that plan still are, in the same order as before.
12. **Another user's session in a plan is never imported (Paul's test).**
    - Given criterion 11's data, with B's session at `status = "Ready to Import"`,
    - when A's plan runs to its end,
    - then:
      - B's session has no new Data Import and no `created_records`, and its `status` is unchanged.
      - No record from B's file exists.
      - The plan's approval summary and progress do not count B's rows.
13. **Plan fields on update are still protected.**
    - Given a session in a plan,
    - when a stale copy of it, read before the plan moved on, is saved by the engine,
    - then the plan fields in the database keep their current values. This is the existing `before_save` behaviour.
    - And when the owner changes a plan field on save without the engine mark, the database value is unchanged.
14. **The engine still works.**
    - Given the engine mark on the engine's own loads, inserts and saves,
    - when the existing data-migration modules run,
    - then they pass as they do now: `test_import_session`, `test_import_mapping`, `test_import_dry_run`, `test_import_fixes`, `test_import_run`, `test_import_plan`, `test_run_migration_plan`, `test_plan_openings`, the opening engines, `test_import_undo` and `test_import_guardrails`.
    - Use `full_run.sh both` on the affected list, with no new failures against the STATUS.md baseline.
15. **Delete stays refused for the owner.**
    - Given the owner's session,
    - when the owner calls `frappe.delete_doc("FAC Import Session", name)`,
    - then it raises `frappe.PermissionError`.
    - Desk User has no delete; this test makes sure `has_permission` ignoring `permission_type` never grants it.

## Tests to add

New module `frappe_assistant_core/tests/test_import_session_guard.py` (lane 2). Each test runs as a non-System-Manager Desk User made in setUp, not as Administrator. Administrator skips permission checks, so a test run as Administrator can't fail.

| Criterion | Test |
|---|---|
| 1 | `test_the_owner_cannot_mark_a_session_ready_with_a_recomputed_fingerprint` |
| 2 | `test_start_import_still_refuses_after_the_owner_tries_to_mark_it_ready` |
| 3 | `test_every_engine_owned_field_refuses_a_change_from_the_owner` (one subTest per field) |
| 4 | `test_the_owner_cannot_add_a_data_import_link`, `test_the_owner_cannot_add_a_created_record`, `test_the_owner_cannot_edit_or_remove_a_step` |
| 5 | `test_frappe_client_set_value_cannot_change_the_status`, `test_frappe_client_save_cannot_change_created_records` |
| 6 | `test_update_document_cannot_change_an_import_sessions_status` |
| 7 | `test_saving_an_unchanged_session_is_allowed` |
| 8 | `test_another_user_cannot_insert_a_session_into_a_plan`, `test_each_plan_field_is_refused_on_insert` (one subTest per `PLAN_FIELDS` entry) |
| 9 | `test_an_insert_that_sets_engine_owned_fields_is_refused` |
| 10 | `test_a_session_cannot_be_inserted_for_another_user` |
| 15 | `test_the_owner_cannot_delete_a_session` |

In `frappe_assistant_core/tests/test_import_plan.py`:

| Criterion | Test |
|---|---|
| 11 | `test_children_leaves_out_another_users_session_in_the_plan` |
| 13 | `test_the_owner_cannot_change_a_plan_field_on_save` (the stale-copy half is already covered: keep that test as it is) |

In `frappe_assistant_core/tests/test_run_migration_plan.py`:

| Criterion | Test |
|---|---|
| 12 | `test_another_users_session_in_the_plan_is_never_imported` |

Criterion 14 has no new test: it is the existing modules, run with `full_run.sh`.

Red check: criteria 1, 3-6, 8, 9, 11 and 12 must fail on de2df60. Criteria 2, 7, 10 and 15 may already pass, because they are regression guards. Say so in `dev-notes.md`.

## Browser check

**Yes.** Paul's evidence is a Desk screenshot.

Prerequisites (see Open question 3):

- A non-System-Manager Desk User login on the v16 site that carries the L2-A commit. That is `fac16-l2.localhost` if lane 2's v16 bench is up; otherwise `fac16.localhost:8007` after the merge.
- One FAC Import Session owned by that user, made by a setup script and not by the tester. Use status Mapped, with one `created_records` row.

Steps:

1. Log in as that user. Open `/app/fac-import-session/<name>`. Take a screenshot.
2. Check that Status, User, Validated Fingerprint, Excluded Rows, Template Options and the Data Imports and Created Records tables are shown read-only: no input box, no "Add Row", no row delete. Take a screenshot of the plan and validation sections.
3. Click Save on the untouched form, or Ctrl+S. Check that it either is not offered or saves without an error, and that the values do not change.
4. In the browser console, run `cur_frm.set_value("status", "Ready to Import")`, then `cur_frm.save()`. Check that an error message appears. Take a screenshot.
5. Reload the page. Check that Status is still Mapped. Take a screenshot.
6. In the console, run `frappe.call("frappe.client.set_value", {doctype: "FAC Import Session", name: "<name>", fieldname: "validated_fingerprint", value: "x"})`. Check that it fails. Reload and check that the fingerprint is unchanged.

Steps 4 and 6 are attempts that must be refused, not data changes. If either one succeeds, the check fails. Report the changed value; don't try to repair it.

## Out of scope / needs lane 1

**The engine mark in lane-1 files.** The guard (criteria 1-10, 13) breaks the engine's own writes until these places mark them. These must be done together with L2-A (Open question 1):

- `utils/import_mapping.py:305-321` `load_session()`: every import tool loads its session here, then saves it through `save_mapping` (:343), `import_fixes` (:535, :980), `import_run` (:217) and `dry_run_import.py:218`.
- `utils/import_dry_run.py:286`, `:309`, `:1191`: direct `frappe.get_doc` loads, saved at :289 and :1233. Line :219 saves a session passed in from `load_session`.
- `utils/import_fixes.py:1014`: loaded, then written with `db_set`, which skips validate. Check this one only.
- `utils/import_run.py:384`, `:433`: `record_result` and the job end, saved at :423 and :450.
- Session inserts in tools that are in neither lane's list:
  - `plugins/core/tools/start_import_session.py:229`
  - `plugins/core/tools/apply_import_preset.py:499`

**`result_of()` permission check** (`utils/import_run.py:454-457`, Paul's fix bullet). Its callers pass checked or new names today. Once criterion 4 holds, a session's `data_imports` can't be forged. Adding the `own_import` check is defense-in-depth for lane 1.

**Lane-2 engine sites that need the same mark** (in scope for L2-A, listed so none is missed):

- `utils/import_plan.py`: saves at 256, 631, 703, 717, 793 and 1172, and loads in `load_plan` and `children`.
- The opening engines: `import_opening_balances.py`, `import_opening_invoices.py` and `import_opening_stock.py`, at their `session.save()` lines.
- `utils/import_undo.py`: 1152-1445.
- `utils/import_failures.py:303`: this file is in neither lane's list.
- `plugins/core/tools/plan_migration.py:149`.

**Not in this batch:**

- The batch create/update guard for other doctypes: L2-B, #34396.
- Reads of another user's `source_file`: fixed by 82869d20.

## Open questions

1. **Lane-1 coordination.** The guard can't land green unless the engine marks its writes in the lane-1 files listed above (`import_mapping.py`, `import_dry_run.py`, `import_run.py`, and possibly `import_fixes.py`). Lane 1 is editing `import_dry_run.py` now, for #34395. Choose one:
   - lane 1 adds the marks in its next commit, after the L2-A developer publishes the mark helper in `fac_import_session.py`;
   - the L2-A developer edits those lines once lane 1 has committed #34395;
   - L2-A waits for the phase merge.
2. **System Managers in Desk.** Should the guard also apply to System Managers? This requirement assumes yes: engine-owned fields change only through the engine, and repairs go through bench console with the engine mark. If no, criteria 1-9 apply to non-System-Managers only, and a System Manager test is added.
3. **Browser login.** `test_site.env` has only Administrator, which skips every check, so it can't show the bug. Someone has to create a non-System-Manager Desk User and a session it owns on the v16 site, and put the login in `test_site.env`. Who does it: the developer's setup script or the orchestrator? Also confirm the lane-2 v16 site: `/home/madesh/FAC16-L2/sites` was not found when this requirement was written.
4. **Ownership of unlisted files.** `plugins/core/tools/start_import_session.py`, `apply_import_preset.py`, `dry_run_import.py` and `utils/import_failures.py` are in neither lane's list. Which lane may edit them for the mark?
