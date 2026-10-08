# How to Use dry_run_import

## Overview

The `dry_run_import` tool checks every row of an import session's file **the way the real import would, and writes nothing**. Each row goes through ERPNext's own insert (required fields, links, duplicates and the DocType's own rules, such as "Cannot select a Group type Customer Group"), and is then rolled back. Emails and other side effects are held back.

Data Import's own preview catches only bad links, options and dates. This catches everything a real import would fail on, so the user fixes everything in one pass.

Call it once the columns are mapped (`set_column_mapping`), and again after every round of fixes.

## Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `session` | string | **Yes** | The import session id |
| `file_url` | string | No | Only when the session's file is just in the chat (Claude, ChatGPT): the same file on this site, e.g. a FAC Chat attachment. Its columns must match the session's. The mapping is kept. |

## Response Format

```json
{
  "success": true,
  "session": "IMP-2026-00012",
  "status": "Dry Run Failed",
  "message": "10 rows ready, 5 would fail. Nothing was written.",
  "total_rows": 15,
  "ready": 10,
  "failed": 5,
  "excluded": 0,
  "duplicate_key": "customer_name",
  "groups": [
    {
      "id": "G1",
      "cause": "Missing link",
      "field": "customer_group",
      "label": "Customer Group",
      "value": "Retial",
      "message": "Customer Group 'Retial' doesn't exist.",
      "count": 2,
      "examples": [{"row": 7, "values": {"Customer Name": "Kaveri Textiles", "Customer Group": "Retial"}}]
    },
    {
      "id": "G2",
      "cause": "Rejected by ERPNext",
      "error": "ValidationError",
      "message": "Cannot select a Group type Customer Group. Please select a non-group Customer Group.",
      "count": 1,
      "examples": [{"row": 9, "values": {"Customer Name": "Metro Hardware Mart", "Customer Group": ""}}]
    }
  ],
  "previous": {"failed": 7, "fixed": 2}
}
```

- **Rows** are numbered as in the spreadsheet: row 1 is the header, so the first data row is row 2.
- **`cause`** is one of: `Missing link`, `Not an allowed option`, `Wrong date format`, `Invalid value`, `Duplicate in the file`, `Already in ERPNext`, `Rejected by ERPNext` (the DocType's own rules; `error` is the exception type).
- **`previous`** appears on a second check: how many rows failed last time, and how many of those now pass.
- **`status`** becomes `Ready to Import` when nothing fails, else `Dry Run Failed`.

The full per-row list is attached to the session (`validation_results`). You get only the groups and up to 3 examples each.

## Large files

A file of more than 500 rows is checked in the background. The reply has `"state": "running"`. Tell the user, then read the session later with `get_document` (doctype `FAC Import Session`): `progress` shows how far it got, and `validation_summary` holds the result once `status` changes. Calling the tool again while it runs only reports progress.

## Best Practices

1. **Lead with the headline** ("10 rows ready, 5 would fail"), then one line per group: cause, count, an example row. Never list every failed row.
2. **Duplicates are their own groups.** "Already in ERPNext" may mean the user wants to update those records instead, so ask.
3. **After fixes, check again** and report only what still fails, plus how many were fixed (`previous`).
4. **Nothing is imported.** Say so if the user seems to think it was.

## When it can't run

- **Not mapped yet:** map the columns first with `set_column_mapping`.
- **File only in the chat:** the rows can't be read on the site. Ask the user to attach the file in FAC Chat, then call again with its `file_url`.
- **No permission** to create or import the target DocType: tell the user and stop.
- **DocTypes whose side effects can't be held back** (DocType, Custom Field, Payment Request, or any the site lists): the check is refused, with the reason.
- **Already importing or imported:** a finished session isn't checked again.

## Next

Explain each group in plain words and agree fixes with the user: map a value to an existing one, create the missing records, or leave the rows out. Then call `dry_run_import` again.
