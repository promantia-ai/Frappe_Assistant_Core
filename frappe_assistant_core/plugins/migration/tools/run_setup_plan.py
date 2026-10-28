# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Run Setup Plan Tool for Core Plugin (#34421)."""

from typing import Any, Dict

import frappe
from frappe import _
from frappe.utils import cstr

from frappe_assistant_core.core.base_tool import BaseTool, exception_message, permission_error_result
from frappe_assistant_core.utils import setup_plan
from frappe_assistant_core.utils.untrusted_output import as_fenced, unfenced


class RunSetupPlan(BaseTool):
    """Apply a Draft setup plan, once the user approves."""

    def __init__(self):
        super().__init__()
        self.name = "run_setup_plan"
        self.description = (
            "Apply a Draft setup plan on one approval: creates or updates every record and setting in it, "
            "in the order they need, in the background, as the user. Customisations in the plan (Custom "
            "Field, Property Setter, Workflow, ...) must already be created with create_document: the plan "
            "only checks them, and stops before writing anything if one is missing or differs. Pass "
            "`items`, `digest` and `decisions` exactly as setup_plan's `approval` "
            "gives them, so the approval card shows the user every item; a plan that changed since is "
            "refused. The plan stops at the first item that fails (or goes on, with failure_rule "
            "continue): what was applied stays, and the plan ends Completed, Partially Completed or "
            "Failed, never Running. Called for a plan that is running or done it only reports. The "
            "staff area's groups become users with their role profile and user permissions, all or "
            "nothing per group; welcome emails only if the plan says so, and FAC access is never given "
            "(seats are billed: added in FAC's user settings)."
        )
        self.requires_permission = None  # checked on the plan; each item checks its own
        self.untrusted_fields = ("decisions", "items[].message", "message", "error")
        self.inputSchema = {
            "type": "object",
            "properties": {
                "plan": {"type": "string", "description": "The FAC Setup Plan id."},
                "items": {"type": "integer", "description": "How many items the plan has."},
                "digest": {"type": "string", "description": "The plan's digest, as its approval gives it."},
                "decisions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Every item, one line each, as the approval gives them.",
                },
            },
            "required": ["plan", "items", "digest", "decisions"],
        }

    def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        name = arguments.get("plan")
        try:
            doc = frappe.get_doc(setup_plan.PLAN, name)
            doc.check_permission("write")
        except frappe.DoesNotExistError:
            return {"success": False, "error": _("Setup plan '{0}' not found.").format(name)}
        except frappe.PermissionError as e:
            return permission_error_result(setup_plan.PLAN, exception_message(e), name=name)
        if doc.status != "Draft":
            message = (
                _("Plan {0} is already running: nothing was started again.")
                if doc.status == "Running"
                else _("Plan {0} already {1}: nothing was started again.")
            )
            return {
                "success": True,
                **setup_plan.describe(doc),
                "message": message.format(doc.name, _(doc.status)),
                "already": doc.status,
            }
        problem = setup_plan.run_problem(doc)
        if problem:
            return {"success": False, "error": problem, "plan": doc.name}
        figures = setup_plan.card_figures(doc)
        asked = {key: arguments.get(key) for key in figures}
        # a line comes back as the model read it: fenced, a closing tag in it escaped
        asked["decisions"] = [as_fenced(unfenced(cstr(line)).strip()) for line in asked["decisions"] or []]
        if asked != {**figures, "decisions": [as_fenced(line) for line in figures["decisions"]]}:
            return {
                "success": False,
                "error": _(
                    "The plan has {items} items, and it may have changed since you showed it. Pass approval "
                    "exactly (items, digest, decisions), so the approval card shows the user all of it."
                ).format(**figures),
                "plan": doc.name,
                "approval": figures,
            }
        try:
            setup_plan.start(doc)
        except setup_plan.SetupProblem as e:
            return {"success": False, "error": str(e), "plan": doc.name}
        except Exception as e:
            frappe.log_error(title=_("Run Setup Plan Error"), message=frappe.get_traceback())
            return {"success": False, "error": exception_message(e), "plan": doc.name}
        return {"success": True, **setup_plan.describe(frappe.get_doc(setup_plan.PLAN, name))}


# Make sure class name matches file name for discovery
run_setup_plan = RunSetupPlan
