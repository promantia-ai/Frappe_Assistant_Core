# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Update Setup Plan Tool for Core Plugin (#34421)."""

from typing import Any, Dict

import frappe
from frappe import _

from frappe_assistant_core.core.base_tool import BaseTool, exception_message, permission_error_result
from frappe_assistant_core.utils import setup_plan


class UpdateSetupPlan(BaseTool):
    """Change a Draft setup plan as the user asks, writing nothing to the site."""

    def __init__(self):
        super().__init__()
        self.name = "update_setup_plan"
        self.description = (
            "Change a Draft setup plan: a decision or its values (items: {number: {decision, values}}), "
            "remove items (remove: [numbers]), add items (add: [{area, decision, target_doctype, "
            "target_name, values}]) or the failure rule. For example 'use FIFO, not moving average' is "
            "items {n: {decision: 'Value stock by FIFO', values: {valuation_method: 'FIFO'}}}. Item numbers "
            "are the ones the plan shows. Writes only the plan. An approved or finished plan can't be "
            "changed: save a new one."
        )
        self.requires_permission = setup_plan.PLAN
        self.untrusted_fields = ("items", "fit_gap", "error")
        self.inputSchema = {
            "type": "object",
            "properties": {
                "plan": {"type": "string", "description": "The FAC Setup Plan id."},
                "items": {
                    "type": "object",
                    "description": "{item number: {decision, values, created_record}}.",
                },
                "remove": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Item numbers to remove.",
                },
                "add": {"type": "array", "items": {"type": "object"}, "description": "Items to add."},
                "failure_rule": {"type": "string", "enum": ["stop", "continue"]},
            },
            "required": ["plan"],
        }

    def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        plan = arguments.get("plan")
        changes = {k: arguments[k] for k in ("items", "remove", "add", "failure_rule") if arguments.get(k)}
        try:
            doc = setup_plan.edit(plan, changes)
        except frappe.DoesNotExistError:
            return {"success": False, "error": _("Setup plan '{0}' not found.").format(plan)}
        except setup_plan.SetupProblem as e:
            return {"success": False, "error": str(e)}
        except frappe.PermissionError as e:
            return permission_error_result(setup_plan.PLAN, exception_message(e), name=plan)
        except Exception as e:
            frappe.log_error(title=_("Update Setup Plan Error"), message=frappe.get_traceback())
            return {"success": False, "error": exception_message(e)}
        return {"success": True, **setup_plan.describe(doc)}


# Make sure class name matches file name for discovery
update_setup_plan = UpdateSetupPlan
