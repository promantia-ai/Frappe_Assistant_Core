# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Dry Run Import Tool for Core Plugin.

Checks every row of an import session's file the way the real import would, and
writes nothing. The full per-row result goes on the session; the model gets the
grouped summary.
"""

from typing import Any, Dict

import frappe
from frappe import _

from frappe_assistant_core.core.base_tool import BaseTool, exception_message, permission_error_result
from frappe_assistant_core.plugins.core import import_dry_run as dry_run
from frappe_assistant_core.plugins.core.import_mapping import SESSION_DOCTYPE, load_session
from frappe_assistant_core.plugins.core.import_session import FileProblem


class DryRunImport(BaseTool):
    """Check every row of an import session's file without importing anything."""

    def __init__(self):
        super().__init__()
        self.name = "dry_run_import"
        self.description = (
            "Check every row of an import session's file the way the real import would, writing "
            "nothing: required fields, links, duplicates (in the file and already in ERPNext) and "
            "ERPNext's own rules. Call it once the columns are mapped, and again after fixes. "
            "Returns '<n> rows ready, <m> would fail' and the failures grouped by cause, each with "
            "a count and up to 3 example rows; show the user that, not row-by-row lists. Files of "
            f"more than {dry_run.BACKGROUND_ROWS} rows are checked in the background: say so, and "
            "read the session with get_document later (progress, then validation_summary). The "
            "file must be on this site: if the session's file is only in the chat, ask the user "
            "to attach it in FAC Chat and pass its file_url. Next: explain each group and agree "
            "fixes with the user."
        )
        self.requires_permission = None  # checked on the session and the target DocType

        self.inputSchema = {
            "type": "object",
            "properties": {
                "session": {"type": "string", "description": "The import session id"},
                "file_url": {
                    "type": "string",
                    "description": "Only when the session's file is just in the chat: the same file "
                    "on this site, e.g. a FAC Chat attachment. Its columns must match.",
                },
            },
            "required": ["session"],
        }

    def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        session_name = arguments.get("session")
        try:
            session = load_session(session_name)
        except frappe.DoesNotExistError:
            return {"success": False, "error": f"Import session '{session_name}' not found."}
        except frappe.PermissionError as e:
            return permission_error_result(SESSION_DOCTYPE, exception_message(e), name=session_name)

        try:
            if dry_run.is_running(session):
                return self._running(session)
            if arguments.get("file_url"):
                dry_run.link_site_file(session, arguments["file_url"])
            dry_run.check_can_run(session)

            if dry_run.row_count(session) > dry_run.BACKGROUND_ROWS:
                dry_run.enqueue(session)
                return self._running(session)

            summary = dry_run.run(session.name)
        except (dry_run.DryRunProblem, FileProblem) as e:
            return {"success": False, "error": str(e), "session": session_name}
        except Exception as e:
            frappe.log_error(title=_("Dry Run Import Error"), message=frappe.get_traceback())
            return {"success": False, "error": str(e), "session": session_name}

        session.reload()
        examples = dry_run.examples(session, summary["groups"])
        groups = []
        for group in summary["groups"]:
            shown = {k: v for k, v in group.items() if k != "example_rows"}
            shown["examples"] = examples.get(group["id"], [])
            groups.append(shown)
        return {
            "success": True,
            "session": session.name,
            "status": session.status,
            "message": dry_run.headline(summary) + " " + _("Nothing was written."),
            **{k: v for k, v in summary.items() if k != "groups"},
            "groups": groups,
        }

    @staticmethod
    def _running(session) -> Dict[str, Any]:
        progress = frappe.parse_json(session.progress or "{}") or {}
        return {
            "success": True,
            "session": session.name,
            "state": "running",
            "done": progress.get("done", 0),
            "total_rows": dry_run.row_count(session),
            "message": _(
                "Checking every row in the background. Read the session with get_document to see "
                "progress; the result will be in validation_summary."
            ),
        }


# Make sure class name matches file name for discovery
dry_run_import = DryRunImport
