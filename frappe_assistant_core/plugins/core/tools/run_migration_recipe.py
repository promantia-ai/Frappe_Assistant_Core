# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Run Migration Recipe Tool for Core Plugin (#34420)."""

from typing import Any, Dict

import frappe
from frappe import _

from frappe_assistant_core.core.base_tool import BaseTool, exception_message, permission_error_result
from frappe_assistant_core.utils import migration_recipe
from frappe_assistant_core.utils.import_mapping import SESSION_DOCTYPE


class RunMigrationRecipe(BaseTool):
    """Apply a saved recipe to new files and make the migration plan, ready for its questions."""

    def __init__(self):
        super().__init__()
        self.name = "run_migration_recipe"
        self.description = (
            "Run a saved migration recipe on new files (a fresher export, or the same migration on "
            "another site). Open each new file with start_import_session first (a file from a source "
            "system with a preset: apply_import_preset), then pass the recipe and those sessions. Each "
            "file is matched to a step (sheet name, and at least 80% of the step's columns present), "
            "mapped as the recipe did it, and the matched files become a migration plan in the recipe's "
            "order with its failure rule. Nothing is imported. The result lists unmatched files and "
            "steps, and `opening` for opening stock, invoices or balances steps: set those up with "
            "set_opening_stock, set_opening_invoices or set_opening_balances (the cut-off date is asked "
            "again). Next: plan_questions. It asks only what is new (kind recipe_new): answer each with "
            "{id, ok: true} after telling the user, or map the new column first with set_column_mapping."
        )
        self.requires_permission = None  # the recipe and each session are checked
        self.untrusted_fields = ("applied", "unmatched_steps", "problems", "error")
        self.inputSchema = {
            "type": "object",
            "properties": {
                "recipe": {"type": "string", "description": "The FAC Migration Recipe id."},
                "sessions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "The import sessions of the new files.",
                },
                "since": {
                    "type": "string",
                    "description": "The cut-off date, YYYY-MM-DD, for a delta run: the transactions made "
                    "after it, uploaded after the opening migration. Ask the user; never guess it. Only "
                    "the recipe's transaction steps run, rows dated on or before it are left out, "
                    "everything is submitted, and no opening entry is posted.",
                },
                "masters": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "A delta run only: the positions of master steps (customers, items) "
                    "the user ticked to bring in when new. Others are skipped.",
                },
            },
            "required": ["recipe", "sessions"],
        }

    def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        recipe, sessions = arguments.get("recipe"), arguments.get("sessions")
        if not recipe or not isinstance(sessions, list) or not all(isinstance(s, str) for s in sessions):
            return {"success": False, "error": _("Pass the recipe and a list of import session ids.")}
        try:
            try:
                return {
                    "success": True,
                    **migration_recipe.run(
                        recipe, sessions, arguments.get("since"), arguments.get("masters")
                    ),
                }
            except frappe.DoesNotExistError as e:
                return {"success": False, "error": exception_message(e)}
            except frappe.PermissionError as e:
                return permission_error_result(
                    migration_recipe.RECIPE if "Recipe" in str(e) else SESSION_DOCTYPE, exception_message(e)
                )
            except migration_recipe.RecipeProblem as e:
                return {"success": False, "error": str(e)}
        except Exception as e:
            frappe.log_error(title=_("Run Migration Recipe Error"), message=frappe.get_traceback())
            return {"success": False, "error": exception_message(e)}


# Make sure class name matches file name for discovery
run_migration_recipe = RunMigrationRecipe
