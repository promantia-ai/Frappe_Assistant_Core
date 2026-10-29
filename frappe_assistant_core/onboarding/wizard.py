# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""The setup wizard's answers, kept for the go-live entry point (#34419).

ERPNext v16's wizard asks "Who are you setting this up for?" and "What do you use today?" and sends
the answers only to its telemetry. Frappe passes the whole form to every app's setup_wizard_complete
hook, so FAC keeps them on the company's FAC Go-Live. v15's wizard asks neither: the intent is Unknown
and the new-site rule decides.
"""

from typing import Any, Dict, Optional

import frappe

from frappe_assistant_core.assistant_core.doctype.fac_go_live.fac_go_live import get_or_create
from frappe_assistant_core.onboarding import entry

INTENTS = {
    "A client I'm consulting for": "Consultant for a client",
    "My own business": "Own business",
    "A company I work for": "Company I work for",
}


def after_wizard(args: Optional[Dict[str, Any]]) -> None:
    """setup_wizard_complete hook. Never raises: Frappe rolls the whole wizard back if a hook does."""
    try:
        args = args or {}
        company = (args.get("company_name") or "").strip()
        if not company or not frappe.db.exists("Company", company):
            companies = entry._companies()
            company = companies[0] if len(companies) == 1 else None
        if not company:
            return
        intent = INTENTS.get(args.get("persona_implementing_for") or "", "Unknown")
        doc = get_or_create(company)
        doc.update(
            {
                "intent": intent,
                "current_system": args.get("persona_current_system") or None,
                "intent_source": "Setup wizard",
            }
        )
        doc.save(ignore_permissions=True)
        # The wizard runs once, at the start: switching the tools on here overrides nobody's choice.
        # After the wizard's commit, as enable_plugin commits and the wizard must stay able to roll back.
        if intent == "Consultant for a client":
            frappe.db.after_commit.add(_enable_tools)
        entry.clear_cache()
    except Exception:
        frappe.log_error(title="FAC go-live wizard hook", message=frappe.get_traceback())


def _enable_tools() -> None:
    try:
        manager = entry.get_plugin_manager()
        if entry.MIGRATION_PLUGIN not in manager.get_enabled_plugins():
            manager.enable_plugin(entry.MIGRATION_PLUGIN)
        entry.clear_cache()
    except Exception:
        frappe.log_error(title="FAC go-live wizard hook", message=frappe.get_traceback())
