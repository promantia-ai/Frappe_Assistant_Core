# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Start Import Tool for Core Plugin (#33993).

Imports an import session's checked file through Frappe's own Data Import, after one approval,
and continues an import that was interrupted.
"""

from typing import Any, Dict

import frappe
from frappe import _

from frappe_assistant_core.core.base_tool import BaseTool, exception_message, permission_error_result
from frappe_assistant_core.plugins.core import import_run
from frappe_assistant_core.plugins.core.import_mapping import SESSION_DOCTYPE, load_session
from frappe_assistant_core.plugins.core.import_session import FileProblem


class StartImport(BaseTool):
    """Import a checked session's file through Data Import, or continue its import."""

    def __init__(self):
        super().__init__()
        self.name = "start_import"
        self.description = (
            "Import an import session's file into ERPNext through Frappe's Data Import, once the "
            "user approves. Only after dry_run_import said the file is ready, and only when the "
            "user asks to import. Pass doctype, rows (the check's 'ready' count) and import_type "
            "exactly as the last check reported them: the approval card shows them, and they must "
            "match. The import runs in the background: follow it with list_documents on 'Data "
            "Import Log' filtered by data_import (total_count with success 1 is the count "
            "imported) against the reply's total. It has ended only when the session's status "
            "(get_document) is no longer Importing: then its last step gives created, failed and "
            "left-out counts with row numbers. If it stopped (Data Import status Timed Out, or no "
            "progress for minutes), call again with the same values to continue: records already "
            "imported are skipped, never duplicated. If rows failed, the reply groups and explains "
            "them, with what to do next: once the user has fixed the causes and asks to try those "
            "again, check them with dry_run_import (it checks only the rows not imported yet), "
            "then call this with its counts: a new Data Import imports just those rows, with the "
            "fixes, and the session reports every run."
        )
        self.requires_permission = None  # checked on the session, the DocType and Data Import

        self.inputSchema = {
            "type": "object",
            "properties": {
                "session": {"type": "string", "description": "The import session id"},
                "doctype": {"type": "string", "description": "The DocType imported, as the last check said"},
                "rows": {"type": "integer", "description": "Rows to import: the last check's 'ready'"},
                "import_type": {
                    "type": "string",
                    "enum": ["Insert New Records", "Update Existing Records"],
                    "description": "New records or updates, as the last check said",
                },
            },
            "required": ["session", "doctype", "rows", "import_type"],
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
            import_run.check_card(
                session, arguments.get("doctype"), arguments.get("rows"), arguments.get("import_type")
            )
            problem = import_run.cannot_import(session.target_doctype)
            if problem:
                raise import_run.ImportProblem(problem)
            if session.status == "Completed":
                raise import_run.ImportProblem(
                    _("This file was already imported, in Data Import {0}.").format(
                        session.data_imports[-1].data_import
                    )
                )
            if session.status == import_run.IMPORTING:
                data_import, state = import_run.resume(session)
            else:
                data_import, state = import_run.start(session), "started"
        except (import_run.ImportProblem, FileProblem) as e:
            return {"success": False, "error": str(e), "session": session_name}
        except Exception as e:
            frappe.log_error(title=_("Start Import Error"), message=frappe.get_traceback())
            return {"success": False, "error": exception_message(e), "session": session_name}

        session.reload()
        result = import_run.result_of(session, data_import)
        queued = state == "running" or (state in ("started", "continued") and not import_run._runs_now())
        if queued:
            # Queued or still running in the background: what the Data Import shows now is from
            # before this run (a retry's old counts, say), not its end.
            result["state"] = state
            result["message"] = _(
                "Importing {0} {1} records in the background ({2} done so far). Follow it with "
                "get_document on Data Import {3}."
            ).format(
                # A queued Data Import hasn't counted its records yet: the check has.
                result["total"] or arguments.get("rows"),
                _(session.target_doctype),
                result["created"],
                data_import,
            )
        return {"success": True, "session": session.name, **result}


# Make sure class name matches file name for discovery
start_import = StartImport
