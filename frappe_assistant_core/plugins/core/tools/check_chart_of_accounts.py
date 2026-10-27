# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Check Chart Of Accounts Tool for Core Plugin (#34422)."""

from typing import Any, Dict

import frappe
from frappe import _

from frappe_assistant_core.core.base_tool import BaseTool, exception_message, permission_error_result
from frappe_assistant_core.utils import import_chart
from frappe_assistant_core.utils.import_mapping import SESSION_DOCTYPE, load_session

SHOWN = 100


class CheckChartOfAccounts(BaseTool):
    """Read the client's chart of accounts from an import session and say what it would become."""

    def __init__(self):
        super().__init__()
        self.name = "check_chart_of_accounts"
        self.description = (
            "Read the client's own chart of accounts from an import session (a sheet from Tally, Zoho, "
            "QuickBooks or any export, with columns mapped to Account fields with set_column_mapping: "
            "Account Name, Parent Account, and where the file has them Account Number, Root Type, Account "
            "Type, Is Group). Shows the tree's top levels with counts and each account's proposed root "
            "type and account type, ALL the accounts it couldn't classify in one list (ask the user about "
            "them together; pass the answers as `classifications` on the next call), the problems of the "
            "tree (a cycle, an account under a ledger, an account twice) and, with `company`, which modes "
            "the company allows and, for map mode, which of the client's accounts match the company's. "
            "Writes nothing. Next: import_chart_of_accounts (map or add) or replace_chart_of_accounts."
        )
        self.requires_permission = None  # checked on the session
        self.untrusted_fields = (
            "outline",
            "unclassified",
            "problems",
            "parents_not_in_the_file",
            "map",
            "error",
        )
        self.inputSchema = {
            "type": "object",
            "properties": {
                "session": {"type": "string", "description": "The import session of the chart file."},
                "company": {"type": "string", "description": "The company the chart is for."},
                "root_type_column": {
                    "type": "string",
                    "description": "The heading of the column that holds each account's root type (Asset, Liability, Equity, Income, Expense), when the file has one: it can't be mapped, Root Type being read-only on Account.",
                },
                "classifications": {
                    "type": "object",
                    "description": "The user's answers for accounts that couldn't be classified: "
                    "{account name: {root_type, account_type}}.",
                },
            },
            "required": ["session"],
        }

    def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        name, company = arguments.get("session"), (arguments.get("company") or "").strip()
        try:
            session = load_session(name)
            nodes = import_chart.read_chart(session, arguments.get("root_type_column"))
            if company and frappe.db.exists("Company", company):
                import_chart.apply_outside_parents(nodes, company)
            import_chart.apply_answers(nodes, arguments.get("classifications"))
            classes = import_chart.classify(nodes)
            out: Dict[str, Any] = {"success": True, "session": name, **import_chart.summary(nodes, classes)}
            if company:
                if not frappe.db.exists("Company", company):
                    return {"success": False, "error": _("Company {0} doesn't exist.").format(company)}
                out["company"] = company
                out["modes"] = import_chart.modes(company)
                proposal = import_chart.map_proposal(nodes, classes, company)
                out["map"] = {
                    "matched": len(proposal["matched"]),
                    "unmatched": len(proposal["unmatched"]),
                    "unmatched_accounts": proposal["unmatched"][:SHOWN],
                }
            return out
        except frappe.DoesNotExistError:
            return {"success": False, "error": _("Import session '{0}' not found.").format(name)}
        except frappe.PermissionError as e:
            return permission_error_result(SESSION_DOCTYPE, exception_message(e), name=name)
        except import_chart.ChartProblem as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            frappe.log_error(title=_("Check Chart Of Accounts Error"), message=frappe.get_traceback())
            return {"success": False, "error": exception_message(e)}


# Make sure class name matches file name for discovery
check_chart_of_accounts = CheckChartOfAccounts
