# #34430: test report

**Bug:** Test suite isn't portable to Frappe v16 or to sites with India Compliance (High)
**Commit:** de2df60 on `fix/data-migration-bugs` (tests only; `git diff da17554 de2df60` outside `tests/` is empty)
**Testers:** tester-v15, tester-v16 (in parallel); tester-browser not needed (no UI change)

## Verdict: PASS

| Tester | Verdict |
|---|---|
| tester-v15 | PASS |
| tester-v16 | PASS |
| tester-browser | Not needed |

## Criteria (tester-v15, with tester-v16's fac16 results)

| # | Criterion | Test(s) | Result |
|---|---|---|---|
| 1 | No Blogger dependency | `test_base_test_fixtures` (role exists, throwaway user is a desk user); grep `--include=*.py` | PASS. 0 Blogger failures on v16 (63 before) |
| 2 | The "other user" has no rights | `test_a_throwaway_user_cannot_read_or_create_import_doctypes`; `assert_cannot` at all 19 call sites | PASS. Real `frappe.has_permission` against live rules |
| 3 | Item fixtures insert under India Compliance | `test_an_item_fixture_inserts_on_this_site`, `test_an_item_fixture_has_a_valid_hsn_when_india_compliance_is_installed` | PASS on v16 (its India Compliance branch runs there); on v15 only the plain branch runs |
| 4 | Item files carry an HSN under India Compliance | `test_an_item_file_has_an_hsn_column_when_india_compliance_is_installed` | PASS. The "in particular" clause needs a product fix in `_stand_in`, which the user moved to #34395 |
| 5 | Without India Compliance nothing changes | `test_an_item_fixture_sets_no_hsn_without_india_compliance`; v15 runs | PASS. All 20 affected modules green on v15 |
| 6 | GSTIN assertion follows the site | `assert_gstin_and_email_follow_the_site` (Tally ledgers, CSV and XML) | PASS. `tax_id` branch on v15, `gstin` branch on v16; 0 GSTIN failures on v16 (2 before) |
| 7 | Email assertion follows the site | the same helper; `test_a_hidden_email_field_is_listed_as_not_on_this_site` | PASS. Both branches run on v15 |
| 8 | Product code untouched | diff review | PASS |
| 9 | CI job with India Compliance | — | Out of scope; moved to #34418 (user decision) |

## Red evidence (tester-v16)

The commit changes tests only, so `red_check.sh` doesn't apply. The pre-fix v16 baseline (da17554) shows
exactly the failures the bug describes: 63 `Could not find Row #1: Role: Blogger`, 101
`HSN/SAC Code is required`, 2 `'gstin' != 'tax_id'`.

## Runs

**v15 (fac.localhost):** 20 affected modules, all PASS: test_base_test_fixtures 6,
apply_import_preset 49, apply_import_preset_xml 30, document_batch 25, import_dry_run 30,
import_fixes 35, import_google_sheet 19, import_mapping 22, import_masters 29,
import_opening_stock 78, import_plan 36, import_session 36, import_undo 96, import_update 38,
plan_openings 27, run_migration_plan 28, migration_report 21, import_guardrails 32,
import_failures 27, undo_import_tools 47. Nothing was left behind on the site.

**v16 (fac16.localhost):** all 26 data-migration modules plus `test_base_test_fixtures`.
- **Failing tests:** 473 before, 320 now.
- **Regressions:** none. Every test failing now also failed on da17554, compared by full test name.
- **Now fully passing:** base_test_fixtures, apply_import_preset, apply_import_preset_xml, document_batch,
  import_mapping, import_session, plus the modules that were already green.

The 320 remaining v16 failures, each classified:

| Count | Signature | Belongs to |
|---|---|---|
| 305 | `'types.SimpleNamespace' object has no attribute '_uses_tree_aliases'` | #34395 |
| 7 | Knock-ons of #34395 (a file never ready, every row rejected) | #34395 |
| 1 | `prices.csv` HSN through the dry run's `_stand_in` | #34395 (user decision) |
| 5 | `Cannot enable Has Batch No as Activate Serial / Batch No for Item is disabled` | #34428 (user decision) |
| 1 | `KeyError: None` in `test_accounts_frozen_till_falls_back_to_accounts_settings_for_every_company` | Pre-existing on da17554; not yet assigned |
| 1 | `test_a_validate_only_list_reaches_the_model_fenced`: v16's `validate_email_address` quotes only the parsed address | **New** v16 test-portability gap, not covered by any bug (see below) |

## Concerns (not blocking)

1. **A new v16 test-portability gap.** `test_import_guardrails.test_a_validate_only_list_reaches_the_model_fenced`
   expects the full injection text back in v15's email error; v16 Frappe quotes only the parsed address
   (`frappe/utils/__init__.py:181-220`). Needs a home: a fix in #34412 or a new item.
2. The India Compliance branches (`gstin`, HSN) are checked only on fac16; GSTIN could also be tested on v15
   with a temporary Custom Field. Optional.
3. `item_hsn_fields()` returns `{}` silently when Item has `gst_hsn_code` but no GST HSN Code record exists. Minor.
4. Two call sites check one right short of the documents they use
   (`test_a_record_the_user_may_not_change_stops_the_update_list`: Guest; `test_import_session` setUp:
   Data Import rather than FAC Import Session). Minor.
5. Stale `.pyc` files still match a plain `grep -rn Blogger`. Housekeeping.
