# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Plan Questions Tool for Core Plugin (#33995).

Every open question of a migration plan in one list, before anything is imported, and the user's
answers applied in one call. Once nothing is left, the plan is Ready to Run.
"""

from typing import Any, Dict

import frappe
from frappe import _

from frappe_assistant_core.core.base_tool import BaseTool, exception_message, permission_error_result
from frappe_assistant_core.plugins.core import import_fixes, import_plan


class PlanQuestions(BaseTool):
    """List a migration plan's open questions, or apply the user's answers to them."""

    def __init__(self):
        super().__init__()
        self.name = "plan_questions"
        self.description = (
            "Every open question of a migration plan in one list, so the user answers them all "
            "before one approval: files still to map (kind structure), files that link to each "
            "other (order), each group of rows that would fail with the fixes that fit it (fix), "
            "the cut-off date of opening stock, invoices and balances (cut_off: ask the user, never "
            "guess it), and what happens to rows that still fail when their file comes up "
            "(failure_rule). "
            "Files are checked again first, in the plan's order. Ask once: one table of every "
            "question (duplicates and left-out rows too, and what needed_by says) with the answer "
            "you suggest and the cut-off date, in your message, not in the card; then ONE "
            "single-choice card with one short line (ask_user single_select, never "
            "multi_select): 'Apply all suggested answers and start (cut-off <date>)' or 'Let me "
            "change some'. A question with nothing to suggest gets its own single-choice card. "
            "Never questions as text to type answers to. Map files yourself where you are sure. "
            "Then call again with "
            "answers: [{id, ...}], a fix's fields as dry_run_import takes them (action map/"
            "leave_out/create...; create plans a missing record that the plan creates when it "
            "runs), {id: 'plan:cut_off', date}, or {id: 'plan:failure_rule', rule}. All answers "
            "apply or none. When ready is "
            "true, the plan is Ready to Run: next run_migration_plan, given approval exactly."
        )
        self.requires_permission = None  # checked on the plan and its sessions
        # Questions quote the user's files: values, file names, columns (#34003).
        self.untrusted_fields = ("questions", "files", "problems", "applied", "error", "approval")

        self.inputSchema = {
            "type": "object",
            "properties": {
                "plan": {"type": "string", "description": "The migration plan id"},
                "answers": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "The user's answers, each with the id of its question.",
                },
            },
            "required": ["plan"],
        }

    def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        plan_name = arguments.get("plan")
        answers = arguments.get("answers") or []
        if not isinstance(answers, list):
            return {"success": False, "error": _("answers must be a list.")}
        try:
            plan = import_plan.load_plan(plan_name)
        except frappe.DoesNotExistError:
            return {"success": False, "error": _("Migration plan '{0}' not found.").format(plan_name)}
        except frappe.PermissionError as e:
            return permission_error_result(import_plan.SESSION_DOCTYPE, exception_message(e), name=plan_name)
        if plan.status not in import_plan.OPEN:
            return {
                "success": False,
                "error": _("Plan {0} is {1}: it has no questions left.").format(plan.name, _(plan.status)),
            }
        try:
            applied = import_plan.apply_answers(plan, answers) if answers else []
            plan.reload()
            result = import_plan.open_questions(plan)
        except import_fixes.FixProblems as e:
            return {"success": False, "error": str(e), "problems": e.problems}
        except (import_plan.PlanProblem, import_fixes.FixProblem) as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            frappe.log_error(title=_("Plan Questions Error"), message=frappe.get_traceback())
            return {"success": False, "error": exception_message(e)}
        if applied:
            result["applied"] = applied
        result["next"] = _next(result, bool(answers))
        return {"success": True, **result}


def _next(result: Dict[str, Any], answered: bool) -> str:
    """What to do with the result: FACO otherwise asks again what the one card already asked."""
    if result.get("ready") and answered:
        return (
            "If the user chose 'Apply all suggested answers and start', call run_migration_plan now "
            "with approval exactly: they already said start, ask nothing more."
        )
    if result.get("ready"):
        return (
            "Show approval.decisions as a table in your message, then ONE ask_user single_select: "
            "'Start the migration (cut-off <date>)' or 'Not yet'."
        )
    if any(q.get("kind") in ("checking", "structure") for q in result.get("questions") or []):
        return "Map or wait for the files the questions name, then call plan_questions again."
    return (
        "One table in your message of every question (file, problem, suggested answer, the cut-off), "
        "then ONE ask_user single_select: 'Apply all suggested answers and start (cut-off <date>)' "
        "or 'Let me change some'. No other card."
    )


# Make sure class name matches file name for discovery
plan_questions = PlanQuestions
