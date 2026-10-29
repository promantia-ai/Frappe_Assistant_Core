# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Endpoints of the go-live banner and checklist (#34419). The GET decides, the page only renders. The
POSTs are the user's own clicks: Not now, Already live, Skip, Undo and Turn on onboarding tools; each
checks the user's role again here."""

from typing import Any, Dict, Optional

import frappe
from frappe import _

from frappe_assistant_core.assistant_core.doctype.fac_go_live.fac_go_live import get_or_create
from frappe_assistant_core.onboarding import entry

SNOOZE_DAYS = 7


def _company(company: Optional[str]) -> str:
    company = company or entry.pick_company(frappe.session.user)
    if not company or not frappe.db.exists("Company", company):
        frappe.throw(_("Say which company to take live."))
    return company


@frappe.whitelist(methods=["GET"])
def get_entry_state(view: str = "banner", company: Optional[str] = None) -> Dict[str, Any]:
    """What the banner (view "banner") or the checklist (view "checklist") shows the current user."""
    return entry.get_state(frappe.session.user, view, company)


@frappe.whitelist(methods=["POST"])
def snooze(company: Optional[str] = None) -> Dict[str, Any]:
    """Not now: the banner stays away from this user for a week."""
    frappe.only_for("System Manager")
    doc = get_or_create(_company(company))
    until = frappe.utils.add_days(frappe.utils.now_datetime(), SNOOZE_DAYS)
    row = next((r for r in doc.snoozes if r.user == frappe.session.user), None)
    if row:
        row.until = until
    else:
        doc.append("snoozes", {"user": frappe.session.user, "until": until})
    doc.save(ignore_permissions=True)
    return {"snoozed_until": until}


@frappe.whitelist(methods=["POST"])
def mark_live(company: Optional[str] = None) -> Dict[str, Any]:
    """Already live: FAC stops offering to take the company live, for everyone."""
    frappe.only_for("System Manager")
    doc = get_or_create(_company(company))
    doc.update(
        {"marked_live": 1, "marked_live_by": frappe.session.user, "marked_live_on": frappe.utils.now_datetime()}
    )
    doc.save(ignore_permissions=True)
    return entry.get_state(frappe.session.user, "checklist", doc.company)


@frappe.whitelist(methods=["POST"])
def unmark_live(company: Optional[str] = None) -> Dict[str, Any]:
    frappe.only_for("System Manager")
    doc = get_or_create(_company(company))
    doc.update({"marked_live": 0, "marked_live_by": None, "marked_live_on": None})
    doc.save(ignore_permissions=True)
    return entry.get_state(frappe.session.user, "checklist", doc.company)


@frappe.whitelist(methods=["POST"])
def skip_step(company: str, step: str, reason: Optional[str] = None) -> Dict[str, Any]:
    frappe.only_for("System Manager")
    if step not in entry.SKIPPABLE:
        frappe.throw(_("The step {0} can't be skipped.").format(step))
    doc = get_or_create(_company(company))
    if not any(r.step == step for r in doc.skipped_steps):
        doc.append(
            "skipped_steps",
            {
                "step": step,
                "skipped_by": frappe.session.user,
                "skipped_on": frappe.utils.now_datetime(),
                "reason": reason,
            },
        )
        doc.save(ignore_permissions=True)
    return entry.get_state(frappe.session.user, "checklist", doc.company)


@frappe.whitelist(methods=["POST"])
def unskip_step(company: str, step: str) -> Dict[str, Any]:
    frappe.only_for("System Manager")
    doc = get_or_create(_company(company))
    doc.set("skipped_steps", [r for r in doc.skipped_steps if r.step != step])
    doc.save(ignore_permissions=True)
    return entry.get_state(frappe.session.user, "checklist", doc.company)


@frappe.whitelist(methods=["POST"])
def enable_tools() -> Dict[str, Any]:
    """Turn on onboarding tools: switch the Migration plugin on."""
    frappe.only_for("System Manager")
    if not frappe.has_permission("Assistant Core Settings", "write"):
        frappe.throw(_("Ask an administrator to turn on the onboarding tools."), frappe.PermissionError)
    entry.get_plugin_manager().enable_plugin(entry.MIGRATION_PLUGIN)
    entry.record_tools_switch(True)
    return entry.get_state(frappe.session.user, "banner")
