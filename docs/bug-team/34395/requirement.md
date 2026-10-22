# #34395 Requirement: dry run on Frappe 16.52, with missing-link and bad-option fixes

## Bug

- **#34395** "Dry run rejects every row on Frappe 16.52, and missing-link fixes never appear". Priority
  High, target v3.2.x, reporter Paul Clinton. Blocks #33990 (dry run) and #33991 (fixes); relates to
  epic #33986.
- Paul's repro (00ebb85, Frappe 16.52.0 / ERPNext 16.14 / IC 16.3.2): `migtest_customers_clean.csv`
  (32 customers; Cust Name, Party Group, Region, Customer Type, GSTIN No.) gives "0 rows ready, 32
  would fail", 31 rows in one *Rejected by ERPNext* group with
  `'types.SimpleNamespace' object has no attribute '_uses_tree_aliases'`. The file's real problems
  (Customer Groups Retail/Wholesale, Territories Karnataka/Maharashtra, Customer Type "Pvt Ltd" don't
  exist) never show.
- Note of 2026-10-22: still open at da17554; "the largest cause" of the v16 suite failures, cascading
  as "Dry Run Failed != Ready to Import" in import_run, import_undo, undo_import_tools,
  run_migration_plan, import_failures, migration_report and import_plan.
- Paul's fix: build a real unsaved Data Import and `Importer`, run Frappe's own setup and
  `process_doc`, read `import_file.get_warnings()`; no private methods with a stand-in `self`. His
  second point (CI on version-15/16 tips) went to #34418 by user decision (STATUS.md, #34430 row).
- **Added by user decision (STATUS.md "Decisions", 2026-10-22):** the dry run's stand-in record
  (`_stand_in`) is rejected by India Compliance for a missing HSN code;
  `test_import_plan.test_an_earlier_files_failing_row_still_counts_as_coming` must pass on v16.

## Problem

