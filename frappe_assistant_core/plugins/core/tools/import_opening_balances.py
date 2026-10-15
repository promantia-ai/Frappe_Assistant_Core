# frappe_assistant_core/plugins/core/tools/import_opening_balances.py
# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Import Opening Balances Tool: create and submit the checked opening Journal Entry (#33998)."""

from typing import Any, Dict

import frappe
from frappe import _

from frappe_assistant_core.core.base_tool import BaseTool, exception_message, permission_error_result
from frappe_assistant_core.plugins.core import import_opening_balances as opening_balances
from frappe_assistant_core.plugins.core.import_mapping import SESSION_DOCTYPE, load_session
from frappe_assistant_core.plugins.core.import_session import FileProblem


class ImportOpeningBalances(BaseTool):
    """Create and submit the checked opening Journal Entry, or report on it."""

    def __init__(self):
        super().__init__()
        self.name = "import_opening_balances"
        self.description = (
            "Bring the checked opening trial balance into ERPNext, once the user approves: one "
            "submitted opening Journal Entry (voucher type Opening Entry) at the cut-off date, all or "
            "nothing. Receivable, Payable and Stock rows are not in the entry; one balancing line to "
            "the Temporary Opening account keeps it balanced. Only after dry_run_import said the sheet "
            "is ready, and only when the user asks to import. Pass company, cut_off, rows (the check's "
            "'ready'), total_debit and total_credit exactly as the last check reported them: the "
            "approval card shows them, and they must match. Calling again never creates a second entry "
            "and reports what the first made."
        )
        self.requires_permission = None  # checked on the session and Journal Entry
        self.inputSchema = {
            "type": "object",
            "properties": {
                "session": {"type": "string", "description": "The import session id"},
                "company": {"type": "string"},
                "cut_off": {"type": "string", "description": "The cut-off date, YYYY-MM-DD"},
                "rows": {"type": "integer", "description": "The last check's 'ready'"},
                "total_debit": {"type": "number", "description": "The file's total debit from the check"},
                "total_credit": {"type": "number", "description": "The file's total credit from the check"},
            },
            "required": ["session", "company", "cut_off", "rows", "total_debit", "total_credit"],
        }

    def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        name = arguments.get("session")
        try:
            session = load_session(name)
        except frappe.DoesNotExistError:
            return {"success": False, "error": f"Import session '{name}' not found."}
        except frappe.PermissionError as e:
            return permission_error_result(SESSION_DOCTYPE, exception_message(e), name=name)
        if not opening_balances.is_opening_balances(session):
            return {
                "success": False,
                "error": _("This session isn't opening balances. Use start_import."),
                "session": name,
            }
        try:
            opening_balances.check_card(
                session,
                arguments.get("company"),
                arguments.get("cut_off"),
                arguments.get("rows"),
                arguments.get("total_debit"),
                arguments.get("total_credit"),
            )
            opening_balances.create(session)
        except (opening_balances.OpeningBalancesProblem, FileProblem) as e:
            return {"success": False, "error": str(e), "session": name}
        except Exception as e:
            frappe.log_error(title=_("Opening Balances Import Error"), message=frappe.get_traceback())
            return {"success": False, "error": exception_message(e), "session": name}
        session.reload()
        return {"success": True, "session": session.name, **opening_balances.report(session)}


# Make sure class name matches file name for discovery
import_opening_balances = ImportOpeningBalances
