# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Migration recipes (#34420): a finished migration kept as a recipe, run again on new files.

``from_plan`` / ``from_session`` write a FAC Migration Recipe from a migration that ended: per file the
sheet and header signature, target DocType, mapping, value maps, source key and the opening engine's
settings, never a row of the file and never a date (M-D1). The run state of the plan or session
(progress, stop flag, fingerprints, left-out rows, file ids) is not copied.
"""

import json
import os
import re
from typing import Any, Dict, List, Optional

import frappe
from frappe import _

from frappe_assistant_core.utils import import_dry_run as dry_run
from frappe_assistant_core.utils import import_keys, import_plan
from frappe_assistant_core.utils.import_mapping import session_headers

RECIPE = "FAC Migration Recipe"
#: A recipe is made from files that were imported, as far as they went.
SAVEABLE = ("Completed", "Partially Completed")
#: The rules that carry over. left_out is row numbers of one file: it stays behind.
KEPT_RULES = ("value_maps", *import_keys.RULE_KEYS)
ENGINE_NAMES = {
    "import_opening_stock": "stock",
    "import_opening_invoices": "invoices",
    "import_opening_balances": "balances",
}
#: Imported by a delta run, the transactions made after the cut-off (M-D4).
TRANSACTION_DOCTYPES = (
    "Sales Invoice",
    "Purchase Invoice",
    "Payment Entry",
    "Journal Entry",
    "Stock Entry",
    "Delivery Note",
    "Purchase Receipt",
)
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")


class RecipeProblem(Exception):
    """The recipe can't be made or used. The message is shown to the user."""


def _json(value: Any) -> Optional[str]:
    return json.dumps(value, sort_keys=True, default=str) if value else None


def _without_dates(value: Any) -> Any:
    """``value`` without any key that names a date and any value that is one (M-D1: the cut-off rule is
    kept, not the date)."""
    if isinstance(value, dict):
        return {
            key: _without_dates(item)
            for key, item in value.items()
            if "date" not in str(key).lower() and "cut_off" not in str(key).lower() and not _is_date(item)
        }
    if isinstance(value, list):
        return [_without_dates(item) for item in value if not _is_date(item)]
    return value


def _is_date(value: Any) -> bool:
    return isinstance(value, str) and bool(_DATE.match(value))


def _engine(session) -> Optional[str]:
    engine = dry_run.special_engine(session)
    return ENGINE_NAMES.get(engine.__name__.rsplit(".", 1)[-1]) if engine else None


def step_from_session(session, position: int) -> Dict[str, Any]:
    """One recipe step from an imported session."""
    options = frappe.parse_json(session.template_options or "{}") or {}
    rules = frappe.parse_json(session.transformation_rules or "{}") or {}
    engine = dry_run.special_engine(session)
    preset = options.get("preset") or {}
    return {
        "position": position,
        "sheet_name": session.sheet or os.path.splitext(session.file_name or "")[0],
        "original_file_name": session.file_name,
        "target_doctype": session.target_doctype,
        "import_type": session.import_type,
        "preset_id": preset.get("id"),
        "opening_engine": _engine(session) or "none",
        "is_transaction_step": int(session.target_doctype in TRANSACTION_DOCTYPES),
        "header_signature": _json(session_headers(session)),
        "column_map": _json(options.get("column_to_field_map")),
        "transformation_rules": _json({key: rules[key] for key in KEPT_RULES if rules.get(key)}),
        "engine_settings": _json(_without_dates(engine.settings(session))) if engine else None,
    }


def _save(title: str, source_plan: str, failure_rule: Optional[str], steps: List[Dict[str, Any]]):
    if not steps:
        raise RecipeProblem(
            _("None of the files of this migration was imported, so there is nothing to save.")
        )
    doc = frappe.get_doc(
        {
            "doctype": RECIPE,
            "title": title,
            "source_site": frappe.local.site,
            "source_plan": source_plan,
            "failure_rule": failure_rule or "leave_out",
            "steps": steps,
        }
    )
    doc.insert()
    return doc


def from_plan(plan_name: str, title: Optional[str] = None):
    """A recipe from a migration plan that Completed or Partially Completed (M-D2)."""
    plan = import_plan.load_plan(plan_name)
    if plan.status not in SAVEABLE:
        raise RecipeProblem(
            _("Plan {0} is {1}: save a recipe from a plan that Completed or Partially Completed.").format(
                plan_name, _(plan.status)
            )
        )
    steps = [
        step_from_session(session, position)
        for position, session in enumerate(
            (
                s
                for s in import_plan.children(plan)
                if s.status in import_plan.IMPORTED and s.status != "Failed"
            ),
            start=1,
        )
    ]
    return _save(title or plan.title or _("Migration"), plan_name, plan.failure_rule, steps)


def from_session(session_name: str, title: Optional[str] = None):
    """A recipe from one import session that Completed (M-D2)."""
    session = dry_run.engine_session(session_name)
    if not frappe.has_permission(dry_run.SESSION_DOCTYPE, "read", doc=session):
        raise frappe.PermissionError(_("You can't read import session {0}.").format(session_name))
    if session.status != "Completed":
        raise RecipeProblem(
            _("Import session {0} is {1}: save a recipe from a file that Completed.").format(
                session_name, _(session.status)
            )
        )
    return _save(title or session.file_name, session_name, None, [step_from_session(session, 1)])


def describe(doc) -> Dict[str, Any]:
    """What FACO shows of a recipe: its step list and where it came from (RC-1, RC-7)."""
    return {
        "recipe": doc.name,
        "title": doc.title,
        "source_site": doc.source_site,
        "source_plan": doc.source_plan,
        "created_by": doc.created_by,
        "created_on": str(doc.created_on),
        "failure_rule": doc.failure_rule,
        "steps": [
            {
                "position": step.position,
                "file": step.original_file_name,
                "sheet": step.sheet_name,
                "target": step.target_doctype,
                "preset": step.preset_id,
                "opening_engine": step.opening_engine,
                "transaction_step": bool(step.is_transaction_step),
            }
            for step in doc.steps
        ],
    }
