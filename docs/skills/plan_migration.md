# How to Use plan_migration

Group the import sessions of one migration into a **migration plan**, ordered so every file comes
after the files whose records it links to. The plan is how a migration of several files is checked
together and imported after **one approval** (`run_migration_plan`), with no questions after it.

## When to use

- The user migrates several files (customers, items, prices, opening balances).
- Or the user wants to answer everything first and approve once.

For one file the user is happy to take step by step, the plain path (`start_import`) is fine.

## Inputs

| Input | Type | Required | Description |
|---|---|---|---|
| `sessions` | list of strings | Yes | The import session ids to add. Open every file's session first (`apply_import_preset`, else `start_import_session`). |
| `plan` | string | No | An existing plan to add them to: it orders all its files again. Left out, a new plan is made. |
| `failure_rule` | `leave_out` or `stop` | No | What happens to rows that fail when their file comes up. `leave_out` (default) leaves them out with their reason; `stop` stops the plan before that file. `plan_questions` asks it if the user hasn't said. |

## Result

| Field | Description |
|---|---|
| `plan` | The plan id, `MIG-YYYY-NNNNN`. |
| `status` | `Planning` until every question is answered and every file is ready, then `Ready to Run`. |
| `files` | The files in import order: `session`, `file_name`, `doctype`, `position`, `reason` (which earlier files it needs) and the session's `status`. Show this to the user. |
| `problems` | Files that link to each other, so neither can go first. Import one with the linking columns left out, then the other. |

## Refusals

- A session already in another plan, or one already importing, imported, undone or cancelled: nothing changes, `problems` says which.
- A plan that is running or finished: its files can't change.

## Next

Map every file (`get_import_schema`, `set_column_mapping`), asking the user only where you are
unsure, then call `plan_questions` with the plan.
