# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Setup plan tool for the Core plugin (#34421, #34423): the discovery interview of a company's setup, kept
as a Draft setup plan and changed in chat. Writes only the plan; run_setup_plan applies it, on one
approval.
"""

from typing import Any, Dict

import frappe
from frappe import _

from frappe_assistant_core.core.base_tool import BaseTool, exception_message, permission_error_result
from frappe_assistant_core.utils import setup_plan, staff_setup
from frappe_assistant_core.utils.import_session import FileProblem

ACTIONS = ("discover", "save", "update")


class SetupPlan(BaseTool):
    """Discover what a company's setup needs, save it as a Draft plan, and change the plan."""

    def __init__(self):
        super().__init__()
        self.name = "setup_plan"
        self.description = (
            "The setup interview, kept as a Draft setup plan; writes nothing to the site. "
            "action discover: the areas in order (company and branches, GST, financial year, chart of "
            "accounts, what they sell and buy, stock and valuation, price lists and currencies, payment "
            "terms, approvals, numbering, users and roles), each with what the site already has (`set`, "
            "`summary`) and, for an area not set, the plain-word `questions`. Ask only about areas that "
            "aren't set, unless the user wants to change one. With staff_session (a staff list the "
            "user uploaded with files to import, opened by start_import_session): its `staff_list`, the "
            "columns and every row, to propose the groups from; the session is cancelled, as a staff list "
            "isn't imported. "
            "action save: the decisions as a Draft plan, with company and `answers`. Keys: branches "
            "(names), fiscal_year ({from, to}), item_groups ([{name, parent}] or names), uoms (names), "
            "valuation_method (FIFO or Moving Average, before any stock entry), allow_negative_stock "
            "(bool), warehouses ([{name, parent, is_group}] or names), currencies (codes), price_lists "
            "([{name, currency, selling, buying}]), default_selling_price_list, default_buying_price_list, "
            "payment_terms ([{name, due_days}]), staff (groups from the client's staff list, see "
            "get_skill('staff-setup'): [{name: the role profile, roles, user_permissions: [{allow, "
            "for_value}], people: [{full_name, email}]}]), send_welcome_email (bool, only if the user "
            "asks), fit_gap ([{requirement, why_not_standard, suggestion}]) and items ([{area, decision, "
            "target_doctype, target_name, values, depends_on, created_record}]) for anything else. A "
            "customisation record (Custom Field, Property Setter, Workflow, ...) is never made by the plan: "
            "create it first with create_document, on its own card, and pass its name as created_record. "
            "The result lists the items by number, what the site already has, and `problems` (what was "
            "left out and why). Show the plan by area, link it, and let the user change it. "
            "action update: change a Draft plan: items ({number: {decision, values}}; a staff group is "
            "{number: {values: {group}}} and applies to everyone in it), remove ([numbers]), add (items, "
            "or {area: 'staff', group}) or failure_rule. An approved or finished plan can't be changed: "
            "save a new one. Then run_setup_plan with the plan's `approval`."
        )
        self.requires_permission = None  # discover reads as the user; save and update check the plan
        self.untrusted_fields = ("items", "fit_gap", "problems", "error", "staff_list")
        self.inputSchema = {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(ACTIONS)},
                "company": {
                    "type": "string",
                    "description": "discover, save: the company. discover without it: the site's default, "
                    "else its only one.",
                },
                "staff_session": {
                    "type": "string",
                    "description": "discover: the import session that holds the client's staff list.",
                },
                "answers": {"type": "object", "description": "save: the interview's decisions."},
                "title": {"type": "string", "description": "save: a name for the plan."},
                "plan": {"type": "string", "description": "update: the FAC Setup Plan id."},
                "items": {"type": "object", "description": "update: {item number: {decision, values}}."},
                "remove": {"type": "array", "items": {"type": "integer"}, "description": "update: numbers."},
                "add": {"type": "array", "items": {"type": "object"}, "description": "update: items to add."},
                "failure_rule": {
                    "type": "string",
                    "enum": ["stop", "continue"],
                    "description": "stop (default): the plan stops at the first item that fails.",
                },
            },
            "required": ["action"],
        }

    def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        action = arguments.get("action")
        if action not in ACTIONS:
            return {"success": False, "error": _("action is one of {0}.").format(", ".join(ACTIONS))}
        try:
            return getattr(self, f"_{action}")(arguments)
        except frappe.DoesNotExistError:
            return {"success": False, "error": _("Setup plan '{0}' not found.").format(arguments.get("plan"))}
        except setup_plan.SetupProblem as e:
            return {"success": False, "error": str(e)}
        except frappe.PermissionError as e:
            if action == "discover":
                return {"success": False, "error": exception_message(e)}
            return permission_error_result(setup_plan.PLAN, exception_message(e), name=arguments.get("plan"))
        except Exception as e:
            frappe.log_error(title=_("Setup Plan Error"), message=frappe.get_traceback())
            return {"success": False, "error": exception_message(e)}

    def _discover(self, arguments):
        company = (arguments.get("company") or "").strip() or frappe.defaults.get_global_default("company")
        if not company:
            companies = frappe.get_all("Company", pluck="name", limit=2)
            company = companies[0] if len(companies) == 1 else ""
        if not company:
            return {"success": False, "error": _("Say which company to set up.")}
        found = {"success": True, "company": company, "areas": setup_plan.current_settings(company)}
        session = arguments.get("staff_session")
        if session:
            try:
                found["staff_list"] = staff_setup.take_staff_list(session)
            except frappe.DoesNotExistError:
                return {"success": False, "error": _("Import session '{0}' not found.").format(session)}
            except (frappe.ValidationError, FileProblem) as e:
                return {"success": False, "error": str(e)}
        return found

    def _save(self, arguments):
        frappe.has_permission(setup_plan.PLAN, "create", throw=True)
        doc, problems = setup_plan.save(
            arguments.get("company") or "",
            arguments.get("answers") or {},
            arguments.get("title"),
            arguments.get("failure_rule") or "stop",
        )
        return {"success": True, **setup_plan.describe(doc), "problems": problems}

    def _update(self, arguments):
        changes = {k: arguments[k] for k in ("items", "remove", "add", "failure_rule") if arguments.get(k)}
        doc = setup_plan.edit(arguments.get("plan"), changes)
        return {"success": True, **setup_plan.describe(doc)}
