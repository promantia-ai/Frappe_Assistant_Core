# How to Use import_opening_stock

## Overview

Creates and submits the checked opening stock, after the user approves: Stock Reconciliations with purpose
Opening Stock, up to 1,000 rows each, each all or nothing. Use it only after `dry_run_import` said the sheet
is ready, and only when the user asks to import.

## Parameters

Pass exactly what the last check reported, so the approval card shows it: `session`, `company`,
`posting_date`, `rows` (the check's `ready`), `total_qty` and `total_value` (from the check's
`opening_stock`). If they differ from the check, nothing is created.

## The reply

- `documents`: the Stock Reconciliations made.
- `warehouses`: per warehouse, the file's quantity and value next to ERPNext's (from the stock ledger) and the
  difference. `differences` lists the warehouses that don't match. Show this table.
- `pending`: documents ERPNext is still submitting in the background (more than 100 rows). Call again later
  with the same values: nothing is created twice.
- `failed_documents`: drafts ERPNext never submitted (its background submit failed, and it left them as
  drafts). The session is then Failed or Partially Completed. Check the sheet again with `dry_run_import` and
  call this again: it deletes those drafts first, so the same stock can't be brought in twice. Tell the user.
- `reposting`: ERPNext is still recalculating stock values; say values can still change.
- `accounting`: which account was posted, or that perpetual inventory is off.

## When it stops

- A chunk ERPNext refuses is rolled back whole and its reason is given. Earlier chunks stay in. Fix the cause,
  call `dry_run_import` (it shows the rows already in as `imported` and checks only the rest), then call this
  again with that check's values.
- If stock moved after the check, it refuses: check again first.
- While ERPNext is still submitting a document from an earlier try (`pending`), it refuses: wait, then call
  again, so the same stock can't be brought in twice.

## Things to tell the user

- Opening stock **sets** the balance. Items that already had stock in that warehouse were refused by the check.
- The Temporary Opening account is cleared later by the opening balances (trial balance) import.
