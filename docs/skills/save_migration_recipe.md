# How to Use save_migration_recipe

Keep a migration that ended as a recipe: how each of its files was mapped, so the same migration can be
run again on a fresher export, on another site (a rehearsal, then production), or on the transactions made
after the cut-off.

## When

The consultant has finished a migration and says "save this", "we will do this again on production", or
"keep this so I can repeat it". Only a migration plan that Completed or Partially Completed, or one import
session that Completed, can be saved.

## Call

- `save_migration_recipe(plan=...)` for a migration plan (the id of its first session).
- `save_migration_recipe(session=...)` for one file.
- `title` is optional; it defaults to the plan's title.

## What the recipe keeps, and what it never keeps

Keeps, per file: the sheet and the column headers it was matched by, the DocType, how each column was
mapped, the value maps the user agreed, the preset, the old ID column and its source system, and the
opening import's settings (without dates). Also the plan's failure rule, and where it came from.

Never keeps: any row of the files, the cut-off or other dates, which rows were left out, file ids, or
how far the run got. Say this to the user if they ask whether their data is in the recipe.

## Show the user

The result lists the steps (position, file, sheet, DocType, preset, opening engine, and whether the step
is a transaction step). Show it as a short table and say which recipe was made.

## Next

Running the recipe on new files comes with the recipe tools for it; until then the recipe is kept for
the site.
