# How to Use apply_import_preset

## Overview

Recognises an export from another accounting system by its headers, cleans it, and opens one
import session per target with the mapping applied. The user's upload is not changed: each
session works on a cleaned copy attached to it. Call it first for any spreadsheet the user wants
to import; when it answers `matched: false`, carry on with `start_import_session`.

Systems: Tally Prime, Zoho Books, Busy, Marg, QuickBooks Online and Desktop. Exports: party
masters, item masters, chart of accounts, trial balance, outstanding bills (or A/R and A/P
aging), stock summary.

## What cleaning does

- Finds the header row under title rows (company name, report name, period).
- Leaves out blank rows, totals ("Grand Total", "TOTAL", "Total for ..."), report footers,
  repeated headers and group rows (for example "Current Assets" above its ledgers).
- Reads amounts like `8,45,000 Dr`, `(1,250)`, `-$175,804.25`; dates like `1-Apr-24`;
  quantities like `10 Nos` (the unit becomes its own column).
- Splits rows by target: a Tally ledger list gives Customers (Sundry Debtors), Suppliers
  (Sundry Creditors) and Accounts (every other group).

## Reading the reply

| Key | Tell the user |
|---|---|
| `preset` | Which system and export was recognised. When `verified` is false, say the preset has not been checked against a real export of that system and ask them to check the mapping. `how_to_export` says how the export is made, if they need to export again. |
| `dropped` | How many title, total, group and other rows were left out, and why. |
| `sessions` | One per target: its rows and mapping. Show the mapping as a table, as with `set_column_mapping`. |
| `needs` | A target that needs an answer, for example the company for Accounts or the warehouse for a stock summary. Ask, then call again with `values` and `targets` set to those targets only, so the other sessions are not opened twice. |
| `no_target_values` | Rows that fit no target, by value. They are not imported. |
| `not_on_this_site` | Columns whose field this site doesn't have (for example GSTIN fields, HSN, GST category or PAN without India Compliance). GSTIN goes to Tax ID instead. |
| `skipped_targets` | Targets the user may not import into, with the reason. |
| `also_matched` | Other presets that fit as well. When the export could be either, for example Zoho customers or vendors, ask which and call again with `preset`. |

## Next steps

- A session with `doctype`: show its mapping, then `dry_run_import`. The user can change any
  column with `set_column_mapping` first.
- A session with `engine`: ask the company and the cut-off date (and anything else that tool
  asks), then call the tool in `next` (`set_opening_balances`, `set_opening_invoices` or
  `set_opening_stock`) with the returned `columns` (and `invoice_type` for invoices).

## Not imported

- Opening balances in party or ledger masters: bring openings in with the trial balance,
  outstanding bills and stock summary exports instead.
- Addresses: Customer and Supplier have no address fields in an import. They need a separate
  Address import.
- HSN, GST rate and GST category, unless India Compliance is installed.

## Parameters

| Parameter | Required | Description |
|---|---|---|
| `file_id` or `file_url` | Yes | The uploaded file |
| `sheet` | No | Sheet to use; default the first sheet a preset recognises |
| `preset` | No | A preset id, when the user says which export it is |
| `values` | No | Answers to `needs`, by name |
| `targets` | No | Open only these targets |
