# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Save Migration Recipe Tool for Core Plugin (#34420)."""

from typing import Any, Dict

import frappe
from frappe import _

from frappe_assistant_core.core.base_tool import BaseTool, exception_message, permission_error_result
from frappe_assistant_core.utils import migration_recipe


class SaveMigrationRecipe(BaseTool):
    """Keep a finished migration as a recipe that can be run again on new files."""

    def __init__(self):
        super().__init__()
        self.name = "save_migration_recipe"
        self.description = (
            "Save a migration that ended as a recipe: how each file was mapped (columns, value maps, "
            "presets, old ID columns, opening settings), without any row of the files and without dates. "
            "Pass `plan` (a migration plan that Completed or Partially Completed) or `session` (one import "
            "session that Completed). The recipe can then be run on a fresher export, on another site, or "
            "on the transactions made after the cut-off. Show the user the step list it returns."
        )
        self.requires_permission = RECIPE_DOCTYPE
        self.inputSchema = {
            "type": "object",
            "properties": {
                "plan": {
                    "type": "string",
                    "description": "The migration plan to save (its first session's id).",
                },
                "session": {
                    "type": "string",
                    "description": "One import session to save, instead of a plan.",
                },
                "title": {
                    "type": "string",
                    "description": "A name for the recipe. Defaults to the plan's title.",
                },
            },
        }

    def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        plan, session = arguments.get("plan"), arguments.get("session")
        if bool(plan) == bool(session):
            return {"success": False, "error": _("Pass either plan or session.")}
        try:
            try:
                doc = (
                    migration_recipe.from_plan(plan, arguments.get("title"))
                    if plan
                    else migration_recipe.from_session(session, arguments.get("title"))
                )
            except frappe.DoesNotExistError:
                return {"success": False, "error": _("{0} not found.").format(plan or session)}
            except frappe.PermissionError as e:
                return permission_error_result(RECIPE_DOCTYPE, exception_message(e), name=plan or session)
            except migration_recipe.RecipeProblem as e:
                return {"success": False, "error": str(e)}
            return {"success": True, **migration_recipe.describe(doc)}
        except Exception as e:
            frappe.log_error(title=_("Save Migration Recipe Error"), message=f"{e!s}")
            return {"success": False, "error": str(e)}


RECIPE_DOCTYPE = migration_recipe.RECIPE

# Make sure class name matches file name for discovery
save_migration_recipe = SaveMigrationRecipe
