# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Export Migration Recipe Tool for Core Plugin (#34420)."""

from typing import Any, Dict

import frappe
from frappe import _

from frappe_assistant_core.core.base_tool import BaseTool, exception_message, permission_error_result
from frappe_assistant_core.utils import migration_recipe


class ExportMigrationRecipe(BaseTool):
    """Write a saved recipe to a JSON file, to run on another site."""

    def __init__(self):
        super().__init__()
        self.name = "export_migration_recipe"
        self.description = (
            "Export a migration recipe as a private JSON file, to import on another site (a rehearsal "
            "site's recipe for production). The file holds how each file was mapped, never a row of "
            "the files and no date. Give the user the file link it returns."
        )
        self.requires_permission = migration_recipe.RECIPE
        self.inputSchema = {
            "type": "object",
            "properties": {"recipe": {"type": "string", "description": "The FAC Migration Recipe id."}},
            "required": ["recipe"],
        }

    def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        recipe = arguments.get("recipe")
        if not recipe:
            return {"success": False, "error": _("Pass the recipe.")}
        try:
            file_doc = migration_recipe.export_recipe(recipe)
        except frappe.DoesNotExistError:
            return {"success": False, "error": _("Recipe '{0}' not found.").format(recipe)}
        except frappe.PermissionError as e:
            return permission_error_result(migration_recipe.RECIPE, exception_message(e), name=recipe)
        except Exception as e:
            frappe.log_error(title=_("Export Migration Recipe Error"), message=frappe.get_traceback())
            return {"success": False, "error": exception_message(e)}
        return {
            "success": True,
            "recipe": recipe,
            "file_id": file_doc.name,
            "file_name": file_doc.file_name,
            "file_url": file_doc.file_url,
            "message": _("The recipe is in {0}. It holds no data of the files.").format(file_doc.file_name),
        }


# Make sure class name matches file name for discovery
export_migration_recipe = ExportMigrationRecipe
