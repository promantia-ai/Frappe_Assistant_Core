# Migration Sign-off

Use when the user asks what a data migration brought in, for a migration or sign-off report, whether
they can go live, or for a copy of the report to keep.

## 1. Run the report

Call `generate_report` with `report_name: "Migration Sign-off"`. Filters, all optional:

- `import_session`: the session ids of the migration, comma separated.
- `from_date` / `to_date`: without sessions, the sessions started between these dates. Leave them out
  and the current fiscal year is used.
- `user`: whose sessions; the user's own by default.

Every number in the report is counted by ERPNext from the files and from the books. Never add up,
round or change a number yourself: quote the report's values.

## 2. Tell the user

Start with the report's `message` and its `report_summary` cards (rows in the files, imported, left out,
failed, not imported, ready to sign off). Then each `section` of the rows:

- **Records**: per DocType, the rows in the files, imported, left out, failed and not imported. They
  always add up to the rows in the files.
- **Why Not Imported**: for each reason, how many rows and which ones (row numbers in the file).
- **Opening Balances**: stock value, receivables and payables per company: in the files, the part not
  imported, the files' amount for the rows imported, in ERPNext, and the difference. A receivable or
  payable is compared on the invoices' totals, so a payment after go-live is not a difference.
- **Trial Balance**: the opening balances file's debits and credits, the accounts that differ from
  ERPNext at the cut-off, ERPNext's debit and credit, and what is left on Temporary Opening (zero once
  every opening balance is in).
- **Open Points**: what stands in the way of signing off. Go through them one by one. Failed rows can
  be fixed and imported again (`dry_run_import`, then the import).

The same report opens in Desk: **Migration Sign-off**, for the accountant to check or print.

## 3. The PDF, when the user wants a copy

Call `generate_document` with:

- `title`: "Data migration sign-off report"; `filename`: "Migration sign-off" and today's date;
  `orientation`: "landscape".
- `content`: the report as markdown: the message, then one table per section with the report's own
  columns, each value copied exactly as the report gives it. Leave out no row. End with a sign-off
  block: "Name:" and "Date:" lines.

Share the `download_link` exactly as `generate_document` returns it.
