# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Create Missing Masters Tool for Core Plugin (#33992).

Creates the missing masters an import session's last dry run found, all in one call, so the
user approves one plan instead of one card per record. Each record is created through
create_document and linked to the session.
"""

from typing import Any, Dict

import frappe
from frappe import _

from frappe_assistant_core.core.base_tool import BaseTool, exception_message, permission_error_result
from frappe_assistant_core.plugins.core import import_dry_run as dry_run
from frappe_assistant_core.plugins.core import import_fixes
from frappe_assistant_core.plugins.core.import_mapping import SESSION_DOCTYPE, load_session


class CreateMissingMasters(BaseTool):
    """Create the missing masters of an import session in one approved batch."""

    def __init__(self):
        super().__init__()
        self.name = "create_missing_masters"
        self.description = (
            "Create the missing masters (Customer Groups, Territories, UOMs…) that an import "
            "session's last dry_run_import found, all in one call with one approval. First show "
            "the user dry_run_import's masters_plan as one table (DocType, name, parent, rows it "
            "fixes) and let them change a parent; then pass the records they approved. Only those "
            "in masters_plan can be created; names come from the plan, never invent one. All are "
            "created or none: if one can't be, nothing is created and the reply says which and "
            "why. Each record is linked to the session. Then show the change log and call "
            "dry_run_import again without fixes."
        )
        self.requires_permission = None  # checked on the session and on each DocType

        self.inputSchema = {
            "type": "object",
            "properties": {
                "session": {"type": "string", "description": "The import session id"},
                "records": {
                    "type": "array",
                    "description": "The approved masters_plan entries, one per record.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "group": {"type": "string", "description": "The plan entry's group id"},
                            "doctype": {"type": "string", "description": "As in the plan"},
                            "name": {"type": "string", "description": "As in the plan"},
                            "parent": {
                                "type": "string",
                                "description": "For a tree (Customer Group, Territory…): the "
                                "group it goes under, as planned or as the user changed it.",
                            },
                        },
                        "required": ["group", "doctype", "name"],
                    },
                },
            },
            "required": ["session", "records"],
        }

    def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        session_name = arguments.get("session")
        records = arguments.get("records")
        if not records or not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
            return {"success": False, "error": "records must be a list of {group, doctype, name, parent}."}
        try:
            session = load_session(session_name)
        except frappe.DoesNotExistError:
            return {"success": False, "error": f"Import session '{session_name}' not found."}
        except frappe.PermissionError as e:
            return permission_error_result(SESSION_DOCTYPE, exception_message(e), name=session_name)

        try:
            if dry_run.is_running(session):
                raise dry_run.DryRunProblem(
                    _("The file is still being checked. Create the masters once the check has finished.")
                )
            if session.status not in ("Dry Run Failed", "Ready to Import") or not _checked_as_is(session):
                raise dry_run.DryRunProblem(
                    _(
                        "The file or its mapping changed since the last check, or it hasn't been "
                        "checked. Check it with dry_run_import first: the masters come from that check."
                    )
                )
            created = import_fixes.create_masters(session, import_fixes.last_groups(session), records)
        except import_fixes.FixProblems as e:
            return {"success": False, "error": str(e), "problems": e.problems, "session": session_name}
        except import_fixes.FixProblem as e:
            # A record refused while creating: the batch was undone, with the Error Log
            # create_document wrote for it, so keep one here.
            frappe.log_error(title=_("FAC Import: missing masters not created"), message=str(e))
            return {"success": False, "error": str(e), "session": session_name}
        except dry_run.DryRunProblem as e:
            return {"success": False, "error": str(e), "session": session_name}
        except Exception as e:
            frappe.log_error(title=_("Create Missing Masters Error"), message=frappe.get_traceback())
            return {"success": False, "error": str(e), "session": session_name}

        return {
            "success": True,
            "session": session.name,
            "created": created,
            "message": _(
                "Created {0}, linked to the import session. Check the file again with dry_run_import."
            ).format(_("1 record") if len(created) == 1 else _("{0} records").format(len(created))),
        }


def _checked_as_is(session) -> bool:
    """Whether the last check was of the session as it is now: same file, mapping and fixes."""
    if not session.source_file or not session.validated_fingerprint:
        return False
    file_doc = frappe.get_doc("File", session.source_file)
    return dry_run.fingerprint(session, file_doc) == session.validated_fingerprint


# Make sure class name matches file name for discovery
create_missing_masters = CreateMissingMasters
