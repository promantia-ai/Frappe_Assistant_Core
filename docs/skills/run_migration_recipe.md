# How to Use run_migration_recipe

Run a saved migration recipe on new files: a fresher export from the same system, or the same migration
on another site (rehearsal, then production).

## Steps

1. Open each new file: `start_import_session` for a plain file, `apply_import_preset` for an export of a
   system with a preset (Zoho, Tally, Busy, Marg, QuickBooks). Keep every session id.
2. `run_migration_recipe(recipe, sessions)`.
   - Each file is matched to a step by its sheet name and its columns (at least 80% of the step's columns
     must be in the file). A preset file is matched by its preset and which of the preset's files it is.
   - A matched file is mapped as the recipe did it: the same columns, value maps, old ID column and old ID
     translations, even if the columns came in another order.
   - The matched files become a migration plan in the recipe's order with its failure rule. Nothing is
     imported.
3. Tell the user which files matched which steps, and list `unmatched_sessions` and `unmatched_steps`.
4. Steps with `opening` (opening stock, invoices or balances): set those up with `set_opening_stock`,
   `set_opening_invoices` or `set_opening_balances`, passing the saved settings in `opening.settings`.
   The cut-off date is asked again: the recipe keeps no date. Then add the file to the plan with
   `plan_migration(plan, sessions)`.
5. `plan_questions(plan)`. A recipe run asks only what is new, as questions of kind `recipe_new`:
   - `new_columns`: the file has columns the recipe never saw. They are not imported. Map one with
     `set_column_mapping` if the user wants it, then answer `{id, ok: true}`.
   - `gone_columns`: columns the recipe had are missing. Those fields stay empty. Answer `{id, ok: true}`.
   - `not_on_site`: a DocType or field of the recipe isn't on this site (a custom field of the other
     site). Nothing is guessed; the column is left unmapped or the file isn't imported. Tell the user,
     then answer `{id, ok: true}`.
   Everything the user already decided in the first run is not asked again. Values that no saved value
   map covers, and references that don't resolve, come as the usual fix questions.
6. When the plan is ready: `run_migration_plan`, as for any plan.

## Rules

- Never guess a match. If a file matches no step, say so; don't map it by hand without telling the user.
- Run the same recipe twice on the same files and the second run finds the records already there; say so.
