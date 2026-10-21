# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Preview Import Undo Tool for Core Plugin (#34002).

Says what undoing an import session would delete and what blocks it, without changing any
record. undo_import then removes it, after one approval.
"""

from collections import defaultdict
from typing import Any, Dict, List

import frappe
from frappe import _

from frappe_assistant_core.core.base_tool import BaseTool, exception_message, permission_error_result
from frappe_assistant_core.plugins.core import import_undo
from frappe_assistant_core.plugins.core.import_mapping import SESSION_DOCTYPE, load_session


def _named(names: List[str]) -> List[str]:
    """Up to MAX_NAMED names, then "(N more)"."""
    shown = names[: import_undo.MAX_NAMED]
    if len(names) > len(shown):
        shown.append(_("({0} more)").format(len(names) - len(shown)))
    return shown


def _label(doctype: str, name: str) -> str:
    return f"{doctype} {name}"


class PreviewImportUndo(BaseTool):
    """What undoing an import session would delete, and what blocks it. Changes nothing."""

    def __init__(self):
        super().__init__()
        self.name = "preview_import_undo"
        self.description = (
            "Show what undoing an import session would delete, before anything is deleted. Call "
            "this first whenever the user asks to undo, reverse or remove an import. Changes no "
            "record. Show the user the counts by DocType, the blockers (records that can't be "
            "removed, grouped by reason, each with what it is blocked by), the kept records (not "
            "blocked themselves, but kept because a blocked record depends on them) and the "
            "records already removed. An Update Existing Records import can't be undone: nothing "
            "it changed is restored. A session past its time window (window.ends_on) or still "
            "importing is refused, with the reason. When something is blocked, ask the user "
            "which they want: stop, or mode unblocked (remove the rest, keep the blocked records "
            "and what they depend on). Then call undo_import with this reply's records and "
            "blocked figures and that mode. Deleted records cannot be restored."
        )
        self.requires_permission = None  # checked on the session; the job re-checks each record
        # The records are named as the user's file named them (#34003).
        self.untrusted_fields = ("by_doctype", "blockers", "kept_records", "error")

        self.inputSchema = {
            "type": "object",
            "properties": {
                "session": {"type": "string", "description": "The import session id"},
            },
            "required": ["session"],
        }

    def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        session_name = arguments.get("session")
        try:
            session = load_session(session_name)
        except frappe.DoesNotExistError:
            return {"success": False, "error": _("Import session '{0}' not found.").format(session_name)}
        except frappe.PermissionError as e:
            return permission_error_result(SESSION_DOCTYPE, exception_message(e), name=session_name)

        try:
            reply = {
                "success": True,
                "session": session.name,
                "status": session.status,
                "window": import_undo.window(session),
            }
            reason = import_undo.refusal(session)
            if reason:
                if session.status == import_undo.UNDOING:
                    # undo_import reports a running undo, and sets a stopped one back
                    following = _(
                        "Tell the user the undo is under way, then call undo_import (with the last "
                        "preview's figures, or any) to follow it: it only reports while the undo "
                        "runs, and if the undo stopped it says so and sets the session back, so the "
                        "user can preview again."
                    )
                else:
                    following = _("Tell the user why.")
                return {
                    **reply,
                    "can_undo": False,
                    "refused": reason,
                    "records": 0,
                    "by_doctype": [],
                    "blocked": 0,
                    "blockers": [],
                    "kept": 0,
                    "kept_records": [],
                    "already_removed": 0,
                    "import_type_note": None,
                    "next": following,
                }
            plan = import_undo.build_plan(session)
            reason = import_undo.plan_refusal(plan)
            if not reason:
                import_undo.remember_fingerprint(session, plan.fingerprint)
        except Exception as e:
            frappe.log_error(title=_("Preview Import Undo Error"), message=frappe.get_traceback())
            return {"success": False, "error": exception_message(e), "session": session_name}

        return {
            **reply,
            "can_undo": not reason,
            "refused": reason or None,
            "records": len(plan.records),
            "by_doctype": self._by_doctype(plan),
            "blocked": len(plan.blocked),
            "blockers": self._blockers(plan),
            "kept": len(plan.kept),
            "kept_records": self._kept(plan),
            "already_removed": len(plan.already_removed),
            "import_type_note": plan.note,
            "next": self._next(plan, reason),
        }

    @staticmethod
    def _by_doctype(plan) -> List[Dict[str, Any]]:
        grouped = defaultdict(list)
        for record in plan.records:
            grouped[record.doctype].append(record.name)
        ranked = sorted(grouped.items(), key=lambda item: -len(item[1]))
        shown = [
            {"doctype": doctype, "count": len(names), "sample": _named(names)}
            for doctype, names in ranked[: import_undo.MAX_LISTED]
        ]
        rest = ranked[import_undo.MAX_LISTED :]
        if rest:
            shown.append(
                {
                    "doctype": _("({0} more)").format(len(rest)),
                    "count": sum(len(n) for _dt, n in rest),
                    "sample": [],
                }
            )
        return shown

    @staticmethod
    def _blockers(plan) -> List[Dict[str, Any]]:
        grouped = defaultdict(list)
        for blocker in plan.blocked:
            grouped[blocker.reason].append(blocker)
        groups = []
        for reason, blockers in grouped.items():
            doctypes = sorted({b.record.doctype for b in blockers})
            sample = [
                {
                    "record": _label(b.record.doctype, b.record.name),
                    "blocked_by": b.blocked_by,
                    "detail": b.detail,
                }
                for b in blockers[: import_undo.MAX_NAMED]
            ]
            if len(blockers) > len(sample):
                sample.append(
                    {
                        "record": _("({0} more)").format(len(blockers) - len(sample)),
                        "blocked_by": None,
                        "detail": None,
                    }
                )
            groups.append(
                {
                    "reason": reason,
                    "count": len(blockers),
                    "doctypes": _named(doctypes),
                    "sample": sample,
                }
            )
        return groups

    @staticmethod
    def _kept(plan) -> List[Dict[str, Any]]:
        shown = [
            {"record": _label(record.doctype, record.name), "kept_because": because}
            for record, because in plan.kept[: import_undo.MAX_LISTED]
        ]
        if len(plan.kept) > len(shown):
            shown.append(
                {"record": _("({0} more)").format(len(plan.kept) - len(shown)), "kept_because": None}
            )
        return shown

    @staticmethod
    def _next(plan, refused: str) -> str:
        if refused:
            return _("Tell the user why nothing can be undone.")
        if plan.blocked:
            text = _(
                "Show the user the counts, the blockers and the kept records, and ask whether to stop or "
                "to undo with mode unblocked. Then call undo_import with records, blocked and the mode."
            )
        else:
            text = _(
                "Show the user the counts and the kept records. When they approve, call undo_import with "
                "records, blocked and mode all."
            )
        submitted = import_undo.submitted_count(plan.to_delete)
        if submitted:
            text += " " + _(
                "{0} of them are submitted documents: they are cancelled, then deleted, which also removes "
                "their own ledger entries (GL, Payment Ledger and Stock Ledger entries). Tell the user "
                "before they approve."
            ).format(submitted)
        return text


# Make sure class name matches file name for discovery
preview_import_undo = PreviewImportUndo
