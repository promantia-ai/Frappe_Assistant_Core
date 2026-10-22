# #34430 Requirement: test suite portable to Frappe v16 and to sites with India Compliance

## Bug

- **#34430** "Test suite isn't portable to Frappe v16 or to sites with India Compliance". Priority High,
  target v3.2.x, reporter Paul Clinton. Related: #34412 (tests that can't fail), #34418, epic #33986.
- No notes in Redmine beyond the two relation entries; the description is the whole spec.
- Paul's fix list: (1) remove the Blogger dependency, (2) give fixture Items an HSN code when India
  Compliance (IC) is installed, (3) make the GSTIN assertion depend on whether IC is installed,
  (4) add a CI job with ERPNext + IC on the `version-16` tip.
- Paul ran Frappe 16.52 / ERPNext 16.14 / IC 16.3.2 (1,093 tests). Our fac16 is Frappe 16.52 /
  ERPNext 16.52 / IC 16.11.0.

## Problem

This is a **test-only** bug: the product code is right; the fixtures and two assertions assume a
v15 site without IC. On fac16 (baseline 2026-10-22, `/home/madesh/sddscripts/v16-baseline/`) three
causes produce 166 failures/errors that have nothing to do with the code under test, and a fourth
is hidden behind one of them.

1. **Role "Blogger" doesn't exist on v16.** `make_throwaway_user(..., roles=("Blogger",))`
   (`tests/base_test.py:268-286`) inserts a User whose role row links to a missing Role:
   `LinkValidationError: Could not find Row #1: Role: Blogger`. v15 Frappe ships it
   (`frappe/website/doctype/blog_settings/blog_settings.json`); v16 Frappe has no "Blogger" anywhere.
   Current call sites (19, in 10 modules; Paul missed masters and dry_run):
   `test_import_session.py:42,327,347,365,428` · `test_import_google_sheet.py:73` ·
   `test_import_mapping.py:126,265,276` · `test_document_batch.py:102,394` ·
   `test_import_fixes.py:487,562` · `test_apply_import_preset.py:301` ·
   `test_apply_import_preset_xml.py:350` · `test_import_dry_run.py:560` · `test_import_masters.py:505`.
   v16 log count, final exception = Blogger: session 36, google_sheet 16, mapping 3, document_batch 2,
   fixes 2, preset 1, preset_xml 1, dry_run 1, **masters 1** = 63.
