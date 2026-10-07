# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""
Set Column Mapping Tool for Core Plugin.
Saves which file column goes to which field on an import session.
"""

from typing import Any, Dict

import frappe
from frappe import _

from frappe_assistant_core.core.base_tool import BaseTool, exception_message, permission_error_result

from . import import_mapping
from .import_mapping import (
    SESSION_DOCTYPE,
    SessionsUnavailable,
    build_import_schema,
    check_import_target,
    describe_mapping,
    duplicate_targets,
    resolve_mapping,
)


class SetColumnMapping(BaseTool):
    """
    Tool for saving a migration file's column mapping on its import session.

    Every field is checked against the DocType's import schema first, and nothing is saved
    unless all of them pass, so the model can correct a mistake and call again.
    """

    def __init__(self):
        super().__init__()
        self.name = "set_column_mapping"
        self.description = (
            "Save which column of the user's migration file goes to which ERPNext field, on its "
            "import session. Pass only the columns to set or change; columns already mapped keep "
            "their field. Each field is checked against get_import_schema, and nothing is saved if "
            "any is wrong: the result says what to fix. Pass doctype the first time, or to change "
            "the target (that clears the old mapping). The result is the full mapping to show the "
            "user: parent fields and each child table apart, with unmapped columns and required "
            "fields that have no column."
        )
        self.requires_permission = None  # Checked on the session and the target DocType

        self.inputSchema = {
            "type": "object",
            "properties": {
                "session": {"type": "string", "description": "The import session id"},
                "mapping": {
                    "type": "object",
                    "description": (
                        "Column → field. A column is its header exactly as in the file, or its "
                        "0-based position. A field is a fieldname, '<table fieldname>.<fieldname>' "
                        f'for a child table, or "{import_mapping.DONT_IMPORT}" to skip the column.'
                    ),
                },
                "doctype": {
                    "type": "string",
                    "description": "The target DocType. Needed the first time; changing it clears the mapping.",
                },
            },
            "required": ["session", "mapping"],
        }

    def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        session_name = arguments.get("session")
        mapping = arguments.get("mapping")
        try:
            if not isinstance(mapping, dict) or not mapping:
                return {"success": False, "error": "mapping must be an object of column → field."}

            try:
                doc = import_mapping.load_session(session_name)
            except SessionsUnavailable:
                return {
                    "success": False,
                    "error": "Import sessions aren't available on this site yet (FAC Import Session is missing).",
                }
            except frappe.DoesNotExistError:
                return {"success": False, "error": f"Import session '{session_name}' not found."}
            except frappe.PermissionError as e:
                return permission_error_result(SESSION_DOCTYPE, exception_message(e), name=session_name)

            current_target = import_mapping.session_target(doc)
            target = arguments.get("doctype") or current_target
            if not target:
                return {
                    "success": False,
                    "error": "This session has no target DocType yet. Pass doctype, e.g. 'Customer'.",
                }

            # Before any work: the user must be able to import into the target.
            error = check_import_target(target)
            if error:
                return error

            headers = import_mapping.session_headers(doc)
            if not headers:
                return {"success": False, "error": "Couldn't read the column headers of the session's file."}

            schema = build_import_schema(target)
            existing = import_mapping.session_mapping(doc) if target == current_target else {}
            resolved, problems = resolve_mapping(target, schema, headers, mapping)
            merged = {**existing, **resolved}
            problems += duplicate_targets(merged, headers)
            if problems:
                return {
                    "success": False,
                    "error": "The mapping was not saved. Fix these and call again.",
                    "problems": problems,
                    "columns": headers,
                }

            import_mapping.save_session(doc, target, merged)
            return {
                "success": True,
                "session": session_name,
                "doctype": target,
                **describe_mapping(schema, headers, merged),
            }

        except Exception as e:
            frappe.log_error(
                title=_("Set Column Mapping Error"), message=f"Error saving column mapping: {e!s}"
            )
            return {"success": False, "error": str(e)}


# Make sure class name matches file name for discovery
set_column_mapping = SetColumnMapping
