# L2-B: batch create/update — customisation and DDL records, real all-or-none (#34396)

## Bug

| | #34396 |
|---|---|
| Title | Batch create_document/update_document skips the forced customisation approval and isn't all-or-none |
| Priority | High. It affects every create_document / update_document caller: FAC Chat users, and Claude or ChatGPT through FAC Connect, not only migration |
| Filed at | 00ebb857 |
| Related | #33992, epic #33986. #34411 (the batch approval card) is a separate batch, L2-G |

What Paul asks for, from the description:

- In FAC, limit batch mode to plain masters, so external MCP clients are covered too. Refuse `CUSTOMISATION_DOCTYPES` and any doctype whose insert runs DDL.
- Make AR's customisation check also look inside `documents`. This is AR's half and is out of scope here.
- Use Frappe's `_disable_transaction_control` instead of patching `commit`.
- Add tests:
  - a customisation doctype in a batch
  - DDL in a batch
  - a real exception mid-insert. Today's failure tests patch `execute` to return an error dict.

Paul's note of 07:51, "Still open at da175541":

- The only change since 00ebb857 is `_run_rollback_work` (0e1e8446).
- Batch mode still accepts customisation and DDL doctypes.
- `frappe.db.commit` is still replaced with a no-op.
- The audit log still records `target_doctype=None`.
- This matters more now that the onboarding setup plan (#34421) would rely on batch writes.

## Problem

All line numbers are on `fix/data-migration-bugs` at de2df60, in `frappe_assistant_core/`.

### 1. Customisation records in a batch get no forced approval

- AR decides a call is a customisation write from `tool_input["doctype"]`. A batch call has no top-level `doctype`. Its records are in `documents`.
- FAC does not check the doctypes in a list:
  - `utils/document_batch.py:55-84` (`read_documents`) checks only the shape of each entry.
  - `utils/document_batch.py:87-109` (`check_access`) checks only permission and restricted fields.
  - `plugins/core/tools/create_document.py:530-557` and `plugins/core/tools/update_document.py:505-516` then write every entry. So `documents=[{"doctype": "Custom DocPerm" | "Server Script" | "Role" | "Custom Field" | …}]` runs with no customisation card. If the user has set "don't ask again", or the model was steered by a file, no card shows at all.
- The canonical list is `utils/customisation.py:33-49` (`CUSTOMISATION_DOCTYPES`, 13 doctypes, from develop's #340). The SPA and AR mirror it.
- The descriptions invite this use. They say "(up to 100, any DocTypes)" at `create_document.py:106`, and the skills say "Any DocTypes" (`docs/skills/create_document.md:120`, plus the matching section in `docs/skills/update_document.md:112+`).
- Single-record mode is fine. It has a top-level `doctype`, so AR's existing check sees it. It also runs `server_script_guard` (`create_document.py:267`, `update_document.py:392`). So once FAC refuses customisation doctypes in a list, every customisation write has to take the single-record path, where AR's forced approval already applies. **That closes the risk on FAC's side even before AR changes.**

### 2. Audit trail

- `utils/audit_trail.py:103-107` takes `target_doctype` and `target_name` from the top-level `doctype` and `name` arguments. A batch has neither, so both are `None` for every batch call. It is called from `core/base_tool.py:490`.
- `target_doctype` is a Data field (140 characters) on Assistant Audit Log.
- `audit_trail.py` is not in either lane's file list, and lane 1 doesn't touch it.

### 3. "All or none" depends on patching `commit`

How `write_all` works now (`utils/document_batch.py:112-170`):

1. It sets the instance attribute `frappe.db.commit = _no_commit` (:129-130).
2. It opens savepoint `fac_document_batch` (:131).
3. On a failure it does `frappe.db.rollback(save_point=…)` (:149).
4. It hands back a caller's own commit stand-in (:155-158). The dry run's `held_back()` sets one (`utils/import_dry_run.py:1143`, lane 1).

Each record goes through the tool's own single-record `execute`. That code catches every exception and returns `{"success": False}`, and `write_all` turns that into `BatchProblem` (:135-144).

Where Frappe's transaction control differs:

| | v15 (15.121.3, `/home/madesh/FAC/apps/frappe`) | v16 (16.52.0, `/home/madesh/FAC16/apps/frappe`) |
|---|---|---|
| `_disable_transaction_control` | **Does not exist** (no match anywhere in v15's `frappe/`) | `database/database.py:136`. It is a counter: `model/document.py:1645-1652` does `+= 1` / `-= 1` around doc_events hooks |
| `commit()` | `database.py:1173`, always commits | `:1176-1181`, a no-op (with a warning) while the flag is set |
| `rollback()` (no savepoint) | `:1186-1200`, always does a full ROLLBACK | `:1196-1217`, a no-op (with a warning) while the flag is set |
| `sql_ddl()` | `:408-412`: `self.commit()`, then the DDL | `:451-458`: **clears the flag**, `self.commit()` (a real commit), the DDL, then restores the flag |
| DDL after writes in the transaction | `check_implicit_commit` (`:436-442`) raises `ImplicitCommitError` | Same, `:487-489` |

What happens with each case:

- **DDL in a batch today.** For example a Custom Field: `on_update` calls `frappe.db.updatedb` (`custom/doctype/custom_field/custom_field.py:216` v15, `:222` v16), then `schema.alter`, then `sql_ddl`.
  - On both versions, by reading the code, `sql_ddl`'s `self.commit()` hits FAC's no-op. The ALTER then trips `check_implicit_commit`, because the batch has already written rows, and raises `ImplicitCommitError`.
  - The single-record `execute` returns that as an error, and the batch is rolled back. So today the record is probably **not** committed, but the batch fails with a confusing "This statement can cause implicit commit".
  - This was not run: a probe would alter a live table.
- **Paul's scenario** ("A is committed by the DDL, then rollback raises SAVEPOINT does not exist") **becomes true as soon as FAC switches to `_disable_transaction_control` on v16**. `sql_ddl` clears the flag and really commits, which releases the savepoint. So the DDL refusal must land together with that switch, not after it.
- **A controller calling `frappe.db.rollback()`** is not stopped today on either version. It does a real full ROLLBACK, which undoes:
  - the earlier records of the batch
  - anything the request wrote before the batch
  - the savepoint itself

  If that record then raises, `write_all`'s savepoint rollback fails with "SAVEPOINT does not exist", and that error reaches the model. If it doesn't raise, the batch carries on and reports "Created N records" for records that no longer exist.
- **Other doctypes whose save runs DDL,** besides the customisation list (which already has DocType, Custom Field and Property Setter):
  - ERPNext Inventory Dimension: `on_update` calls `add_custom_fields`.
  - ERPNext Service Level Agreement: `before_insert` creates custom fields for a non-Issue document type.
  - ERPNext Accounting Dimension: runs inline in tests, otherwise it is enqueued after the commit.
  - Settings singles whose `on_update` runs `create_custom_fields`, such as CRM Settings and India Compliance's settings.

  A fixed list can't be complete, so a runtime guard is needed as well.
- **The failure tests don't exercise the real path.** `tests/test_document_batch.py:71` makes a record fail in validation, before anything is inserted. `:224-240` (`test_a_controller_that_commits_cannot_end_the_batch`) asserts the implementation (`"commit" in vars(frappe.db)`), not the outcome. There is no test with an exception after the row is written, a DDL record, or a controller rollback.

## Reproduces on

Both v15 and v16.

- Parts 1 and 2 are FAC code only.
- Part 3: the controller rollback reproduces on both versions today. The DDL outcome is the cryptic `ImplicitCommitError` on both today, and becomes a silent partial commit on v16 if the flag is adopted without the DDL guard.
- v15 has no `_disable_transaction_control`, so the v16 mechanism can't be used there (Open question 1).

## Acceptance criteria

These apply to create_document's list mode, including `validate_only` lists, and to update_document's list mode, unless a criterion says otherwise.

1. **A customisation doctype in a list is refused before anything is written.**
   - **Given** a list that contains, at any position, a record whose doctype is in `CUSTOMISATION_DOCTYPES` (any of the 13),
   - **When** create_document or update_document is called with it,
   - **Then:**
     - nothing in the list is written or changed
     - `success` is false
     - `problems` lists **every** such record (its number and doctype), not only the first
     - the error tells the model to make each of those on its own, with `doctype` and `data` (for create) or `doctype`, `name` and `data` (for update), one call each, and that it may send the other records as a list
     - the result has an `error_type` that tests can check
2. **A record whose save would change the schema is stopped before the change runs.**
   - **Given** a list `[ToDo, X]`, where saving X runs DDL (ALTER, CREATE, DROP, TRUNCATE, or anything through `frappe.db.sql_ddl` or `frappe.db.updatedb`), and X's doctype is not refused up front by criterion 1 or 3,
   - **When** the list is written,
   - **Then:**
     - the DDL does not run (no column, table or index is added)
     - nothing in the list is kept: the ToDo is gone and so is X's row
     - nothing the request wrote before the list was committed
     - the error names record 2 and says to create it on its own
     - the error is not "SAVEPOINT … does not exist" and not "This statement can cause implicit commit"
   - This holds on v15 and v16.
3. **Known schema-changing doctypes are refused up front, like criterion 1.** Inventory Dimension, Accounting Dimension and Service Level Agreement are refused before anything is written, with the same kind of message. The list lives in one place next to `CUSTOMISATION_DOCTYPES`. It is not added to that set, which AR and the SPA mirror.
4. **A real error after a record is saved undoes the whole list.**
   - **Given** a list `[ToDo A, Note B, ToDo C]`, where B's own controller raises after B's row is inserted (in `on_update` or `after_insert`, not by patching the tool's `execute`),
   - **When** the list is created,
   - **Then:**
     - A, B and C are all absent
     - the error names "Document 2 (Note)" and says nothing was created
     - the commit-time work queued by A (global search, `after_commit` callbacks, realtime and webhook queues) is dropped, as today
5. **A commit inside the list can't make earlier records permanent.** **Given** a list whose first record's controller calls `frappe.db.commit()`, and whose second record fails, **then** the first record is absent afterwards, and the error is record 2's own error.
6. **A full rollback inside the list fails the list and undoes nothing outside it.**
   - **Given** a ToDo P written in the same transaction before the call, and a list `[ToDo A, Note B]`, where B's controller calls `frappe.db.rollback()` (no savepoint) and returns normally,
   - **When** the list is created,
   - **Then:**
     - no real ROLLBACK runs, so P still exists in the transaction
     - the list is undone to its own savepoint: A and B are absent
     - `success` is false, and the error names record 2
     - the list never reports success for records that were undone
   - See Open question 2.
7. **On v16 the list is written with Frappe's own transaction control.**
   - **Given** Frappe has `frappe.db._disable_transaction_control`,
   - **When** a record is being written in a list,
   - **Then:**
     - the counter is above its value before the call
     - `frappe.db.commit` is not replaced (`"commit" not in vars(frappe.db)`)
     - afterwards the counter is back to exactly its value before the call, whether the list succeeded, failed, or raised. This includes a caller that had already raised it, so the counter is nested, not reset to 0.
   - On v15, which has no flag, criteria 2, 4, 5 and 6 still hold (Open question 1).
8. **A caller's own commit stand-in is still given back.** The dry run's `held_back()` behaviour is unchanged, and `test_a_callers_own_commit_stand_in_is_given_back` still passes.
9. **The audit trail records a list's doctypes.**
   - **Given** a create_document or update_document call with `documents` of ToDo, Note and ToDo,
   - **When** its Assistant Audit Log row is written (through `_safe_execute`, on success and on failure),
   - **Then** `target_doctype` holds the distinct doctypes in order ("ToDo, Note"), not None, and fits the 140-character field even for a long list of different doctypes. `target_name` is empty unless the list has exactly one record with a name.
   - Single-record calls still record `doctype` and `name` exactly as today.
10. **Single-record behaviour is unchanged.**
    - A single-record create or update of a customisation doctype works as before, with `doctype` + `data`. That includes Role, Custom Field, Property Setter, and Server Script under `server_script_guard`.
    - Its result still carries `customisation_result` (`applies_to`, `location`, warnings).
    - A single record with `import_session` (`create_document.py:151-160`) may go through the list code, and can be refused there.
    - The existing single-record tests pass unchanged, in particular `test_customisation_guard`, `test_field_location`, and the create/update tool tests.
11. **The model is told the rule.**
    - The `documents` descriptions in both tools (`create_document.py:106`, `:134`; `update_document.py:210-212`, `:242`) say that customisation and schema-changing records are made one at a time.
    - So do the skills' "Several records at once" sections (`docs/skills/create_document.md:106-126`, `docs/skills/update_document.md:112+`).
    - "any DocTypes" is gone.

## Tests to add

`frappe_assistant_core/tests/test_document_batch.py`, unless noted. Every test goes through `_safe_execute`, except those marked "direct", and none patches the tool's `execute`.

| Criterion | Tests |
|---|---|
| 1 | `test_every_customisation_doctype_in_a_create_list_is_refused_before_anything_is_written`: a ToDo first, then one entry per `CUSTOMISATION_DOCTYPES` member, in subTests. Check that `problems` lists them all, the ToDo is absent, and `error_type` is set<br>`test_a_customisation_record_in_an_update_list_is_refused`: Role "Guest" `disabled: 1` together with a ToDo update. Neither changes<br>`test_a_validate_only_list_with_a_customisation_record_is_refused`<br>`test_the_refusal_says_to_make_each_customisation_on_its_own` |
| 2 | `test_a_schema_change_inside_a_list_is_stopped_and_the_list_undone` (direct): `write_all([ToDo, Custom Field on ToDo], <create_document's single-record execute>)`. This bypasses the up-front refusal, so the real `updatedb` path runs. Afterwards `frappe.db.has_column("ToDo", <fieldname>)` is false, there is no Custom Field row and no ToDo, and the error names record 2 and is not a savepoint or implicit-commit error<br>`test_nothing_before_the_list_is_committed_by_a_schema_change` |
| 3 | `test_a_known_schema_changing_doctype_in_a_list_is_refused_up_front`, with Inventory Dimension (ERPNext) |
| 4 | `test_a_real_error_after_a_record_is_saved_undoes_the_whole_list`: patch `Note.on_update` or `after_insert` (the controller) to raise `frappe.ValidationError`<br>`test_an_undone_list_sends_nothing_out_after_a_real_error` |
| 5 | Rewrite `test_a_controller_that_commits_cannot_end_the_batch` (`:224`) as `test_a_controller_that_commits_cannot_make_earlier_records_permanent`: assert the outcome, not `vars(frappe.db)` |
| 6 | `test_a_controller_rollback_fails_the_list_and_keeps_what_came_before` |
| 7 | `test_a_list_is_written_with_transaction_control_disabled_on_v16`: skip where Frappe has no flag<br>`test_transaction_control_is_given_back_after_a_failed_list`<br>`test_a_callers_raised_transaction_control_is_kept` |
| 8 | Existing `test_a_callers_own_commit_stand_in_is_given_back`: unchanged, must pass |
| 9 | `tests/test_audit_log.py`: `test_a_list_call_records_its_doctypes_as_the_target`, `test_a_long_list_of_doctypes_fits_the_target_field`, `test_a_single_record_call_still_records_its_doctype_and_name`<br>`test_document_batch.py`: `test_a_batch_call_is_audited_with_its_doctypes`, end-to-end through `_safe_execute` |
| 10 | `test_one_customisation_record_on_its_own_is_still_created`: a Role through `doctype` + `data`<br>`test_one_customisation_record_on_its_own_is_still_updated` |
| 11 | `test_the_list_descriptions_say_customisations_go_one_at_a_time` (both tools' `description` and `documents` schema text) |

Existing tests that must change, because they use a customisation doctype to test something else:

- `test_a_record_the_user_may_not_create_stops_the_list_before_anything_is_written` (`:101-122`) uses Role.
- `test_a_record_the_user_may_not_change_stops_the_update_list` (`:394-421`) uses Role.

After the fix both would pass for the wrong reason (the customisation refusal). Move them to a non-customisation doctype the Desk user can't create or write, so they still test permission.

## Browser check

**No.** The change is server-side: a refusal message returned as an ordinary tool error, and the transaction handling. There is no new UI. The approval card and its customisation warning are AR and the SPA. The batch card table is #34411 (L2-G).

## Out of scope

- **AR (Paul):** making `customisation_risk.py` and `approval_hook.py` look inside `documents`, setting `risk: customisation` for a list, and the hook-level test "a customisation doctype in a batch goes through the AR approval hook". With criteria 1–3, FAC itself never writes a customisation or schema-changing record from a list. Every such write then comes as a single record with a top-level `doctype`, which AR already forces to approval. External MCP clients, which have no AR, are covered by the same refusal.
- The batch approval card (#34411, L2-G) and the SPA's customisation warning (`ApprovalCard.vue`).
- Onboarding's setup plan (#34421): if it needs customisation records, it has to make them one at a time. That is not part of this batch.
- The import-session path's own checks (`import_fixes.check_masters`, `create_master`). These are lane 1's files and are not changed here.
- Side effects a save sends out at once (Slack, SMS, jobs enqueued immediately), already documented at `document_batch.py:19-21`.

## Open questions

1. **v15 has no `_disable_transaction_control`** (Frappe 15.121.3 on the v15 bench). Should we use Frappe's flag where it exists (v16), and keep FAC's own stand-in for `commit` *and* a full `rollback` on v15, with the same outcomes (criteria 2, 4–6) on both? Recommended: yes. The alternative is dropping v15 support for list mode.
2. **A controller's full rollback inside a list:** should the list be failed and undone (criterion 6, recommended, the conservative choice), or should we follow Frappe v16's own semantics (ignore the rollback with a warning and carry on)?