2. **Items without an HSN code fail under IC.** IC's Item `validate` hook
   (`india_compliance/gst_india/overrides/item.py:25-30`, Paul's ":30" is right) calls
   `validate_hsn_code` (`gst_india/doctype/gst_hsn_code/gst_hsn_code.py:115-133`) for every sales item
   (`is_sales_item` defaults to 1). On fac16 GST Settings has `validate_hsn_code = 1`,
   `min_hsn_digits = 6`, and 18,687 GST HSN Code records. Direct-insert fixtures that fail:
   - `test_import_opening_stock.py:84-94` `_item` (called from `setUp` :34-35); shared by every
     `OpeningStockCase` subclass: `test_plan_openings.py:54` (StockPlanCase),
     `test_import_undo.py:1783`, `test_migration_report.py:350`.
   - `test_import_undo.py:434-446` and `:455-467` (`_item_with_a_price`).
   - `test_import_update.py:562-564` and `:617-619`.
   v16 count, final exception = `MandatoryError: HSN/SAC Code is required`: opening_stock 78,
   plan_openings 15, undo 5, update 2, migration_report 1 = 101.
   **Also affected, but hidden today by #34395:** Item *files* sent through the importer have no HSN
   column: `test_import_plan.py:579-586` (`_item_file`) and `:628-634`, `test_plan_openings.py:55-61`
   (`_items`), `test_run_migration_plan.py:45-49`. Already visible in
   `test_import_plan.test_an_earlier_files_failing_row_still_counts_as_coming` (:680-700): the
   prices.csv question reads "HSN/SAC Code is required" (log line 274).
3. **GSTIN maps to `gstin` under IC.** The shipped presets map `"GSTIN": ["gstin", "tax_id"]`
   (`data/import_presets/tally_prime.json:30,33`, likewise busy, marg, zoho_books), and
   `ApplyImportPreset._map` (`plugins/core/tools/apply_import_preset.py:526-552`, pick at :536-537)
   takes the first candidate the site's schema has. IC adds `gstin` to Customer/Supplier, so the
   preset is right and the tests are wrong: `test_apply_import_preset.py:965`
   (`test_tally_ledgers_ac1`) and `test_apply_import_preset_xml.py:145`
   (`test_ledgers_open_customers_and_suppliers_and_ask_for_the_company`) fail with
   `AssertionError: 'gstin' != 'tax_id'`. Paul's claim is confirmed.
   `test_gstin_goes_to_the_first_field_this_site_has` (:138) uses `gstin_not_here` and passes on v16.
4. **Found here, not in Paul's report: Email is not importable on v16.** The very next assertion in
   both tests, `mapped["Email"] == "email_id"` (`test_apply_import_preset.py:966`,
   `test_apply_import_preset_xml.py:146`), will fail on v16 once the GSTIN line is fixed. ERPNext 16
   marks Customer/Supplier `email_id` and `mobile_no` `hidden: 1` (v15: not hidden), and
   `utils/import_mapping.py:83` drops hidden fields, so the v16 result lists
   `not_on_this_site: ['Email (email_id)', 'Mobile (mobile_no)']` (v16 preset log line 157).

**Correction to today's baseline numbers:** test_plan_openings did not run only 4 tests on v16. The v16
runner prints two summaries for that module: "Ran 23 tests … FAILED (errors=15)" (integration: 8
invoice tests passed, all 15 StockPlanCase tests errored in `setUp`) and "Ran 4 tests … OK" (the plain
`unittest.TestCase` class at :463). The baseline TOTAL took the last line. v16 actually ran
1,029 + 23 = **1,052** tests, the same as v15.

## Reproduces on

- **v16 only** on this machine. Blogger: Frappe v16 (not v15). HSN, GSTIN: any site with IC
  (independent of version; we have no v15 + IC site). Email: ERPNext v16.
- v15 (fac.localhost, no IC, Blogger role present): all 1,052 tests pass; nothing reproduces.

## Acceptance criteria

1. **No Blogger dependency.** Given a v16 site with no Role "Blogger", when the 26 data-migration
   modules run, then no test errors with `Role: Blogger`, and `grep -rn Blogger frappe_assistant_core/tests/`
   returns nothing.
2. **The "other user" still has no rights.** Given the user each of the 19 call sites now creates,
   when the test runs, then that user exists on both sites, is a desk (System) user like the Blogger
   user was, and has no read, create or write permission on the doctype the test checks (Customer,
   Supplier, File, FAC Import Session, or the target doctype). This is asserted in the tests, so a
   role that silently grants access makes them fail (#34412).
3. **Item fixtures insert under IC.** Given fac16 (IC, `validate_hsn_code = 1`, `min_hsn_digits = 6`),
   when every direct Item fixture listed in Problem 2 runs, then the Item inserts, with a
   `gst_hsn_code` that exists as a GST HSN Code and is a valid length for the site's setting, and no
   test in opening_stock, plan_openings, undo, update or migration_report ends in
   `HSN/SAC Code is required`.
4. **Item files carry an HSN under IC.** Given fac16, when a test builds an Item CSV for the importer
   (`test_import_plan.py` :579, :628; `test_plan_openings.py` :55; `test_run_migration_plan.py` :45),
   then the file has an HSN column mapped to `gst_hsn_code`, and no dry run or plan question in those
   modules reports "HSN/SAC Code is required". In particular
   `test_an_earlier_files_failing_row_still_counts_as_coming` gets exactly the Brand question.
   *Fully verifiable only after #34395* (these rows fail on the SimpleNamespace error first).
5. **Without IC nothing changes.** Given fac.localhost (no IC, no `gst_hsn_code` field), when the same
   fixtures run, then they set and map no HSN field, and the v15 baseline is still 1,052 tests, all
   passing.
6. **GSTIN assertion follows the site.** Given a site whose Customer has a `gstin` field (IC), when
   `test_tally_ledgers_ac1` and the XML ledgers test run, then they assert GSTIN → `gstin`; given a
   site without it, they assert GSTIN → `tax_id`. Both branches assert; neither is a skip.
7. **Email assertion follows the site.** Given v16 (Customer `email_id` hidden), when those two tests
   run, then they assert Email is not mapped and `Email (email_id)` is in `not_on_this_site`; on v15
   they still assert Email → `email_id`.
8. **Product code untouched.** The preset JSON files, `apply_import_preset.py` and
   `import_mapping.py` are not changed by this bug; only test files (and CI, criterion 9).
9. **CI job with IC on version-16.** `.github/workflows/ci.yml` (today: matrix version-15/16 with
   ERPNext only, :22-30, :109-123) gains a job on the `version-16` tip that installs ERPNext and
   India Compliance and runs the app's tests. Checked by reading the workflow; it only runs on GitHub
   after a push, which is the user's decision.
10. **v16 overall.** Given the fix, when `baseline.sh v16` runs, then the logs contain zero
    `Role: Blogger`, zero `HSN/SAC Code is required`, and zero `'gstin' != 'tax_id'`; and
    test_document_batch, test_import_mapping, test_apply_import_preset and
    test_apply_import_preset_xml are **OK** on v16 (all their v16 failures today are this bug's).
    Every remaining v16 failure carries the #34395 signature
    (`'types.SimpleNamespace' object has no attribute ...`) or follows from it.
    *Fully verifiable only after #34395:* test_import_session, test_import_google_sheet,
    test_import_fixes, test_import_dry_run, test_import_masters, test_import_opening_stock,
    test_plan_openings, test_import_plan, test_import_undo, test_import_update, test_migration_report,
    test_run_migration_plan being green on v16, and criterion 4 in full.

## Tests to add

Red evidence for a test-only bug is the v16 baseline itself (the current tests error on fac16; logs
above). `red_check.sh` (old product code + new tests) does not apply in its usual sense; the new
guard tests below must instead fail when the helper is reverted to the old fixture.

| Criterion | Module | Test names |
|---|---|---|
| 1, 2 | `tests/test_base_test_fixtures.py` (new) | `test_the_throwaway_role_exists_on_this_site`, `test_a_throwaway_user_is_a_desk_user`, `test_a_throwaway_user_cannot_read_or_create_import_doctypes` |
| 2 | each of the 10 modules in Problem 1 | the existing "other user / cannot read" tests, each asserting its user lacks the permission before acting |
| 3 | `tests/test_base_test_fixtures.py` | `test_an_item_fixture_inserts_on_this_site`, `test_an_item_fixture_has_a_valid_hsn_when_india_compliance_is_installed` |
| 4 | `tests/test_import_plan.py` | update `test_an_earlier_files_failing_row_still_counts_as_coming` (exactly one question, the Brand one); `test_an_item_file_has_an_hsn_column_when_india_compliance_is_installed` |
| 5 | `tests/test_base_test_fixtures.py` | `test_an_item_fixture_sets_no_hsn_without_india_compliance` (simulated: IC reported not installed / field absent) |
| 6 | `tests/test_apply_import_preset.py`, `tests/test_apply_import_preset_xml.py` | update `test_tally_ledgers_ac1`, `test_ledgers_open_customers_and_suppliers_and_ask_for_the_company` |
| 7 | same two modules | same two tests (Email branch); `test_a_hidden_email_field_is_listed_as_not_on_this_site` |
| 8, 9 | (review) | tester reads the diff and `ci.yml` |
| 10 | all 26 modules | `baseline.sh v15` and `baseline.sh v16` |

Existing tests that are wrong: the 19 Blogger call sites, the 4 GSTIN/Email assertions above, and the
Item fixtures in Problem 2. None of them can pass on an IC/v16 site today.

## Browser check

No. Nothing a user sees changes.

## Out of scope

- **#34395** (the dry run's `SimpleNamespace` stand-in): about 300 further v16 failures. Don't fix
  or work around it here; criteria 4 and 10 name what waits for it.
- Whether the importer should offer Email/Mobile on v16 (e.g. through the primary Contact) is
  product behaviour, not this bug.
- `chat/tests/test_role_aware_default_prompts.py:53,63` uses "Blogger" only as a string (no Role
  record), so it is unaffected. Leave it alone.
- The baseline TOTAL undercount (it reads only the last "Ran" line per module) is in our scripts, not
  in FAC. For the orchestrator, not the developer.
- AR / FAC Cloud: not involved.

## Open questions

1. CI job (criterion 9): Paul asks for it, but it can only be proven by a push, and it adds an IC
   install to every CI run. Should it be in this bug's commit? If yes, should it run the whole app or
   only the data-migration modules?
