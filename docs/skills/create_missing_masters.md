# Create Missing Masters

## Overview

The `create_missing_masters` tool creates the missing masters an import session's last dry run found (Customer Groups, Territories, UOMs, Brands…) **all in one call**, so the user approves **one plan** instead of one approval card per record. It follows the **setup-change-protocol**: plan table, one confirmation, change log.

Every record is created or none is: if one can't be, nothing is created and the reply says which and why. Each record created is linked to the import session (its **Created Records** table), so the session can undo it later.

## Where the plan comes from

Every `dry_run_import` reply has **`masters_plan`**: one entry per missing master that can be created, with the rows it fixes.

```json
"masters_plan": [
  {"group": "G3fa2c1", "doctype": "Customer Group", "name": "Wholesale", "parent": "All Customer Groups", "rows": 18},
  {"group": "G8b01d4", "doctype": "Territory", "name": "North", "parent": "All Territories", "rows": 7},
  {"group": "G51e0aa", "doctype": "UOM", "name": "Dozen", "parent": null, "rows": 12}
]
```

Only **simple masters** are planned: ones that need nothing but their name (and a parent, in a tree) and keep the name they are given. A missing record that can't be created has no `create` fix; its group has **`cannot_create`** saying why, for example:

- *Customer, Supplier, Warehouse, Account*: ERPNext names it itself (a number, or the company's abbreviation added), so it wouldn't get the name the file uses.
- *Item, Price List, Tax Withholding Category*: it needs more than a name (another field, or a row in a table).
- *Supplier Scorecard*: its name must be an existing record (a Supplier).
- *The DocType the file itself imports*, such as a parent Territory in a Territory file: it is one of the file's rows, or should be added to the file.
- *Currency, Country, User*: a system record.
- *Company*: creating one also creates a chart of accounts.
- The user isn't allowed to create that DocType, or creating records with FACO (`create_document`) is turned off for them.

For those, offer **map** to an existing record, or **leave out** the rows.

## Workflow

1. **Show the plan as one table**, with every record:

   | # | DocType | Name | Parent | Rows it fixes |
   |---|---|---|---|---|
   | 1 | Customer Group | Wholesale | All Customer Groups | 18 |
   | 2 | Territory | North | All Territories | 7 |
   | 3 | UOM | Dozen | — | 12 |

2. **Let the user change a parent.** A parent must be an existing group of the same tree (for example a Customer Group with "Is Group" ticked). A `null` parent means the record goes at the top of the tree. Ask once: "Create these 3 records?"
3. **Call the tool once** with the approved entries. The approval card shows the count; the table above is what the user is approving.
4. **Show the change log**: the same table with a Result column. Then call `dry_run_import` again **without fixes**; the rows that needed these records now pass.

## Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `session` | string | **Yes** | The import session id |
| `records` | array | **Yes** | The approved `masters_plan` entries: `{group, doctype, name, parent?}` |

`group`, `doctype` and `name` must be copied from the plan: the tool checks that the group is the missing `doctype` `name`. **Never invent a name.** To use another name, map the rows instead. `parent` is only for trees; leave it out to use the planned one.

## Response

```json
{
  "success": true,
  "session": "IMP-2026-00012",
  "created": [
    {"group": "G3fa2c1", "doctype": "Customer Group", "name": "Wholesale", "parent": "All Customer Groups", "rows": 18}
  ],
  "message": "Created 1 record, linked to the import session. Check the file again with dry_run_import."
}
```

## When nothing is created

- **A wrong entry** (a group not in the last check, a name that isn't the group's, a record that can't be created here, one that already exists, a parent that isn't a group): `problems` lists each, with the reason. Correct them and call again.
- **A record ERPNext refuses** while creating (its own rules, beyond a name): the error names it and says why; the ones before it are undone. Tell the user, take that record out of the plan (map or leave out its rows instead), and ask again.
- **The file is still being checked** in the background: wait for the check to finish.
- **No check, or the file, mapping or fixes changed since it**: call `dry_run_import` first. The plan always comes from the latest check.

## Anti-patterns

- Calling `create_document` once per record: one approval card each.
- Creating a record that isn't in `masters_plan`.
- Creating without showing the plan table first.
