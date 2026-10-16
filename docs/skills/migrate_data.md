# Migrate Data

Use when the user wants to bring data from another system into ERPNext: "migrate my data", "import
my customers", "bring my data from Tally", "load my item master", "move our opening balances", or
attaches a spreadsheet and says to import it. Follow this workflow every time, however the request
is worded. Each tool has its own skill page with the details; this page is the order and the rules
that hold across them.

## The workflow, one file at a time

Every file goes through the same steps, in this order. Never skip one, and never import before the
check says the file is ready.

| # | Step | Tool | Done when |
|---|---|---|---|
| 1 | **Session**: open one for the file | `start_import_session` | You have the `session_id`, sheet, columns and row count |
| 2 | **Mapping**: file columns to ERPNext fields | `get_import_schema`, then `set_column_mapping` | The user agreed the mapping table |
| 3 | **Cleaning**: value fixes the user picks (map a value, leave rows out) | `dry_run_import` once to find the groups, then again with `fixes` | Every group has a decision |
| 4 | **Dry run**: check every row, nothing written | `dry_run_import` | `status` is `Ready to Import` |
| 5 | **Fixes**: create the missing masters, then check again | `create_document` with `documents` and `import_session`, then `dry_run_import` | No group is left |
| 6 | **Import**: with one approval, only when the user asks | `start_import` | The session has left Importing |
| 7 | **Failed rows**: explain, try again, or give them as a file | `dry_run_import`, then `start_import`; or `dry_run_import` with `failed_rows_file` | Every row is imported or left out with a reason |
| 8 | **Report**: what the migration brought in | `get_skill("migration-sign-off")` | The user has the report and its PDF |

Steps 3 to 5 repeat until nothing fails: explain each group in its own words, offer only its
`fixes`, apply the user's choices in one call, and give the new count.

**Opening balances use their own setup tool in step 2** instead of `set_column_mapping`. They also
have their own import tool in step 6 instead of `start_import`:

| File | Step 2 | Step 6 |
|---|---|---|
| Stock on hand at the cut-off | `set_opening_stock` | `import_opening_stock` |
| Unpaid customer invoices / supplier bills | `set_opening_invoices` | `import_opening_invoices` |
| Closing trial balance | `set_opening_balances` | `import_opening_balances` |

Ask for the **company** and the **cut-off date** (the last day in the old system) once, and use the
same date for all three. Never guess either of them.

## The order across files

Records must exist before the records that link to them. When the user has several files, show
this order as the plan, and work through it one file at a time:

1. **Chart of accounts.** The accounts must be right before any transaction is posted. Replacing
   the chart is possible only on a company with no transactions yet, with ERPNext's **Chart of
   Accounts Importer** in Desk. No import tool does that. Adding missing accounts to an existing
   chart is a normal import of `Account`.
2. **Setup masters**: Payment Terms, Customer Groups, Supplier Groups, Item Groups, Territories,
   Warehouses, UOMs, Price Lists.
3. **Parties and items**: Customers, Suppliers, Items.
4. **What links to them**: Item Prices, Addresses, Contacts.
5. **Opening stock** (needs items and warehouses).
6. **Opening receivables and payables** (needs customers and suppliers).
7. **Opening trial balance** (needs the accounts). Steps 5 to 7 can come in any order among
   themselves: Temporary Opening holds the difference until all three are in.
8. **The sign-off report.**

If a file needs records from a later file (for example, Customers whose group is in the Customer
Group file), import the other file first. Use step 5 only for a few missing masters the file
doesn't bring itself.

## What goes wrong without this skill

- **Steps out of order.** Importing before the dry run, or starting a new session for failed rows.
  A new session imports the done rows again. Retry failed rows, continue an interrupted import, or
  bring in a corrected file, always in the **same session**.
- **Opening stock through Stock Entry or Data Import.** Opening stock is always a **Stock
  Reconciliation with purpose Opening Stock**, made by `set_opening_stock` and
  `import_opening_stock`. A Stock Entry posts it to an expense account and the books are wrong.
- **Transactions before the chart of accounts.** Once any GL Entry exists, the chart can't be
  replaced. Check the accounts before opening invoices, stock or balances.
- **Sending emails.** The dry run and `start_import` run with emails muted, so imported records
  send no welcome mails or notifications. Never create a file's records one by one with
  `create_document` or `update_document` instead: emails are not muted there, and a migration of
  500 customers must not email 500 customers.
- **Reading the whole file.** Never read a migration file in full, page through its rows, or paste
  rows into the chat or a tool call. Not with `extract_file_content`, `run_python_code` or
  `get_document` either. The import tools read the file on the site and give you a summary:
  columns, row count, up to 10 sample rows, and grouped failures with row numbers. Work from those.
- **Guessing.** Never invent a value, a mapping or a name. When you aren't sure what a column
  means, ask with a choice card (`ask_user`). A fix maps only to a name the tool listed in
  `closest`, or one the user gave.
- **Adding up numbers yourself.** Row counts, totals and differences come from the tools. Quote
  them as they are.

## Files the tools can take

`.csv`, `.xlsx` or `.xls`, with the column headers in row 1 and **one kind of record per file**. A
workbook with several sheets is one session per sheet.

- **A `.zip`**: ask the user to unzip it and attach the files.
- **A Tally XML export**: it can be looked at with `extract_file_content`, but not imported. Ask the
  user to export each list from Tally to Excel instead (ledgers, stock items, outstanding bills,
  trial balance), one list per file.
- **A file larger than Max Rows per Import**: refused with the limit. Ask the user to split it.

## Off-topic questions during a migration

The user may ask something unrelated in the middle of a migration ("what were our sales last
month?"). Answer it fully, as you would any question. Then offer to carry on, saying where the
migration stands, for example: "Shall we carry on with the customer file? The check found 3 groups
still to decide." Don't refuse the question, and don't drop the migration.

The session keeps every step, so a migration can also continue in a later chat. To find where it
stands, read the user's sessions with `list_documents` on `FAC Import Session` (its `status`), and
open one with `get_document`. Carry on from its status. Never start the file again.

## Talking to the user

- Plain business words: "412 customers use a group that doesn't exist", not the ERPNext error.
- One decision at a time, grouped, never row by row.
- Say what each step will do before an approval card, and what it did afterwards, with the counts.
- After each file, say what's next in the order above.
