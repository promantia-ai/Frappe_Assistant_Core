# #34430 Developer notes: test suite portable to Frappe v16 and India Compliance

Commit: **de2df60** on `fix/data-migration-bugs` (not pushed). Test files only; no product code, preset or
CI file changed (criterion 8). CI job (criterion 9): out of scope by the user's decision; it goes with #34418.

## What changed and why

| File | Change |
|---|---|
| `tests/base_test.py` | `NO_RIGHTS_ROLE` ("FAC Test No Rights"): a role with desk access and no permission rules, made in the test transaction when missing. `make_desk_user(prefix)`: a throwaway user with only that role; asserts the user is a System User. `assert_cannot(user, ptype, doctype, doc=None)`: `frappe.has_permission` must be False. `item_hsn_fields()`: `{"gst_hsn_code": <first 8-digit GST HSN Code>}` when Item has `gst_hsn_code`, else `{}` (8 digits is valid at every "minimum HSN digits" setting). |
| 10 modules, 19 call sites (session, google_sheet, mapping, document_batch, fixes, preset, preset_xml, dry_run, masters) | `make_throwaway_user(..., roles=("Blogger",))` → `make_desk_user(...)`, plus an `assert_cannot` before the test relies on the refusal: Data Import create (session/google_sheet setUp), File read on the other user's file (session ×3, preset, preset_xml), FAC Import Session read/write on the other user's session (session, mapping, fixes, dry_run), Contact import (mapping ×2), Role create/write (document_batch ×2), Customer Group read/create (fixes, masters). |
| `test_import_opening_stock.py` `_item`, `test_import_undo.py` ×2, `test_import_update.py` ×2 | Item fixtures add `**item_hsn_fields()`. |
| `test_import_plan.py` | `_item_file` moved to `ImportPlanCase`, builds the HSN column when the site has the field; `group=None` leaves the Group column out. Used for the former literal item files at :353 and :628. |
| `test_plan_openings.py` `_items`, `test_run_migration_plan.py` `_ready_plan` | Items CSV gets an `HSN` column mapped to `gst_hsn_code` when the site has the field. |
| `test_apply_import_preset.py`, `_xml.py` | `assert_gstin_and_email_follow_the_site()`: GSTIN → `gstin` if Customer has it, else `tax_id`; Email → `email_id` if not hidden, else not mapped and `Email (email_id)` in `not_on_this_site`. Both branches assert. |

## Tests added

