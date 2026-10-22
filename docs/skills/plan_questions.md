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
| `fix` | Rows that would fail, with `explanation`, `fixes` (the engine's suggestion first) and, when a later file needs their records, `needed_by`. | `{id, action, ...}` as `dry_run_import` takes a fix: `map` with `to`, `leave_out` with an optional `reason`, `create` (with an optional `parent`) to have the plan create the missing record when it runs. |
| `cut_off` | The plan has opening stock, invoices or balances: the cut-off date, asked of the user. `dates` shows the date each opening file is set up with; `choices` are those dates and `another_date`; `file_names_say` lists a file whose name gives another date (Closing Stock 30-Sep): show it, the user checks which is right. | Show it on the one card ("cut-off <date>"), so the user approves the date they see; never take it from a file name or today's date without showing it. If they give another date, or a file's date differs from theirs, set it again first (`set_opening_stock`, `set_opening_invoices`, `set_opening_balances`). Then `{id: "plan:cut_off", date: "YYYY-MM-DD"}`: refused, with nothing applied, unless every opening file has that date. |
| `failure_rule` | What happens to rows that still fail when their file comes up. | `{id: "plan:failure_rule", rule: "leave_out"}` or `"stop"`. Suggest `leave_out`. |
| `checking` / `blocked` | A file still being checked, or that can't be checked yet (`message` says why). | Ask again in a moment, or do what `message` says. |

Ask once, with one card. The user is touched twice in a whole migration: the cut-off date at the
start (`plan_migration`'s `opening_setup`; the company only when the site has more than one), then
this card. `next` in the result says which of the steps below comes now.

1. Show **every** question in **one table, in your message** (not inside the card: a card shows
   no tables): file, the problem in plain words, and the answer you suggest (the first of its
   `fixes`). Every one: typos, missing records, duplicates, rows left out, values with no match.
   Put the cut-off date in it, and the failure rule (`leave_out`). A question with `needed_by`
   says so in its row: "leaving it out also leaves out <needed_by>"; prefer a fix that keeps it.
2. Then **one single-choice card** (`ask_user`, `single_select`, never `multi_select`), its
   question one short line ("Apply these answers?"), with two
   options: **"Apply all suggested answers and start (cut-off <date>)"** first, and **"Let me
   change some"**. Never one option per answer: a card of checkboxes lets the user pick two
   answers to one question (map it and create it).
3. On "Apply all…": answer every question as suggested, and once `ready` is true, call
   `run_migration_plan` with the `approval` straight away: the user already said start. No
   "Start the migration?" card after it.
4. On "Let me change some": ask which, in their words or a card each, then apply all the answers
   together and show the card again.
5. A question with nothing to suggest (a value with no close match) gets its own single-choice
   card in the same turn, with its options and "leave the row out".

Don't write the questions as text for the user to type answers to, and don't ask about column
mapping: map the columns yourself; a column you can't place is a row of the table.

A row of opening stock or invoices whose item, customer or supplier an earlier file of the plan
brings isn't a question: it is ready, and checked in full when its file comes up. Don't ask to
create those records or to leave those rows out.

## Result

| Field | Description |
|---|---|
| `files` | The plan's files in order, with their status. |
| `questions` | What is still open. |
| `ready` | True when nothing is open and every file is Ready to Import: the plan is `Ready to Run`. Next: `run_migration_plan`. |
| `approval` | Once ready: what `run_migration_plan` is given, exactly (files, rows, masters, `cut_off`, `decisions`). Its approval card shows the user every decision. |
| `applied` | What the answers did. |
| `next` | What to do now: the table and the one card, map the files first, or run straight away. |