All lines are on `fix/data-migration-bugs` at de2df60, file `frappe_assistant_core/utils/import_dry_run.py`
unless named. Paul's `:309-317` is now `:422-430`; his `:345-352` is now `:555-567`. Frappe v16 is
`/home/madesh/FAC16/apps/frappe` at 949a179 (shallow clone, so Paul's commit ids 2fd5c4fe1c and
e274dab38b can't be looked up; the code they describe is there). v15 is
`/home/madesh/FAC/apps/frappe` at 0b282e8 (15.121.3).

**(a) Stand-in `self` for Importer.** `_Checker.__init__` (:421-430) builds a `SimpleNamespace`
with only `doctype`, `import_type` and `data_import`, and `_insert_problems` (:765-801) calls
`Importer.update_record(self.importer, doc)` (:774) and `Importer.insert_record(self.importer, ...)`
(:779, :789) with it.
- v16 `importer.py`: `insert_record` reads `self._uses_tree_aliases` at :564 and :575, and
  `update_record` reads it at :610, for **every** DocType. These attributes are set only in
  `before_import` (:117-137), together with `_tree_parent_field`, `_tree_alias_field`,
  `_id_fieldname`, `_inserted_name_map` and `provider`. So every row of every insert or update dry run
  raises AttributeError and becomes *Rejected by ERPNext*.
- v15 `importer.py:256-295`: `insert_record`/`update_record` read only `doctype`, `import_type` and
  `data_import`. **v16 only.**

**(b) Bad Link/Select values are column warnings on v16.** `_parse_problems` (:555-567) reads only
`row.warnings`, and `_from_warning` (:569-603) turns a row warning's `field` into
`CAUSE_MISSING_LINK` / `CAUSE_LATER_FILE` / `CAUSE_BAD_OPTION`.
- v16 `Row.validate_value` (:1634-1646) returns Link and Select values unchanged with no row warning
  ("Invalid values are reported once per column"). `Column.validate_values` (:2001-2004) calls
  `value_mapping.warn_invalid_link_select_values` (`value_mapping.py:298-335`), which appends one
  warning per column, `{"col", "message", "type": "value_mapping"}` with no `field` and no `row`; its
  message is translated HTML whose row list is cut to 6 rows (`format_row_numbers_for_warning`). The
  values and their full row lists are kept on the column as `col.invalid_value_items`
  (`value_mapping.py:175-215`, 303). Self-links whose value is a record of the same file are left out
  (`importer.py:880-909`, `value_mapping.py:197-205`).
- Result on v16: a bad link or option is never a group of its own. The bad value also stays in the
  parsed record (v15 leaves it out), so once (a) is fixed the row would only fail at insert as
  *Rejected by ERPNext* (LinkValidationError), with no map/create/leave-out fixes from
  `import_fixes`.
- v15 `Row.validate_value` (:701-760) adds a row warning with `field` for each bad Link and Select
  value. v15 *also* adds column warnings for them (`Column.validate_values` :1010-1059: Link with
  `type: "warning"`, Select with no type), which the dry run ignores today. **v16 only**; on v15 the
  column warnings repeat the row warnings and must not add groups.

**(c) What the real import does that the dry run skips.** v16 only:
- `Importer.process_doc` (:533-542) hands the row to an import provider when one is registered
  (`import_provider.py:42-55`, hook `data_import_providers`). ERPNext 16 registers
  `PartyImportProvider` for **Customer and Supplier** (`erpnext/hooks.py:736-739`), the DocType of
  the bug's file; its `import_row` creates linked Contacts and Addresses after the party
  (`party_import_provider.py:40-51`). `ImportFile.get_warnings` (:1036-1053) also adds the provider's
  `validate()` warnings. The dry run calls `insert_record`/`update_record` directly, so neither runs.
- In-file tree parents by alias: for a nested-set DocType that isn't named `field:`, v16 lets a row
  name its parent by its alias (title field, else first required Data field:
  `_get_tree_alias_field_from_meta` :1140-1159). `before_import` primes
  `_inserted_name_map` from the database (:139-167), `insert_record`/`update_record` swap the alias
  for the real name (`_resolve_tree_parent_link` :581-590), and each insert registers its name
  (:592-601). On fac16 this applies to Account (`account_name`), Cost Center, Warehouse and Employee
  (`employee_name`, parent `reports_to`). Item Group, Customer Group and Territory are named
  `field:`, so they have no alias. The dry run's own in-file handling (`self_links`, `planned`,
  `_needs`, :437-439, :493-499, :687-701) matches a parent only by the name the parent row
  creates, e.g. "Assets X - TC", not "Assets X". So such rows fail in the dry run while the real
  import accepts them.
- Correction to Paul: Item Group rows naming an earlier row as parent
  (`test_a_tree_row_whose_parent_is_an_earlier_row_of_the_file_is_ready`) fail on v16 only because
  of (a). The dry run's own `planned`/`_needs` logic already handles them.
- v15 has none of this: no provider, no alias resolution (`importer.py:250-295`). A v15 import
  refuses a parent given by alias, and the dry run already says so.

