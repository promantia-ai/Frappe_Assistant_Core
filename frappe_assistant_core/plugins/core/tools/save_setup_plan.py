# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Save Setup Plan Tool for Core Plugin (#34421)."""

from typing import Any, Dict

import frappe
from frappe import _

from frappe_assistant_core.core.base_tool import BaseTool, exception_message, permission_error_result
from frappe_assistant_core.utils import setup_plan


class SaveSetupPlan(BaseTool):
    """Save what the discovery interview decided as a Draft setup plan."""

    def __init__(self):
        super().__init__()
        self.name = "save_setup_plan"
        self.description = (
            "Save the discovery interview's decisions as a Draft setup plan. Writes only the plan, "
            "nothing to the site. `answers` is an object; keys it understands: branches (names), "
            "fiscal_year ({from, to} dates), item_groups ([{name, parent}] or names), uoms (names), "
            "valuation_method (FIFO or Moving Average, decide before any stock entry), "
            "allow_negative_stock (bool), warehouses ([{name, parent, is_group}] or names), currencies "
            "(codes to enable), price_lists ([{name, currency, selling, buying}]), "
            "default_selling_price_list, default_buying_price_list, payment_terms ([{name, due_days}]), "
            "fit_gap ([{requirement, why_not_standard, suggestion}]: what ERPNext doesn't cover as asked), "
            "and items ([{area, decision, target_doctype, target_name, values, depends_on, created_record}]) "
            "for anything else. A record of a customisation DocType (Custom Field, Property Setter, "
            "Workflow, ...) is never made by the plan: create it first with create_document, on its own "
            "card, and pass its name as created_record. The result lists the items by number, what the "
            "site already has, and `problems` (what was left out and why). Show the plan by area, link it, "
            "and let the user change it (update_setup_plan) before run_setup_plan."
        )
        self.requires_permission = setup_plan.PLAN
        self.untrusted_fields = ("items", "fit_gap", "problems", "error")
        self.inputSchema = {
            "type": "object",
            "properties": {
                "company": {"type": "string", "description": "The company the plan is for."},
                "answers": {
                    "type": "object",
                    "description": "The interview's decisions; see the description.",
                },
                "title": {"type": "string", "description": "A name for the plan."},
                "failure_rule": {
                    "type": "string",
                    "enum": ["stop", "continue"],
                    "description": "stop (default): the plan stops at the first item that fails.",
                },
            },
            "required": ["company", "answers"],
        }

    def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        try:
            doc, problems = setup_plan.save(
                arguments.get("company") or "",
                arguments.get("answers") or {},
                arguments.get("title"),
                arguments.get("failure_rule") or "stop",
            )
        except setup_plan.SetupProblem as e:
            return {"success": False, "error": str(e)}
        except frappe.PermissionError as e:
            return permission_error_result(setup_plan.PLAN, exception_message(e))
        except Exception as e:
            frappe.log_error(title=_("Save Setup Plan Error"), message=frappe.get_traceback())
            return {"success": False, "error": exception_message(e)}
        return {"success": True, **setup_plan.describe(doc), "problems": problems}


# Make sure class name matches file name for discovery
save_setup_plan = SaveSetupPlan
