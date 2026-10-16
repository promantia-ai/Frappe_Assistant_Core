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
from frappe_assistant_core.plugins.core import import_failures, import_fixes
from frappe_assistant_core.plugins.core.import_mapping import SESSION_DOCTYPE, load_session
from frappe_assistant_core.plugins.core.import_session import FileProblem, find_site_file


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
            "read the session with get_document later (progress, then validation_summary, which "
            "holds this same reply). The file must be on this site: if the session's file is only "
            "in the chat, ask the user to upload it in File Manager (or attach it in FAC Chat) and "
            "pass its file_id; the user must be allowed to read it. A different file passed as "
            "file_id or file_url puts back the rows left out before. Each group comes with a plain "
            "explanation and the fixes that fit it: explain it and offer only those fixes. For an Update "
            "Existing Records session the reply also has update: records_changing, fields_changing, "
            "by_field (how many records change each field) and up to 5 samples with each field's old "
            "and new value: show the user those as a table before asking to import. Rows that change "
            "nothing are counted in unchanged and skipped. Then "
            "call again with the user's choices in fixes: they are saved on the session and every "
            "row is checked again. Per group: 'map' a value to an existing one ('to': one of the "
            "group's 'closest' names, or one the user gives; never invent one), or 'leave_out' "
            "its rows (with a reason; the import skips and lists them). To create a missing "
            "record, use create_document, then call again without fixes. Nothing is saved if any "
            "fix is wrong. A group keeps its id from check to check; once its rows pass, the id is "
            "gone. After an import that didn't get every row in, call it again once the causes are "
            "fixed: it checks only the rows not imported yet ('imported' counts the rest), and "
            "start_import then imports just those. failed_rows_file: true checks nothing; it gives "
            "the rows still not imported (failed, or left out) as a CSV with the reason for each, "
            "for the user to correct and upload: show its download_url. The corrected file comes "
            "back as file_id (or file_url), and is checked like any other."
        )
        self.requires_permission = None  # checked on the session and the target DocType
        # what the user's file says reaches the model fenced (#34003)
        self.untrusted_fields = (
            "groups",
            "masters_plan",
            "left_out",
            "problems",
            "applied",
            "update",
            "error",
        )

        self.inputSchema = {
            "type": "object",
            "properties": {
                "session": {"type": "string", "description": "The import session id"},
                "file_id": {
                    "type": "string",
                    "description": "When the session's file is just in the chat: the ID of the same "
                    "file on this site (its name in File Manager). Or of the user's corrected file of "
                    "failed rows. Its columns must match.",
                },
                "file_url": {
                    "type": "string",
                    "description": "Instead of file_id: the file's URL on this site, e.g. a FAC Chat "
                    "attachment's.",
                },
                "failed_rows_file": {
                    "type": "boolean",
                    "description": "True to get the rows still not imported, with the reason for "
                    "each, as a file to download. Nothing is checked.",
                },
                "fixes": {
                    "type": "array",
                    "description": "The user's fixes for the last check's groups, one per group, "
                    "applied together before checking again.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "group": {"type": "string", "description": "Group id, e.g. 'G3fa2c1'"},
                            "action": {"type": "string", "enum": list(import_fixes.ACTIONS)},
                            "to": {
                                "type": "string",
                                "description": "For 'map': the existing value to use instead.",
                            },
                            "reason": {
                                "type": "string",
                                "description": "For 'leave_out': why, in the user's words.",
                            },
                        },
                        "required": ["group", "action"],
                    },
                },
            },
            "required": ["session"],
        }

    def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        session_name = arguments.get("session")
        # Clients fill optional inputs in, so an empty list means no fixes, as a missing one does.
        fixes = arguments.get("fixes") or None
        file_id = (arguments.get("file_id") or "").strip()
        file_url = (arguments.get("file_url") or "").strip()
        if fixes is not None and (not isinstance(fixes, list) or not all(isinstance(f, dict) for f in fixes)):
            return {"success": False, "error": "fixes must be a list of {group, action, ...}."}
        try:
            session = load_session(session_name)
        except frappe.DoesNotExistError:
            return {"success": False, "error": f"Import session '{session_name}' not found."}
        except frappe.PermissionError as e:
            return permission_error_result(SESSION_DOCTYPE, exception_message(e), name=session_name)

        if arguments.get("failed_rows_file"):
            if fixes or file_id or file_url:
                return {
                    "success": False,
                    "error": "failed_rows_file is asked on its own, without fixes or a file.",
                }
            try:
                return {"success": True, **import_failures.failed_rows_file(session)}
            except (import_failures.FailuresProblem, FileProblem) as e:
                return {"success": False, "error": str(e), "session": session_name}
            except Exception as e:
                frappe.log_error(title=_("Failed Rows File Error"), message=frappe.get_traceback())
                return {"success": False, "error": str(e), "session": session_name}

        try:
            if dry_run.is_running(session):
                if fixes:
                    raise dry_run.DryRunProblem(
                        _("The file is still being checked. Apply the fixes once the check has finished.")
                    )
                return self._running(session)
            notice = None
            # Read access is checked here, before the file is compared or linked.
            new_file = find_site_file(file_id=file_id, file_url=file_url) if file_id or file_url else None
            # The session's own file passed again (as a client may) is no new file: nothing to link.
            if new_file and not _is_sessions_file(session, new_file):
                if fixes:
                    raise dry_run.DryRunProblem(
                        _(
                            "This file isn't the session's. Check it first, without fixes: fixes "
                            "apply to the groups its check finds."
                        )
                    )
                dry_run.check_status(session)  # a session that can't be checked keeps its file
                before = session.source_file
                dry_run.link_site_file(session, new_file)
                if before and not _same_content(before, session.source_file):
                    notice = import_fixes.forget_left_out(session)
            if fixes and session.status not in import_failures.AFTER_CHECK:
                raise dry_run.DryRunProblem(
                    _("Check the file first, without fixes; fixes apply to the groups it finds.")
                )
            dry_run.check_can_run(session)

            applied = None
            if fixes:
                applied = import_fixes.apply_fixes(session, import_fixes.last_groups(session), fixes)
                session.save()

            if dry_run.row_count(session) > dry_run.BACKGROUND_ROWS:
                dry_run.enqueue(session)
                return self._running(session, applied, notice)

            reply = import_fixes.check(session.name)
        except import_fixes.FixProblems as e:
            return {"success": False, "error": str(e), "problems": e.problems, "session": session_name}
        except (import_fixes.FixProblem, dry_run.DryRunProblem, FileProblem) as e:
            return {"success": False, "error": str(e), "session": session_name}
        except Exception as e:
            frappe.log_error(title=_("Dry Run Import Error"), message=frappe.get_traceback())
            return {"success": False, "error": str(e), "session": session_name}

        result = {"success": True, **reply}
        if applied:
            result["applied"] = applied
        if notice:
            result["notice"] = notice
        return result

    @staticmethod
    def _running(session, applied=None, notice=None) -> Dict[str, Any]:
        progress = frappe.parse_json(session.progress or "{}") or {}
        result = {
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
        if applied:
            result["applied"] = applied
        if notice:
            result["notice"] = notice
        return result


def _is_sessions_file(session, file_doc) -> bool:
    """Whether ``file_doc`` holds the same bytes as the session's file."""
    return bool(session.source_file) and _same_content(session.source_file, file_doc.name)


def _same_content(file_a: str, file_b: str) -> bool:
    """Whether two File records hold the same bytes, so the same rows."""
    if file_a == file_b:
        return True
    hashes = [frappe.db.get_value("File", name, "content_hash") for name in (file_a, file_b)]
    return bool(hashes[0]) and hashes[0] == hashes[1]


# Make sure class name matches file name for discovery
dry_run_import = DryRunImport
