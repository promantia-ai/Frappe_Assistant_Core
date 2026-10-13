# How to Use start_import_session

## Overview

The `start_import_session` tool starts a data import for a spreadsheet the user wants to import. It opens an **import session**: a record on the site that keeps the file's details, the decisions made along the way (mapping, fixes, validation results) and a log of every step. The user can carry on in a later message. **Keep the `session_id`.**

**No copy of the file is saved.** The file stays where the user attached it, and every later step reads it from there. The session is the audit record: it holds only the file's name, sheet, columns and row count, never its rows.

Call it when the user asks to **import** a file, not just to look at one.

## Parameters

Give the file in one of two ways:

| Parameter | Type | When |
|-----------|------|------|
| `file_url` | string | The file is **on this site**, e.g. a FAC Chat attachment such as `/private/files/customers.xlsx`. The tool reads its sheets itself. |
| `file_name` + `columns` + `row_count` (+ `sheet`) | string, array, integer | The file is **attached in this chat** (Claude, ChatGPT) and not on the site. Read the file yourself and pass its name, the sheet you used, the column headers and the number of data rows (not counting the header). **Never pass its rows.** |

Other parameters:

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `sheet` | string | No | For `file_url`: the sheet to use. Default: the first sheet with rows. For `file_name`: the sheet you read. |
| `target_doctype` | string | No | DocType the rows become, if already known. The user's permission to import into it is checked **before** anything else. |
| `import_type` | string | No | `Insert New Records` (default) or `Update Existing Records`. |

Supported files: `.csv`, `.xlsx`, `.xls`. **Row 1 must hold the column headers.** One file holds one kind of record, e.g. customers.

## Response Format

```json
{
  "success": true,
  "session_id": "IMP-2026-00012",
  "file_name": "customers.xlsx",
  "sheet": "Customers",
  "columns": ["Customer Name", "Customer Group", "Territory"],
  "row_count": 15,
  "sample_rows": [["Grant Plastics", "Commercial", "India"]],
  "sheets": [{"name": "Customers", "columns": ["Customer Name", "Customer Group", "Territory"], "row_count": 15}],
  "target_doctype": null,
  "status": "File Read"
}
```

- `sample_rows` (up to 10) come back only for `file_url`, so you can show the user what was read. They are text the way Data Import will read it: dates as `YYYY-MM-DD`, whole numbers without `.0`. They are **not stored** on the session. `row_count` skips blank rows.
- `note` appears when other sheets of a workbook also have rows. Only one sheet is used per session.

## Next

1. Work out the target DocType from the columns (unless the user already said), and tell the user which one and why.
2. `get_import_schema(doctype)` for the fields the columns can map to.
3. `set_column_mapping(session, mapping, doctype)` to save the mapping, then show the user the table it returns.
4. `dry_run_import(session)` to check every row before anything is imported.

## Coming back later

Read the session with `get_document`:

```json
{"doctype": "FAC Import Session", "name": "IMP-2026-00012"}
```

Its `status`, `template_options` (mapping), `transformation_rules`, `validation_summary` and `steps` (the log) say where the import stands. `file_source` says where the file is: `Site File` or `Chat Attachment`. In Desk the user finds every session under **FAC Import Session**.

## Best Practices

1. **Tell the user what you recorded:** sheet, columns and row count, plus any `note`.
2. **Pass `target_doctype` when the user has already said what the file is,** so a user who cannot import into it is told straight away.
3. **Never invent rows,** and never pass the file's rows.

## Edge Cases

- **Target not allowed** — returns `success: false` with the reason: no Create permission, no Import permission, Allow Import turned off for the DocType, or a child table (import it through its parent).
- **File not found or not yours** (`file_url`) — a private file belonging to another user is reported the same way as a missing file.
- **Missing details** (`file_name`) — `columns` and `row_count` are required.
- **Unknown sheet or no rows** (`file_url`) — refused, listing the sheets that exist.
- **Who sees a session** — the user who started it, and System Managers.
