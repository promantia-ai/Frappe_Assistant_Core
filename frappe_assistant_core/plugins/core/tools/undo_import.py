# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Undo Import Tool for Core Plugin (#34002).

Removes what an import session created, in the background, after one approval and only for what
preview_import_undo showed. Called again, it reports how far the undo got.
"""

from typing import Any, Dict

import frappe
from frappe import _
from frappe.utils import cint

from frappe_assistant_core.core.base_tool import BaseTool, exception_message, permission_error_result
from frappe_assistant_core.plugins.core import import_undo
from frappe_assistant_core.plugins.core.import_mapping import SESSION_DOCTYPE, load_session


class UndoImport(BaseTool):
    """Delete the records an import session created, as previewed, or report the undo's progress."""

    def __init__(self):
        super().__init__()
        self.name = "undo_import"
        self.description = (
            "Delete the records an import session created, once the user approves. Only after "
            "preview_import_undo, whose counts, blockers and kept records the user has seen, and "
            "only when the user asks to undo the import. Pass records and blocked exactly as that "
            "preview gave them, and the mode the user chose: all (remove everything; refused if "
            "anything is blocked) or unblocked (remove the rest, keep the blocked records and what "
            "they depend on). The approval card shows them, and they must match what would be "
            "removed now, or nothing is deleted: preview again. Submitted documents are cancelled, "
            "then deleted. The undo runs in the background: follow it by calling this tool again "
            "(any values: it only reports while the undo runs, and never starts a second one) until "
            "state is done, with deleted, kept and the blocked records and their reasons. If state "
            "is stopped, say why; what was removed stays removed, and the user can undo again after "
            "a new preview. Deleted records cannot be restored."
        )
        self.requires_permission = None  # checked on the session; the job re-checks each record

        self.inputSchema = {
            "type": "object",
            "properties": {
                "session": {"type": "string", "description": "The import session id"},
                "records": {"type": "integer", "description": "The preview's 'records'"},
                "blocked": {"type": "integer", "description": "The preview's 'blocked'"},
                "mode": {
                    "type": "string",
                    "enum": list(import_undo.MODES),
                    "description": "all, or unblocked to keep the blocked records and what they need",
                },
            },
            "required": ["session", "records", "blocked", "mode"],
        }

    def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        session_name = arguments.get("session")
        try:
            session = load_session(session_name)
        except frappe.DoesNotExistError:
            return {"success": False, "error": f"Import session '{session_name}' not found."}
        except frappe.PermissionError as e:
            return permission_error_result(SESSION_DOCTYPE, exception_message(e), name=session_name)

        mode = arguments.get("mode")
        try:
            earlier = session.status
            if self._only_reports(session, arguments):
                return self._progress(session, already_ended=earlier != import_undo.UNDOING)
            if mode not in import_undo.MODES:
                raise import_undo.UndoProblem(_("The mode is all or unblocked, not {0}.").format(mode))
            self._check(session, arguments)
            state = import_undo.start(session, mode)
            session.reload()
        except import_undo.UndoProblem as e:
            return {"success": False, "error": str(e), "session": session_name}
        except Exception as e:
            frappe.log_error(title=_("Undo Import Error"), message=frappe.get_traceback())
            return {"success": False, "error": exception_message(e), "session": session_name}

        return self._progress(session, queued=state == "started")

    @staticmethod
    def _only_reports(session, arguments: Dict[str, Any]) -> bool:
        """Whether this call only reports: an undo that is running, or that ended (its result).
        A Partially Undone session asked again with the figures of a new preview is undone
        again, since what blocked it may be gone; with old figures it gets its result."""
        if session.status == import_undo.UNDOING:
            return True
        if not import_undo.has_result(session):
            return False
        if session.status == import_undo.UNDONE:
            return True
        if session.status != import_undo.PARTIALLY_UNDONE:
            return False
        plan = import_undo.build_plan(session)
        fresh = (
            cint(arguments.get("records")) == len(plan.records)
            and cint(arguments.get("blocked")) == len(plan.blocked)
            and import_undo.stored_fingerprint(session) == plan.fingerprint
        )
        return not fresh

    @staticmethod
    def _check(session, arguments: Dict[str, Any]) -> None:
        """Raise UndoProblem unless the session can be undone now, as previewed and as passed."""
        reason = import_undo.refusal(session)
        if reason:
            raise import_undo.UndoProblem(reason)
        plan = import_undo.build_plan(session)
        reason = import_undo.plan_refusal(plan)
        if reason:
            raise import_undo.UndoProblem(reason)
        stored = import_undo.stored_fingerprint(session)
        if not stored:
            raise import_undo.UndoProblem(
                _("This import has not been previewed. Call preview_import_undo first, and show the user.")
            )
        records, blocked = cint(arguments.get("records")), cint(arguments.get("blocked"))
        if (records, blocked) != (len(plan.records), len(plan.blocked)):
            raise import_undo.UndoProblem(
                _(
                    "Records {0} and blocked {1} don't match what would be removed now ({2} records, "
                    "{3} blocked). Call preview_import_undo again and pass its figures."
                ).format(records, blocked, len(plan.records), len(plan.blocked))
            )
        if stored != plan.fingerprint:
            raise import_undo.UndoProblem(
                _(
                    "The records changed since the preview. Call preview_import_undo again and show the "
                    "user what it says."
                )
            )
        if arguments.get("mode") == "all" and plan.blocked:
            raise import_undo.UndoProblem(
                _(
                    "{0} records are blocked, so mode all can't remove them. Ask the user, and use mode "
                    "unblocked to remove the rest and keep those."
                ).format(len(plan.blocked))
            )

    @staticmethod
    def _progress(session, queued: bool = False, already_ended: bool = False) -> Dict[str, Any]:
        """The undo as it stands: running (follow it), done (the result) or stopped (why).
        ``queued``: the job was queued by this call. The queue holds it only after the commit,
        so it is reported running without asking the queue (unless it already ran, as in tests).
        ``already_ended``: the session was Undone or Partially Undone before this call."""
        session.reload()
        if queued and session.status == import_undo.UNDOING:
            progress = frappe.parse_json(session.progress or "{}") or {}
            report = {
                "state": "running",
                "deleted": 0,
                "total": cint(progress.get("total")),
                "kept": 0,
                "blocked": [],
                "reason": None,
            }
        else:
            report = import_undo.report(session)
            session.reload()
        blocked = list(report["blocked"])
        shown = blocked[: import_undo.MAX_LISTED]
        if len(blocked) > len(shown):
            shown.append(
                {"record": _("({0} more)").format(len(blocked) - len(shown)), "reason": None, "detail": None}
            )
        state = report["state"]
        if state == "running":
            message = _(
                "Undoing in the background: {0} of {1} records removed so far. Call undo_import again "
                "to follow it."
            ).format(report["deleted"], report["total"])
        elif state == "done":
            message = _("Removed {0} records; {1} kept.").format(report["deleted"], report["kept"])
            if blocked:
                message += " " + _("{0} can't be removed.").format(len(blocked))
            if already_ended:
                message = _("This import was already undone. ") + message
        else:
            message = report["reason"] or _("The undo stopped.")
        return {
            "success": True,
            "session": session.name,
            "status": session.status,
            "state": state,
            "deleted": report["deleted"],
            "total": report["total"],
            "kept": report["kept"],
            "blocked": shown,
            "blocked_count": len(blocked),
            "reason": report["reason"],
            "message": message,
        }


# Make sure class name matches file name for discovery
undo_import = UndoImport