**(d) Stand-in rejected by India Compliance (user decision).** `_stand_in` (:829-845) inserts a bare
record, with `ignore_mandatory`, for a link to something an earlier plan file brings. IC's Item
`validate` hook (`india_compliance/gst_india/overrides/item.py:25-30` →
`gst_hsn_code.py:115-125`) throws `MandatoryError: HSN/SAC Code is required` anyway. The linking row
(prices.csv's Item Price) then fails *Rejected by ERPNext*, and the plan asks about it.
`test_an_earlier_files_failing_row_still_counts_as_coming` (test_import_plan.py:675-695) expects only
the Brand question. It reproduces wherever IC validates HSN codes (fac16). On v16 today the
SimpleNamespace error hides it.

**Evidence: v16 logs after #34430** (`/home/madesh/sddscripts/agents/logs/fac16.localhost/`). There are
320 failing tests:
- 306 carry the `_uses_tree_aliases` signature. One of them is
  `test_an_earlier_files_failing_row_still_counts_as_coming`, whose expected message is replaced by
  it; this is #34430's "305 + 1 prices.csv".
- 7 are knock-ons: `test_import_dry_run.test_a_large_file_is_checked_in_the_background` (7 != 6),
  `test_import_failures.test_a_whole_file_sent_again_does_not_bring_in_its_imported_rows_twice`,
  `test_import_guardrails.test_a_master_that_was_not_planned_is_refused_fenced_and_briefly`, and
  `test_import_plan.test_a_file_that_needs_an_earlier_files_records_is_ready`,
  `test_a_name_that_differs_only_in_case_is_the_same_record`,
  `test_a_record_from_a_later_file_is_an_order_problem`,
  `test_a_record_nothing_creates_is_still_missing`.
- 7 belong elsewhere:
  - 5 "Activate Serial / Batch No" (#34428): opening_stock `test_a_batch_item_with_a_new_batch_and_a_serial_item_pass`,
    `test_two_batches_of_one_item_in_a_warehouse_are_not_counted_twice`, `test_each_problem_is_its_own_group`;
    undo `test_a_batched_opening_stock_is_undone_with_its_bundles_and_batch`,
    `test_a_failed_delete_restores_the_batched_reconciliation_its_bundles_and_batch`.
  - 1 email wording (#34412): `test_import_guardrails.test_a_validate_only_list_reaches_the_model_fenced`.
  - 1 not yet assigned: `test_import_undo.test_accounts_frozen_till_falls_back_to_accounts_settings_for_every_company`
    (KeyError: None).

## Reproduces on

- **v16 (fac16.localhost) only.** (a), (b) and (c) come from code that exists only in Frappe v16's
  importer. (d) needs India Compliance with HSN validation on, and only fac16 has IC.
- **v15 (fac.localhost):** all 1,054 tests pass at de2df60. The v15 importer has none of the three
  causes. The fix must keep v15 exactly as it behaves today.

## Acceptance criteria

The dry run's rows, groups, group ids, messages and fixes are the same on v15 before and after the fix
for every existing test. "Ready" below means the row's status in the dry run's per-row list.

1. **A clean file is ready on v16.** Given a Customer session whose rows link only to existing
   records and use allowed options (the bug's file with its masters created), when `dry_run_import`
   runs on v16, then every row is ready, status "Ready to Import", and no group names
   `_uses_tree_aliases` or any `AttributeError`. The same holds for an update session
   (`update_record` path) and on v15.
2. **Rows go through Frappe's own importer.** Given any insert or update session, when a row is
   tried, then it is tried through `Importer.process_doc` of a real `Importer`, with a Data Import that
   is never saved and whose import type and submit-after-import come from the session. Frappe's own
   setup for that importer has run, so the attributes `before_import` sets exist on v16. No Frappe
   method is called with a stand-in `self`. This works with v15's `process_doc` (returns the document)
   and v16's (returns `(document, action)`).
3. **A bad Link value is its own group, with its rows (v16 column warning).** Given a file whose
   Customer Group column has two values that don't exist, A on 7 rows and B on 2 rows, when the dry
   run runs, then there are exactly two *Missing link* groups:
   - Each has `field` = customer_group, `parent` = Customer, `value` = A (or B), its message
     "Customer Group 'A' doesn't exist.", and its count and rows exactly 7 (or 2). All 7 are listed,
     although v16's warning text shows only 6.
   - The group id is the one v15 gives for the same failure (the key is cause, parent, field, value).
   - No group comes from the column warning itself (no *Invalid value* group, none without rows).
   - This holds on both versions.
4. **A bad Select value is its own group, with its rows.** Given a Customer Type column with a value
   outside the options ("Pvt Ltd"), when the dry run runs, then there is exactly one *Not an allowed
   option* group for it: `field` customer_type, `value` "Pvt Ltd", message
   "'Pvt Ltd' isn't one of the options for Customer Type.", and its exact rows. This holds on both
   versions.
5. **The #33991 fixes are offered for those groups.** Given the groups of criteria 3-4 on v16, then
   the *Missing link* group offers the closest existing names (map), create and leave out, and the
   *Not an allowed option* group offers the allowed options (map) and leave out. Applying a map fix
   and checking again makes the group disappear and the rows ready. The same holds on v15.
6. **Column warnings respect the dry run's own rules.**
   - A bad value on rows left out (`excluded_rows`) or already imported puts none of those rows in a
     group.
   - A link to a record an earlier plan file creates is not a problem (the row is ready and
     `after_files` names that file).
   - A link to a record a *later* plan file creates is a *Comes from a later file* group.
   - A link to a record that a ready row higher up the same file creates is not a problem.
   - A bad value in a child-table column is a group whose `parent` is the child DocType.
   - The same file and session give the same groups on v15 and v16.
7. **The bug's file.** Given a Customer file shaped like `migtest_customers_clean.csv`, with Customer
   Groups "Retail …"/"Wholesale …", Territories "Karnataka …"/"Maharashtra …" and Customer Type
   "Pvt Ltd" that don't exist (tagged names) plus valid rows, when the dry run runs on v16, then:
   - there are 4 *Missing link* groups and 1 *Not an allowed option* group, each with its fixes;
   - rows with none of those values are ready;
   - nothing says `SimpleNamespace`.
8. **Import providers are used (v16).** Given a DocType with a `data_import_providers` entry, when a
   row is tried, then the provider's `import_row` runs for it. A provider error is a *Rejected by
   ERPNext* group. Each warning the provider's `validate()` returns that isn't `type: "info"` comes
   back as a group with its row. For Customer/Supplier on fac16 (PartyImportProvider), a clean row is
   still ready.
9. **In-file tree parents by alias (v16).**
   - Given an Account file where row 3 names as its parent the `account_name` of row 2 (a group
     account in the same file), when the dry run runs on v16, then both rows are ready, and a row
     whose parent row fails is not ready.
   - Given a row that names an existing parent account by its `account_name` (without the company
     abbreviation), it is ready on v16 as the real import would accept it.
   - On v15 the same rows fail, as v15's import refuses them.
   - Item Group in-file parents
     (`test_a_tree_row_whose_parent_is_an_earlier_row_of_the_file_is_ready`) are ready on both
     versions.
10. **The stand-in is accepted under India Compliance.** Given a migration plan where prices.csv
    links Item "I x" that items.csv brings, and the site's Item controller refuses a bare Item (IC's
    HSN rule on fac16, or a validate hook that throws, in the test), when prices.csv is checked, then
    its row is ready (not *Rejected by ERPNext*), and afterwards no Item "I x" exists.
    `test_an_earlier_files_failing_row_still_counts_as_coming` passes on v16 with exactly the one
    Brand question.
11. **Still writes nothing.** Given any dry run, when it finishes:
    - Customer, Contact, Address, Email Queue, Version, Data Import and Data Import Log counts are
      unchanged;
    - no Data Import record was saved and no `template_warnings`/`status` was written;
    - `frappe.flags.in_import` and `frappe.flags.mute_emails` are back to their values before the
      check (Frappe's `before_import` sets them and `after_import` clears them to False);
    - the "held back" behaviour still holds: emails, jobs, realtime events, commits, global search,
      Google sync, rollback callbacks.
12. **Tree-level and cross-file behaviour unchanged.**
    - The per-row `after` (rows higher up the file) and `after_files`/`needs` (earlier plan files)
      are as today.
    - Ready rows still record the `name` they create.
    - Failing rows still record their intended `name`.
    - `import_plan.planned_names`/`later_names` get the same data as before.
13. **Both suites.** Given the fix:
    - v15: every data-migration module passes (1,054 tests plus the new ones, 0 failures).
    - v16: every data-migration test passes except exactly the 7 listed under "Evidence" as
      belonging elsewhere, compared by test name.

## Tests to add

Red evidence: on v16 every existing test below fails on de2df60 (the logs above). New tests must fail
on de2df60 on v16 (`red_check.sh`). Tests for criteria 3, 4, 6 and 11 also run on v15 and must pass
there, so they can't be written only for one version.

| Criterion | Module | Tests |
|---|---|---|
| 1 | `test_import_dry_run` | existing `test_a_clean_file_is_ready_to_import`, `test_a_check_adds_nothing_to_global_search`; `test_import_update` (all 11 failing on v16) |
| 2 | `test_import_dry_run` | new `test_each_row_is_tried_through_frappes_own_process_doc` (spy on `Importer.process_doc` with autospec and the real method as side effect: called once per checked row, `self` is an `Importer`) |
| 3 | `test_import_dry_run` | new `test_a_bad_link_value_is_its_own_group_with_every_row` (7 + 2 rows, asserts the full group list, rows, message, id) |
| 4 | `test_import_dry_run` | new `test_a_bad_select_value_is_its_own_group_with_every_row`; existing `test_every_row_is_checked_and_failures_are_grouped_by_cause`, `test_a_group_shows_at_most_three_example_rows` |
| 5 | `test_import_fixes` | existing `test_a_missing_link_offers_the_closest_names_create_and_leave_out`, `test_a_value_outside_the_options_is_explained_with_the_choices`, `test_mapping_a_value_becomes_a_rule_and_the_rows_are_checked_again`, `test_a_select_value_can_be_mapped_to_an_allowed_option` (all 23 failing in the module) |
| 6 | `test_import_dry_run`, `test_import_plan`, `test_import_fixes` | existing `test_rows_left_out_are_not_checked`, `test_a_record_from_a_later_file_is_an_order_problem`, `test_a_file_that_needs_an_earlier_files_records_is_ready`, `test_a_child_table_value_is_mapped_on_the_child_table_not_the_parent`; new `test_a_bad_value_only_on_rows_left_out_makes_no_group` |
| 7 | `test_import_fixes` | new `test_unknown_groups_territories_and_customer_type_each_get_their_fixes` |
| 8 | `test_import_dry_run` | new `test_a_registered_import_provider_imports_the_row`, `test_an_import_providers_warning_is_a_group` (v16; skipped with the reason when `frappe.core.doctype.data_import.import_provider` doesn't exist) |
| 9 | `test_import_dry_run` | existing `test_a_tree_row_whose_parent_is_an_earlier_row_of_the_file_is_ready`; new `test_an_account_naming_its_parent_by_account_name_from_an_earlier_row_is_ready`, `test_an_account_naming_an_existing_parent_by_account_name_is_ready` (each asserts ready where the importer resolves tree aliases, failed where it doesn't: a branch on both versions, not a skip) |
| 10 | `test_import_plan` | existing `test_an_earlier_files_failing_row_still_counts_as_coming`; new `test_a_stand_in_is_made_when_its_doctype_refuses_a_bare_record` (Item validate made to throw; runs on both versions) |
| 11 | `test_import_dry_run` | existing `test_nothing_is_written`, the `held_back` tests; new `test_a_check_saves_no_data_import_or_log`, `test_a_check_leaves_the_import_flags_as_they_were` |
| 12 | `test_import_dry_run`, `test_import_plan`, `test_import_run` | existing tree, plan and `import_run` tests (33 failing in import_run) |
| 13 | all modules | `baseline.sh v15`, `baseline.sh v16` |

No existing test is wrong. They fail on v16 only because of this bug (and #34428/#34412 for the 6
named).

## Browser check

No. The dry run's reply is shown by FAC Chat through the AR runtime, which isn't on this machine, and
no Desk form changes. Criterion 7's test stands in for Paul's live repro; Paul can re-run his CSV in
FAC Chat after the push.

## Out of scope

- CI on the version-15/16 tips (Paul's second fix point): #34418, by user decision.
- The 5 "Activate Serial / Batch No" failures (#34428), the guardrails email wording (#34412), and
  `test_accounts_frozen_till_falls_back_to_accounts_settings_for_every_company` (KeyError, unassigned).
- Other dry-run side effects (#34401), including the lang-cache delete in Frappe's `before_import`.
  This bug must add no *new* side effect (criterion 11).
- Naming-series duplicates (#34398), job id (#34397), group splitting/capping (#34407), parent-first
  creation of masters (#34409), update required fields (#34408).
- v16's tree-preview warnings (cycles, duplicate ids, non-group parents; `get_all_warnings`), which
  the real import blocks on but `get_warnings()` doesn't include. Not in Paul's report. Note them in
  dev-notes if seen.
- `import_run.py:316` (the import guard reads `get_warnings()`): unchanged unless the fix needs it.
- The opening engines (stock, invoices, balances) have their own checks, which already pass on v16.
  They must stay green.
- AR / FAC Cloud: not involved.

## Open questions

None.
