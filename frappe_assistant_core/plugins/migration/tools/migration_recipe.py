# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Migration recipe tool for the Core plugin (#34420): save a finished migration as a recipe, run it on new
files, and move it between sites as a file. One tool with an action, so the model sees one entry for
recipes instead of four.
"""

from typing import Any, Dict

import frappe
from frappe import _

from frappe_assistant_core.core.base_tool import BaseTool, exception_message, permission_error_result
from frappe_assistant_core.utils import migration_recipe
from frappe_assistant_core.utils.import_mapping import SESSION_DOCTYPE
from frappe_assistant_core.utils.import_session import FileProblem, find_site_file

RECIPE = migration_recipe.RECIPE
ACTIONS = ("save", "run", "export", "import")


class MigrationRecipe(BaseTool):
    """Save, run, export or import a migration recipe."""

    def __init__(self):
        super().__init__()
        self.name = "migration_recipe"
        self.description = (
            "A migration recipe is how a migration was mapped (columns, value maps, presets, old ID "
            "columns, opening settings), without any row of the files and without dates, to run again. "
            "action save: keep a migration that ended as a recipe; pass plan (a plan that Completed or "
            "Partially Completed) or session (one Completed import session); show the user the step list "
            "it returns. action run: run a recipe on new files (a fresher export, the same migration on "
            "another site, or with since the transactions after the cut-off); open each new file with "
            "start_import_session first, then pass recipe and sessions. Each file is matched to a step and "
            "mapped as the recipe did it, and the matched files become a migration plan; nothing is "
            "imported. Set up the `opening` steps it lists with set_opening_stock, set_opening_invoices "
            "or set_opening_balances (ask the cut-off date again). Next: dry_run_import with the plan, "
            "which asks only what is new (kind recipe_new): answer each with {id, ok: true} after "
            "telling the user, or map the new column first with set_column_mapping. action export: write "
            "a recipe as a private JSON file for another site; give the user the link. action import: "
            "make a recipe from such a file (file_id or file_url); nothing is run."
        )
        self.requires_permission = RECIPE
        # a run quotes the client's files; an imported recipe comes from another site (#34003), its
        # origin and note included
        self.untrusted_fields = (
            "applied",
            "unmatched_steps",
            "skipped_steps",
            "problems",
            "title",
            "steps",
            "origin",
            "source_site",
            "source_plan",
            "created_by",
            "created_on",
            "imported_note",
            "error",
        )
        self.inputSchema = {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(ACTIONS)},
                "plan": {
                    "type": "string",
                    "description": "save: the migration plan (its first session's id).",
                },
                "session": {"type": "string", "description": "save: one import session, instead of a plan."},
                "title": {"type": "string", "description": "save: a name. Defaults to the plan's title."},
                "recipe": {"type": "string", "description": "run, export: the FAC Migration Recipe id."},
                "sessions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "run: the import sessions of the new files.",
                },
                "since": {
                    "type": "string",
                    "description": "run: the cut-off date, YYYY-MM-DD, for a delta run: the transactions "
                    "made after it, uploaded after the opening migration. Ask the user; never guess it. "
                    "Only the recipe's transaction steps run, rows dated on or before it are left out, "
                    "everything is submitted, and no opening entry is posted.",
                },
                "masters": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "run, a delta run only: the positions of master steps (customers, "
                    "items) the user ticked to bring in when new. Others are skipped.",
                },
                "file_id": {"type": "string", "description": "import: the recipe file's id in File Manager."},
                "file_url": {"type": "string", "description": "import: the recipe file's URL."},
            },
            "required": ["action"],
        }

    def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        action = arguments.get("action")
        if action not in ACTIONS:
            return {"success": False, "error": _("action is one of {0}.").format(", ".join(ACTIONS))}
        try:
            return getattr(self, f"_{action}")(arguments)
        except frappe.DoesNotExistError as e:
            return {"success": False, "error": exception_message(e)}
        except frappe.PermissionError as e:
            doctype = SESSION_DOCTYPE if action == "run" and "Recipe" not in str(e) else RECIPE
            return permission_error_result(doctype, exception_message(e))
        except (migration_recipe.RecipeProblem, FileProblem) as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            frappe.log_error(title=_("Migration Recipe Error"), message=frappe.get_traceback())
            return {"success": False, "error": exception_message(e)}

    def _save(self, arguments):
        plan, session = arguments.get("plan"), arguments.get("session")
        if bool(plan) == bool(session):
            return {"success": False, "error": _("Pass either plan or session.")}
        try:
            doc = (
                migration_recipe.from_plan(plan, arguments.get("title"))
                if plan
                else migration_recipe.from_session(session, arguments.get("title"))
            )
        except frappe.DoesNotExistError:
            return {"success": False, "error": _("{0} not found.").format(plan or session)}
        return {"success": True, **migration_recipe.describe(doc)}

    def _run(self, arguments):
        recipe, sessions = arguments.get("recipe"), arguments.get("sessions")
        if not recipe or not isinstance(sessions, list) or not all(isinstance(s, str) for s in sessions):
            return {"success": False, "error": _("Pass the recipe and a list of import session ids.")}
        return {
            "success": True,
            **migration_recipe.run(recipe, sessions, arguments.get("since"), arguments.get("masters")),
        }

    def _export(self, arguments):
        recipe = arguments.get("recipe")
        if not recipe:
            return {"success": False, "error": _("Pass the recipe.")}
        try:
            file_doc = migration_recipe.export_recipe(recipe)
        except frappe.DoesNotExistError:
            return {"success": False, "error": _("Recipe '{0}' not found.").format(recipe)}
        return {
            "success": True,
            "recipe": recipe,
            "file_id": file_doc.name,
            "file_name": file_doc.file_name,
            "file_url": file_doc.file_url,
            "message": _("The recipe is in {0}. It holds no data of the files.").format(file_doc.file_name),
        }

    def _import(self, arguments):
        file_id = (arguments.get("file_id") or "").strip()
        file_url = (arguments.get("file_url") or "").strip()
        if not file_id and not file_url:
            return {"success": False, "error": _("Give file_id or file_url of the recipe file.")}
        doc = migration_recipe.import_recipe(find_site_file(file_id=file_id, file_url=file_url))
        return {"success": True, **migration_recipe.describe(doc), "imported_note": doc.imported_note}
