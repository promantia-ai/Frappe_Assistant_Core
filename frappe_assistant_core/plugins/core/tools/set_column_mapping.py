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
from frappe_assistant_core.plugins.core.import_mapping import (
    DONT_IMPORT,
    SESSION_DOCTYPE,
    build_import_schema,
    describe_mapping,
    duplicate_targets,
    load_session,
    resolve_mapping,
    save_mapping,
    session_headers,
    session_mapping,
)
from frappe_assistant_core.plugins.core.import_session import import_target_problem


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
            "fields that have no column. Next, once the user agrees the mapping: dry_run_import to "
            "check every row."
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
                        f'for a child table, or "{DONT_IMPORT}" to skip the column.'
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
                doc = load_session(session_name)
            except frappe.DoesNotExistError:
                return {"success": False, "error": f"Import session '{session_name}' not found."}
            except frappe.PermissionError as e:
                return permission_error_result(SESSION_DOCTYPE, exception_message(e), name=session_name)

            current_target = doc.target_doctype or None
            target = arguments.get("doctype") or current_target
            if not target:
                return {
                    "success": False,
                    "error": "This session has no target DocType yet. Pass doctype, e.g. 'Customer'.",
                }

            # Before any work: the user must be able to import into the target.
            problem = import_target_problem(target)
            if problem:
                return {"success": False, "error": problem, "doctype": target}

            headers = session_headers(doc)
            if not headers:
                return {"success": False, "error": "The session has no column headers for its sheet."}

            schema = build_import_schema(target)
            existing = session_mapping(doc) if target == current_target else {}
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

            described = describe_mapping(schema, headers, merged)
            save_mapping(doc, target, merged, described["summary"])
            return {
                "success": True,
                "session": session_name,
                "doctype": target,
                "status": doc.status,
                **described,
            }

        except Exception as e:
            frappe.log_error(
                title=_("Set Column Mapping Error"), message=f"Error saving column mapping: {e!s}"
            )
            return {"success": False, "error": str(e)}


# Make sure class name matches file name for discovery
set_column_mapping = SetColumnMapping
