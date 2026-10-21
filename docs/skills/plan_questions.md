# How to Use plan_questions

Every open question of a **migration plan** in one list, so the user answers everything before
the one approval (`run_migration_plan`), and nothing asks again once the plan runs.

## When to use

After `plan_migration`, and after mapping the files you were sure of. Call it, show the
questions, collect the answers, call it again with them. Repeat until `ready` is true.

## Inputs

| Input | Type | Required | Description |
|---|---|---|---|
| `plan` | string | Yes | The plan id. |
| `answers` | list of objects | No | The user's answers. Each has the `id` of its question. All apply, or none. |

## Questions

Each question has `id`, `kind`, and for a file `session` and `file_name`.

| `kind` | What it is | How to answer |
|---|---|---|
| `structure` | A file not mapped yet (`columns` lists its headers). | Map it yourself with `get_import_schema` and `set_column_mapping`, asking the user only where you are unsure; or set it up with `set_opening_stock`, `set_opening_invoices` or `set_opening_balances`. Not through `answers`. |
| `order` | Two files link to each other. | Tell the user: one is imported with the linking columns left out. |
| `fix` | Rows that would fail, with `explanation` and `fixes` (the engine's suggestion first). | `{id, action, ...}` as `dry_run_import` takes a fix: `map` with `to`, `leave_out` with an optional `reason`, `create` (with an optional `parent`) to have the plan create the missing record when it runs. |
| `failure_rule` | What happens to rows that still fail when their file comes up. | `{id: "plan:failure_rule", rule: "leave_out"}` or `"stop"`. Suggest `leave_out`. |
| `checking` / `blocked` | A file still being checked, or that can't be checked yet (`message` says why). | Ask again in a moment, or do what `message` says. |

Show the questions together, grouped by file, with the suggested answer first, so the user can
accept most of them at once.

## Result

| Field | Description |
|---|---|
| `files` | The plan's files in order, with their status. |
| `questions` | What is still open. |
| `ready` | True when nothing is open and every file is Ready to Import: the plan is `Ready to Run`. Next: `run_migration_plan`. |
| `applied` | What the answers did. |
