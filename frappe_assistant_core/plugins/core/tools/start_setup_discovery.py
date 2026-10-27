# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Start Setup Discovery Tool for Core Plugin (#34421)."""

from typing import Any, Dict

import frappe
from frappe import _

from frappe_assistant_core.core.base_tool import BaseTool, exception_message
from frappe_assistant_core.utils import setup_plan


class StartSetupDiscovery(BaseTool):
    """What the site already answers, area by area, and what to ask about the rest."""

    def __init__(self):
        super().__init__()
        self.name = "start_setup_discovery"
        self.description = (
            "Start the discovery interview of a company's setup. Returns the areas in order (company and "
            "branches, GST, financial year, chart of accounts, what they sell and buy, stock and valuation, "
            "price lists and currencies, payment terms, approvals, numbering), each with what the site "
            "already has (`set`, `summary`) and, for an area not set, the plain-word `questions` to ask. "
            "Ask only about areas that aren't set, unless the user wants to change one. Then save_setup_plan. "
            "Writes nothing."
        )
        self.requires_permission = None  # reads counts and settings as the user
        self.inputSchema = {
            "type": "object",
            "properties": {
                "company": {
                    "type": "string",
                    "description": "The company to set up. Left out: the site's default, else its only one.",
                }
            },
        }

    def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        company = (arguments.get("company") or "").strip() or frappe.defaults.get_global_default("company")
        if not company:
            companies = frappe.get_all("Company", pluck="name", limit=2)
            company = companies[0] if len(companies) == 1 else ""
        if not company:
            return {"success": False, "error": _("Say which company to set up.")}
        try:
            return {"success": True, "company": company, "areas": setup_plan.current_settings(company)}
        except setup_plan.SetupProblem as e:
            return {"success": False, "error": str(e)}
        except frappe.PermissionError as e:
            return {"success": False, "error": exception_message(e)}
        except Exception as e:
            frappe.log_error(title=_("Start Setup Discovery Error"), message=frappe.get_traceback())
            return {"success": False, "error": exception_message(e)}


# Make sure class name matches file name for discovery
start_setup_discovery = StartSetupDiscovery
