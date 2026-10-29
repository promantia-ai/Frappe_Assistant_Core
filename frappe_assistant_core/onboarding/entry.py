# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""The go-live entry point (#34419): when FAC offers a System Manager to take a company live, the stage
the onboarding is at and what each of the six go-live steps counts as done. The Desk banner, the
checklist and the MCP server instructions all read these rules; they live here once.

Everything is read from the site's own records, so every consultant and every AI client sees the same.
"""

import json
from typing import Any, Dict, List, Optional

import frappe
from frappe import _

STEP_KEYS = ("discovery", "setup_plan", "chart_of_accounts", "data_migration", "go_live_test_run", "hypercare")
# Discovery can't be skipped: it is what the rest is planned from
SKIPPABLE = STEP_KEYS[1:]
STEP_LABELS = {
    "discovery": _("Discovery"),
    "setup_plan": _("Setup plan"),
    "chart_of_accounts": _("Chart of accounts"),
    "data_migration": _("Data migration"),
    "go_live_test_run": _("Go-live test run"),
    "hypercare": _("Hypercare"),
}
PLAN_NEEDS_ATTENTION = ("Partially Completed", "Failed", "Stopped")
# how many of the latest go_live_test_run calls are looked through for this company's
TEST_RUNS_READ = 20


def _record(doctype: str, name: Optional[str]) -> Optional[Dict[str, str]]:
    # doctype and name, not a URL: v15 opens forms under /app, v16 under /desk
    return {"doctype": doctype, "name": name} if name else None


def _go_live(company: str):
    name = frappe.db.get_value("FAC Go-Live", {"company": company})
    return frappe.get_doc("FAC Go-Live", name) if name else None


def _latest_plan(company: str) -> Optional[Dict[str, Any]]:
    plans = frappe.get_all(
        "FAC Setup Plan",
        filters={"company": company},
        fields=["name", "status"],
        order_by="creation desc",
        limit=1,
    )
    return plans[0] if plans else None


def _test_run(company: str) -> Optional[Dict[str, Any]]:
    """The latest go_live_test_run call for the company: {"name", "ready"}, or None.

    The company is read from the output's used.company, because the input has it only when the caller
    passed one. A truncated output can't be read: it is put down to the company through the input, or
    the site's only company, and counts as not ready.
    """
    runs = frappe.get_all(
        "Assistant Audit Log",
        filters={"tool_name": "go_live_test_run", "status": "Success"},
        fields=["name", "output_data", "output_truncated", "input_data"],
        order_by="creation desc",
        limit=TEST_RUNS_READ,
    )
    only_company = None
    for run in runs:
        if run.output_truncated:
            try:
                named = (json.loads(run.input_data or "{}") or {}).get("company")
            except (TypeError, ValueError):
                named = None
            if not named:
                if only_company is None:
                    companies = frappe.get_all("Company", pluck="name", limit=2)
                    only_company = companies[0] if len(companies) == 1 else ""
                named = only_company
            if named == company:
                return {"name": run.name, "ready": False}
            continue
        try:
            output = json.loads(run.output_data or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(output, dict):
            continue
        if ((output.get("used") or {}).get("company")) == company:
            return {"name": run.name, "ready": bool(output.get("ready"))}
    return None


def _hypercare() -> Optional[Dict[str, Any]]:
    # FAC Hypercare has no company field: it counts for the site, one onboarding at a time
    rows = frappe.get_all(
        "FAC Hypercare",
        fields=["name", "status", "start_date", "days"],
        order_by="creation desc",
        limit=1,
    )
    return rows[0] if rows else None


def steps(company: str) -> List[Dict[str, Any]]:
    """The six go-live steps for the company, each with its state and the record it links to.

    States: done, needs_attention, skipped, next (the first step neither done nor skipped, when it
    isn't already needs_attention) and not_started. Steps don't have to be done in order.
    """
    go_live = _go_live(company)
    skipped = {}
    if go_live:
        for row in go_live.skipped_steps:
            if row.step in SKIPPABLE:
                skipped[row.step] = {"by": row.skipped_by, "on": row.skipped_on, "reason": row.reason}

    plan = _latest_plan(company)
    run = _test_run(company)
    care = _hypercare()
    out = []

    def add(key, state, record=None, **extra):
        if state != "done" and key in skipped:
            state = "skipped"
        out.append(
            {
                "key": key,
                "label": STEP_LABELS[key],
                "state": state,
                "record": record,
                "skipped": skipped.get(key) if state == "skipped" else None,
                **extra,
            }
        )

    plan_record = _record("FAC Setup Plan", plan and plan.name)
    add("discovery", "done" if plan else "not_started", plan_record)

    setup_record = frappe.db.get_value(
        "FAC Onboarding Document", {"company": company, "document_type": "Setup Record"}, "name"
    )
    if plan and plan.status == "Completed":
        state = "done"
    elif plan and plan.status in PLAN_NEEDS_ATTENTION:
        state = "needs_attention"
    else:
        state = "not_started"
    add("setup_plan", state, plan_record, setup_record=_record("FAC Onboarding Document", setup_record))

    add(
        "chart_of_accounts",
        "done" if go_live and go_live.chart_imported_on else "not_started",
        _record("FAC Import Session", go_live and go_live.chart_import_session),
    )
    add(
        "data_migration",
        "done" if go_live and go_live.migration_completed_on else "not_started",
        _record("FAC Import Session", go_live and go_live.migration_plan),
    )
    add(
        "go_live_test_run",
        "done" if run and run["ready"] else "not_started",
        _record("Assistant Audit Log", run and run["name"]),
    )
    add(
        "hypercare",
        "done" if care and care.status in ("Active", "Ended") else "not_started",
        _record("FAC Hypercare", care and care.name),
    )

    for step in out:
        if step["state"] in ("done", "skipped"):
            continue
        if step["state"] == "not_started":
            step["state"] = "next"
        break
    return out


def next_step(step_list: List[Dict[str, Any]]) -> Optional[str]:
    """The key of the first step neither done nor skipped: only a suggestion."""
    for step in step_list:
        if step["state"] not in ("done", "skipped"):
            return step["key"]
    return None
