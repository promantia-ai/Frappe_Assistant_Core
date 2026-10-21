# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Run Migration Plan Tool for Core Plugin (#33995).

The one approval of a migration plan: creates its masters and imports every file in order in the
background, with no questions after it. Called again, it reports how far the plan got.
"""

from typing import Any, Dict

import frappe
from frappe import _

from frappe_assistant_core.core.base_tool import BaseTool, exception_message, permission_error_result
from frappe_assistant_core.plugins.core import import_plan


class RunMigrationPlan(BaseTool):
    """Run an approved migration plan, or report a running one."""

    def __init__(self):
        super().__init__()
        self.name = "run_migration_plan"
        self.description = (
            "Run a migration plan that is Ready to Run, once the user approves: creates the "
            "masters the plan planned, then imports every file in order in the background, each "
            "checked again just before its import, rows that would fail then handled by the plan's "
            "failure rule. Nothing asks the user again. Pass files, rows and masters exactly as "
            "plan_questions' last result implies (the card shows them), or nothing runs. Called "
            "again for a plan that is running or done, it only reports: each file's status, "
            "created and left-out counts. Stopping keeps what is imported (stop_migration_plan)."
        )
        self.requires_permission = None  # checked on the plan; each file's import checks its own
        # File names and messages quote the user's files (#34003).
        self.untrusted_fields = ("files", "message", "error")

        self.inputSchema = {
            "type": "object",
            "properties": {
                "plan": {"type": "string", "description": "The migration plan id"},
                "files": {"type": "integer", "description": "How many files the plan imports."},
                "rows": {
                    "type": "integer",
                    "description": "How many rows are ready to import, all files together.",
                },
                "masters": {
                    "type": "integer",
                    "description": "How many missing records the plan creates first.",
                },
            },
            "required": ["plan", "files", "rows", "masters"],
        }

    def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        plan_name = arguments.get("plan")
        try:
            plan = import_plan.load_plan(plan_name)
        except frappe.DoesNotExistError:
            return {"success": False, "error": _("Migration plan '{0}' not found.").format(plan_name)}
        except frappe.PermissionError as e:
            return permission_error_result(import_plan.PLAN_DOCTYPE, exception_message(e), name=plan_name)
        if plan.status not in import_plan.OPEN:
            return {"success": True, **import_plan.result(plan)}  # running or done: report

        problem = import_plan.run_problem(plan)
        if not problem:
            figures = import_plan.card_figures(plan)
            asked = {key: arguments.get(key) for key in figures}
            if asked != figures:
                problem = _(
                    "The plan imports {files} files, {rows} rows, and creates {masters} records first. "
                    "Pass those, so the approval card shows them."
                ).format(**figures)
        if problem:
            return {"success": False, "error": problem, "plan": plan.name}
        try:
            import_plan.start(plan)
        except Exception as e:
            frappe.log_error(title=_("Run Migration Plan Error"), message=frappe.get_traceback())
            return {"success": False, "error": exception_message(e), "plan": plan.name}
        plan.reload()
        return {"success": True, **import_plan.result(plan)}


# Make sure class name matches file name for discovery
run_migration_plan = RunMigrationPlan
