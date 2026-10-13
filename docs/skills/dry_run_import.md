# How to Use dry_run_import

## Overview

The `dry_run_import` tool checks every row of an import session's file **the way the real import would, and writes nothing**. Each row goes through ERPNext's own insert (required fields, links, duplicates and the DocType's own rules, such as "Cannot select a Group type Customer Group"), and is then rolled back. Emails and other side effects are held back.

Data Import's own preview catches only bad links, options and dates. This catches everything a real import would fail on, so the user fixes everything in one pass.

Call it once the columns are mapped (`set_column_mapping`). Then call it again with the user's `fixes`: they are saved on the session and every row is checked again.

## Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `session` | string | **Yes** | The import session id |
| `file_url` | string | No | Only when the session's file is just in the chat (Claude, ChatGPT): the same file on this site, e.g. a FAC Chat attachment. Its columns must match the session's. The mapping is kept. A **different** file puts back the rows left out before (`notice` says so). Never send it with `fixes`. |
| `fixes` | array | No | The user's fixes for the last check's groups: `{group, action, to?, reason?}`. See **Fixing the groups**. |

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
      "id": "G3fa2c1",
      "cause": "Missing link",
      "parent": "Customer",
      "field": "customer_group",
      "label": "Customer Group",
      "value": "Retial",
      "message": "Customer Group 'Retial' doesn't exist.",
      "count": 2,
      "examples": [{"row": 7, "values": {"Customer Name": "Kaveri Textiles", "Customer Group": "Retial"}}],
      "explanation": "2 rows: there is no Customer Group called 'Retial' in ERPNext.",
      "fixes": [
        {"action": "map", "closest": ["Retail"]},
        {"action": "create", "use": "create_document", "doctype": "Customer Group", "name": "Retial", "parent": "All Customer Groups"},
        {"action": "leave_out", "rows": 2}
      ]
    },
    {
      "id": "G8b01d4",
      "cause": "Rejected by ERPNext",
      "error": "ValidationError",
      "message": "Cannot select a Group type Customer Group. Please select a non-group Customer Group.",
      "count": 1,
      "examples": [{"row": 9, "values": {"Customer Name": "Metro Hardware Mart", "Customer Group": ""}}],
      "explanation": "1 row rejected by ERPNext: Cannot select a Group type Customer Group. ...",
      "fixes": [{"action": "leave_out", "rows": 1}],
      "fix_in_file": true
    }
  ],
  "previous": {"failed": 7, "fixed": 2}
}
```

- **Rows** are numbered as in the spreadsheet: row 1 is the header, so the first data row is row 2.
- **`cause`** is one of: `Missing link`, `Not an allowed option`, `Wrong date format`, `Invalid value`, `Duplicate in the file`, `Already in ERPNext`, `Rejected by ERPNext` (the DocType's own rules; `error` is the exception type).
- **`previous`** appears on a second check: how many rows failed last time, and how many of those now pass.
- **`status`** becomes `Ready to Import` when nothing fails, else `Dry Run Failed`. Then `message` also says the file is ready, with the final count.
- **`id`** stays the same from check to check while the group's rows still fail. **`parent`** is the DocType the field is on: a child table's for its columns, e.g. `Sales Invoice Item`.
- **`masters_plan`** lists every missing master that can be created, with its parent and the rows it fixes, e.g. `{"doctype": "Customer Group", "name": "Retial", "parent": "All Customer Groups", "rows": 2, "data": {"customer_group_name": "Retial", "parent_customer_group": "All Customer Groups"}}`. Each entry's `doctype` and `data` go straight into `create_document`'s `documents` (see its **Missing masters of an import**); to change a parent, change it in `data`.
- **`explanation`** is the group's cause in plain words, and **`fixes`** the fixes that fit it (see **Fixing the groups**). `fix_in_file` means the rows must be corrected in the file.

The full per-row list is attached to the session (`validation_results`). You get only the groups and up to 3 examples each.

## Large files

A file of more than 500 rows is checked in the background. The reply has `"state": "running"`. Tell the user, then read the session later with `get_document` (doctype `FAC Import Session`): `progress` shows how far it got, and `validation_summary` holds the result once `status` changes: the same reply as above, with each group's `explanation` and `fixes`, and the ready message. Examples are given as row numbers (`example_rows`), since the session keeps no values from the file. Calling the tool again while it runs only reports progress.

## Best Practices

1. **Lead with the headline** ("10 rows ready, 5 would fail"), then one line per group: its `explanation`, an example row and its fixes. Never list every failed row.
2. **Duplicates are their own groups.** "Already in ERPNext" may mean the user wants to update those records instead, so ask.
3. **After fixes,** report only what still fails, plus how many were fixed (`previous`): "Fixed. 12 rows still need attention."
4. **Nothing is imported.** Say so if the user seems to think it was.

## When it can't run

- **Not mapped yet:** map the columns first with `set_column_mapping`.
- **File only in the chat:** the rows can't be read on the site. Ask the user to attach the file in FAC Chat, then call again with its `file_url`.
- **No permission** to create or import the target DocType: tell the user and stop.
- **DocTypes whose side effects can't be held back** (DocType, Custom Field, Payment Request, or any the site lists): the check is refused, with the reason.
- **Already importing or imported:** a finished session isn't checked again.

## Fixing the groups

Explain each group with its `explanation`, offer only its `fixes`, then call `dry_run_import` again with the user's choices. Pass all their decisions in one call: they're applied together, then the file is checked once.

| `action` | For | What it does |
|----------|-----|--------------|
| `map` | Missing link, value that isn't an allowed option | Every row with the group's value gets `to` instead. Stored as a transformation rule on the session; the file isn't changed. |
| `create` | Missing link, when the record is a simple master | **Not sent in `fixes`.** Create every missing master at once with `create_document`: `documents` from `masters_plan`, with `import_session` (one table, one approval), then call `dry_run_import` again without `fixes`: the rows that needed them now pass. A group without this fix has `cannot_create` saying why. |
| `leave_out` | Any group | Its rows are skipped by the import and listed in its report, with the reason. |

Any other problem (a bad date, a duplicate, a rule ERPNext enforces) is corrected in the file: tell the user which rows and what to change, then start again with the new file. The group says so with `"fix_in_file": true`.

Each fix:

| Key | Description |
|-----|-------------|
| `group` | The group's id, e.g. `G3fa2c1`. A group keeps its id from check to check; once its rows pass, the id is gone. |
| `action` | `map` or `leave_out` |
| `to` | For `map`: the existing value to use |
| `reason` | For `leave_out`: why, in the user's words. It appears in the import report. |

### Closest matches come from the engine

A `map` fix on a missing link lists `closest`: existing names ranked by the engine (spelling, "Retail" inside "Retail Customers", shared words). Disabled records and group nodes are never listed, because a link to them fails.

**Never invent a name.** `to` must be one of `closest`, or a name the user gives you. The tool checks it exists; if not, nothing is saved and the error lists the closest names.

### Example

FACO: "412 rows use Customer Group *Retail*, which doesn't exist. Use *Retail Customers* instead, create *Retail*, or leave these rows out?" The user picks the first:

```json
{
  "session": "IMP-2026-00012",
  "fixes": [{"group": "G3fa2c1", "action": "map", "to": "Retail Customers"}]
}
```

The reply is a new check, plus **`applied`**: one sentence per fix, e.g. "Customer Group 'Retail' becomes 'Retail Customers' in 412 rows." Confirm them to the user in one line, then give the new count.

- **`left_out`** lists the rows left out, grouped by reason, e.g. `{"reason": "Closed shop", "count": 2, "rows": "3-4"}`.

### When nothing fails any more

`status` is `Ready to Import`, `groups` is empty, and `message` says so with the final count, e.g. "The file is ready: 4,812 rows will be imported. 6 rows left out; the import report will list them." Tell the user exactly that.

### When no fix is saved

The fixes are all or nothing. If any is wrong, none is saved and `problems` says which group and why:

- **`to` doesn't exist** or isn't an allowed option: the error lists the closest names or the choices.
- **`create` sent in `fixes`**: refused. Use `create_document` instead.
- **A group id that isn't in the latest check**: its rows already pass. Use the ids from the latest result.
- **`fixes` with a new `file_url`**: refused. Check the new file first, then fix its groups.
- **No check yet**, or the mapping changed since: call without `fixes` first.

To change a mapping, map the same value again: the new choice replaces the earlier one.
