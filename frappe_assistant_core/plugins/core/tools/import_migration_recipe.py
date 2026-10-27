# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Import Migration Recipe Tool for Core Plugin (#34420)."""

from typing import Any, Dict

import frappe
from frappe import _

from frappe_assistant_core.core.base_tool import BaseTool, exception_message, permission_error_result
from frappe_assistant_core.utils import migration_recipe
from frappe_assistant_core.utils.import_session import FileProblem, find_site_file


class ImportMigrationRecipe(BaseTool):
    """Make a recipe from a JSON file another site exported."""

    def __init__(self):
        super().__init__()
        self.name = "import_migration_recipe"
        self.description = (
            "Import a migration recipe from the JSON file export_migration_recipe wrote on another site "
            "(uploaded in File Manager or the chat: pass its file_id or file_url). The recipe is made "
            "here with where it came from and a note of the import; nothing is run. A file in another "
            "format is refused with the reason. Next: open the new files and run_migration_recipe."
        )
        self.requires_permission = migration_recipe.RECIPE
        # the file comes from another site: what it says reaches the model fenced (#34003)
        self.untrusted_fields = ("title", "steps", "origin", "error")
        self.inputSchema = {
            "type": "object",
            "properties": {
                "file_id": {"type": "string", "description": "The File's id in File Manager."},
                "file_url": {"type": "string", "description": "The file's URL."},
            },
        }

    def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        file_id, file_url = (
            (arguments.get("file_id") or "").strip(),
            (arguments.get("file_url") or "").strip(),
        )
        if not file_id and not file_url:
            return {"success": False, "error": _("Give file_id or file_url of the recipe file.")}
        try:
            doc = migration_recipe.import_recipe(find_site_file(file_id=file_id, file_url=file_url))
        except (FileProblem, migration_recipe.RecipeProblem) as e:
            return {"success": False, "error": str(e)}
        except frappe.PermissionError as e:
            return permission_error_result(migration_recipe.RECIPE, exception_message(e))
        except Exception as e:
            frappe.log_error(title=_("Import Migration Recipe Error"), message=frappe.get_traceback())
            return {"success": False, "error": exception_message(e)}
        return {"success": True, **migration_recipe.describe(doc), "imported_note": doc.imported_note}


# Make sure class name matches file name for discovery
import_migration_recipe = ImportMigrationRecipe