- `test_base_test_fixtures.py` (new, 6 tests): `test_the_throwaway_role_exists_on_this_site` (desk access, no DocPerm / Custom DocPerm names it), `test_a_throwaway_user_is_a_desk_user`, `test_a_throwaway_user_cannot_read_or_create_import_doctypes` (Customer, Supplier, Customer Group, Item, Role, Data Import: no read/create/write; Contact: no import), `test_an_item_fixture_inserts_on_this_site`, `test_an_item_fixture_has_a_valid_hsn_when_india_compliance_is_installed` (exists as GST HSN Code, length in IC's `get_hsn_settings()`), `test_an_item_fixture_sets_no_hsn_without_india_compliance` (Meta.has_field patched with autospec on v16; real on v15).
- `test_import_plan.test_an_item_file_has_an_hsn_column_when_india_compliance_is_installed`.
- `test_apply_import_preset.test_a_hidden_email_field_is_listed_as_not_on_this_site` (v16: real; v15: a Property Setter hides `email_id`, as Customize Form does, deleted in cleanup; checked afterwards that no Property Setter, role or user was left on fac.localhost).

## Red evidence (v16, before the change)

From `/home/madesh/sddscripts/v16-baseline/` (2026-10-22): Blogger 63 tests (`LinkValidationError: Could not find Row #1: Role: Blogger`),
HSN 101 tests (`MandatoryError: HSN/SAC Code is required`), GSTIN 2 tests (`AssertionError: 'gstin' != 'tax_id'`).
Rerun today on da17554 (before the change):

```
FAIL fac16.localhost  test_document_batch                      ran=25   failures=0    errors=2
     ERROR test_a_record_the_user_may_not_change_stops_the_update_list: frappe.exceptions.LinkValidationError: Could not find Row #1: Role: Blogger
FAIL fac16.localhost  test_apply_import_preset_xml             ran=30   failures=1    errors=1
     ERROR test_a_user_who_cannot_read_the_file_gets_the_usual_refusal: frappe.exceptions.LinkValidationError: Could not find Row #1: Role: Blogger
     FAIL test_ledgers_open_customers_and_suppliers_and_ask_for_the_company
```

The new guard tests fail with the old fixtures by construction: with `roles=("Blogger",)` the user insert raises
`LinkValidationError` on v16; with `item_hsn_fields()` returning `{}` the Item insert raises the HSN `MandatoryError`.

## Green

**v15** (`baseline.sh v15` after the change): every module PASS, `TOTAL ran=1054 failures=0 errors=0`
(1,052 + the 2 new tests in existing modules), plus `PASS fac.localhost test_base_test_fixtures ran=6`.

**v16** (`baseline.sh v16` after the change): `TOTAL ran=1054 failures=277 errors=43` (baseline before: 166 more
from this bug). Logs contain **0** `Role: Blogger`, **0** `'gstin' != 'tax_id'`; `HSN/SAC` only in
test_import_plan (one test, see Seen, not fixed). `PASS fac16.localhost test_base_test_fixtures ran=6`.
No test fails now that passed before.

| Module (v16) | Before | Now | Gone: Blogger / HSN / GSTIN | Remaining |
|---|---|---|---|---|
| test_apply_import_preset | 2 | **0 PASS** | 1 Blogger, 1 GSTIN | – |
| test_apply_import_preset_xml | 2 | **0 PASS** | 1 Blogger, 1 GSTIN | – |
| test_document_batch | 2 | **0 PASS** | 2 Blogger | – |
| test_import_mapping | 3 | **0 PASS** | 3 Blogger | – |
| test_import_session | 36 | **0 PASS** | 36 Blogger | – |
| test_import_google_sheet | 17 | 1 | 16 Blogger | 1 #34395 |
| test_import_fixes | 25 | 23 | 2 Blogger | 23 #34395 |
| test_import_dry_run | 13 | 12 | 1 Blogger | 12 #34395 (incl. `7 != 6`, same as baseline: background check counts) |
| test_import_masters | 19 | 18 | 1 Blogger | 18 #34395 (`Missing doctype, data.` = empty master list from the failed dry run) |
| test_import_opening_stock | 78 | 3 | 75 HSN | **3 not #34395: Has Batch No** (below) |
| test_plan_openings | 15 | 6 | 9 HSN | 6 #34395 (items.csv row rejected with SimpleNamespace) |
| test_import_undo | 83 | 80 | 3 HSN | 77 #34395, `KeyError: None` (same as baseline), **2 Has Batch No** |
| test_import_update | 11 | 11 | 0 (2 HSN tests now reach the dry run) | 11 #34395 |
| test_migration_report | 13 | 12 | 1 HSN | 12 #34395 |
| test_import_plan | 13 | 13 | 0 | 12 #34395 (4 in TestRecordsEarlierFilesCreate: the Item Group file's check fails on SimpleNamespace, assert has no message); 1 also shows the stand-in HSN (below) |
| test_run_migration_plan | 25 | 25 | 0 | 25 #34395 (groups.csv tree rows) |
| failures, guardrails, run, undo_import_tools | unchanged | unchanged | – | same tests as the baseline; #34395 signature, except 2 guardrails tests (`'Ignore previous instructions…' isn't in the payload`) whose block shows no signature: same failure as the baseline, not traced further |

"#34395" = `'types.SimpleNamespace' object has no attribute '_uses_tree_aliases'` in the block, or a dry run
reporting "would fail" / status not "Ready to Import" because of it.

## Seen, not fixed (need a decision)

1. **Product bug, not test-only: the dry run's stand-in Item fails under India Compliance.** `_stand_in`
   (`utils/import_dry_run.py:829-845`) inserts a bare record for a link to a record an earlier file of the plan
   brings, with `ignore_mandatory=True`. IC's Item `validate` raises `MandatoryError` itself, so on an IC site any
   later file linking to Items from an earlier file (prices, stock) gets "HSN/SAC Code is required". This is why
   `test_an_earlier_files_failing_row_still_counts_as_coming` still shows `('prices.csv', 'HSN/SAC Code is
   required…')` though items.csv now carries an HSN (requirement criterion 4's "in particular" can't be met by
   test changes). Needs its own bug, or a scope change here (criterion 8 forbids product changes).
2. **v16 Stock Settings: "Cannot enable Has Batch No as Activate Serial / Batch No for Item is disabled".**
   5 tests (opening_stock: `test_a_batch_item_with_a_new_batch_and_a_serial_item_pass`,
   `test_two_batches_of_one_item_in_a_warehouse_are_not_counted_twice`, `test_each_problem_is_its_own_group`;
   undo: `test_a_batched_opening_stock_is_undone_with_its_bundles_and_batch`,
   `test_a_failed_delete_restores_the_batched_reconciliation_its_bundles_and_batch`). Hidden by the HSN error
   before; another v16 portability gap in the fixtures, not in #34430's requirement.
3. `grep -rn Blogger frappe_assistant_core/tests/` still matches stale `__pycache__/*.pyc` (untracked);
   `grep --include=*.py` returns nothing.
