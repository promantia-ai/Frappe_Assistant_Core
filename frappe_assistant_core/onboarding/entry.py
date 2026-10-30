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

from frappe_assistant_core.utils.plugin_manager import get_plugin_manager

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
# one line on each checklist card: what the step is for
STEP_DESCRIPTIONS = {
    "discovery": _("What the client does, sells and uses today: the setup plan is built from it."),
    "setup_plan": _("Company settings, users, roles and masters, set up in one approved run."),
    "chart_of_accounts": _("The client's own accounts on the company, or ERPNext's chart kept."),
    "data_migration": _("Customers, suppliers, items and opening balances brought over from the old system."),
    "go_live_test_run": _("A buying and selling test and the readiness checks, before the client goes live."),
    "hypercare": _("A weekly summary of what goes wrong in the first weeks after go-live."),
}
PLAN_NEEDS_ATTENTION =("Partially Completed", "Failed", "Stopped")
# how many of the latest go_live_test_run calls are looked through for this company's
TEST_RUNS_READ = 20
# R6: a site this small and this young is offered onboarding although the wizard named no client
NEW_SITE_MAX_GL_ENTRIES = 50
NEW_SITE_MAX_AGE_DAYS = 60
# the site part of the check (stage, steps) is kept this long; the writes that change it clear it
CACHE_KEY = "fac_go_live_entry"
CACHE_SECONDS = 300
SHOWN_STAGES = ("not_started", "not_started_soft", "in_progress", "ready_to_go_live", "live_hypercare")
MIGRATION_PLUGIN = "migration"
ERROR_TITLE = "FAC go-live entry check"
HIDDEN = {"show": False}


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
                "description": STEP_DESCRIPTIONS[key],
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


def _looks_new(company: str) -> bool:
    """R6: fewer than NEW_SITE_MAX_GL_ENTRIES posted GL entries, the oldest under NEW_SITE_MAX_AGE_DAYS.

    The age is read from when the entry was made, not its posting date: an opening balance import
    posts on the old system's dates.
    """
    filters = {"company": company, "docstatus": 1, "is_cancelled": 0}
    if frappe.db.count("GL Entry", filters) >= NEW_SITE_MAX_GL_ENTRIES:
        return False
    oldest = frappe.get_all("GL Entry", filters=filters, fields=["creation"], order_by="creation asc", limit=1)
    if not oldest:
        return True
    age = frappe.utils.time_diff_in_seconds(frappe.utils.now_datetime(), oldest[0].creation) / 86400
    return age < NEW_SITE_MAX_AGE_DAYS


def stage(company: str, step_list: Optional[List[Dict[str, Any]]] = None) -> str:
    """Where the company's onboarding is (rules R1-R7, the first that matches wins).

    live_hypercare, done, ready_to_go_live, in_progress, not_started, not_started_soft (no intent
    from the wizard, but the site looks new) or established.
    """
    care = _hypercare()
    if care and care.status == "Active":
        return "live_hypercare"
    if care and care.status == "Ended":
        return "done"
    step_list = step_list if step_list is not None else steps(company)
    if any(s["key"] == "go_live_test_run" and s["state"] == "done" for s in step_list):
        return "ready_to_go_live"
    # before the new-site rule: a site with completed setup plans can still have few transactions
    if frappe.db.exists("FAC Setup Plan", {"company": company}) or frappe.db.exists(
        "FAC Import Session", {"plan_status": ("is", "set")}
    ):
        return "in_progress"
    if frappe.db.get_value("FAC Go-Live", {"company": company}, "intent") == "Consultant for a client":
        return "not_started"
    if _looks_new(company):
        return "not_started_soft"
    return "established"


def _companies() -> List[str]:
    return frappe.get_all("Company", pluck="name", order_by="creation asc")


def pick_company(user: str) -> Optional[str]:
    """The company the check is about (one onboarding at a time): the only one; else the latest setup
    plan's; else the one the setup wizard named; else the user's default company."""
    companies = _companies()
    if len(companies) == 1:
        return companies[0]
    if not companies:
        return None
    plan = frappe.get_all("FAC Setup Plan", fields=["company"], order_by="creation desc", limit=1)
    if plan and plan[0].company:
        return plan[0].company
    from_wizard = frappe.get_all(
        "FAC Go-Live",
        filters={"intent_source": "Setup wizard"},
        pluck="company",
        order_by="creation desc",
        limit=1,
    )
    if from_wizard:
        return from_wizard[0]
    return frappe.defaults.get_user_default("Company", user) or None


def clear_cache(*args, **kwargs) -> None:
    """Drop the cached site part of the check. Also a doc_events handler, hence the arguments."""
    frappe.cache().delete_keys(CACHE_KEY)


def clear_cache_for_import_session(doc, method=None) -> None:
    # only a migration plan changes the stage; a file's own session is saved at every step
    if doc.get("plan_status"):
        clear_cache()


def clear_cache_for_audit(doc, method=None) -> None:
    # step 5 is read from the latest go_live_test_run call
    if doc.get("tool_name") == "go_live_test_run":
        clear_cache()


def _record_result(company: Optional[str], values: Dict[str, Any]) -> None:
    try:
        if not company or not frappe.db.exists("Company", company):
            return
        from frappe_assistant_core.assistant_core.doctype.fac_go_live.fac_go_live import get_or_create

        doc = get_or_create(company)
        doc.update(values)
        doc.save(ignore_permissions=True)
    except Exception:
        # the import itself succeeded: a checklist that lags is better than a failed import
        frappe.log_error(title=ERROR_TITLE, message=frappe.get_traceback())


def record_chart(company: str, session: str) -> None:
    """Step 3 is done: import_chart_of_accounts put the client's chart on the company. The session
    keeps no company and no final status, so the checklist reads it from here."""
    _record_result(company, {"chart_imported_on": frappe.utils.now_datetime(), "chart_import_session": session})


def record_migration(plan_name: str) -> None:
    """Step 4 is done: a migration plan ended Completed or Partially Completed. A plan has no company:
    it is the one the go-live check is about."""
    _record_result(
        pick_company(frappe.session.user),
        {"migration_completed_on": frappe.utils.now_datetime(), "migration_plan": plan_name},
    )


def record_tools_switch(enabled: bool) -> None:
    """Who switched the Migration plugin off in FAC Admin, so the entry check doesn't switch it back on
    (6.5); switching it on clears it. Kept on every company's FAC Go-Live."""
    names = frappe.get_all("FAC Go-Live", pluck="name")
    if not names and not enabled:
        company = pick_company(frappe.session.user)
        if company:
            from frappe_assistant_core.assistant_core.doctype.fac_go_live.fac_go_live import get_or_create

            names = [get_or_create(company).name]
    values = (
        {"tools_disabled_by": None, "tools_disabled_on": None}
        if enabled
        else {"tools_disabled_by": frappe.session.user, "tools_disabled_on": frappe.utils.now_datetime()}
    )
    for name in names:
        frappe.db.set_value("FAC Go-Live", name, values)
    clear_cache()


def _hypercare_weeks(care) -> Dict[str, Any]:
    days_since = max(frappe.utils.date_diff(frappe.utils.nowdate(), care.start_date), 0)
    total = max(-(-int(care.days or 0) // 7), 1)
    return {"name": care.name, "week": min(days_since // 7 + 1, total), "total": total}


def site_state(company: str) -> Dict[str, Any]:
    """The part of the check that is the same for every user: stage, steps, the plugin. Cached."""
    key = f"{CACHE_KEY}:{company}"
    cached = frappe.cache().get_value(key)
    if cached:
        return cached
    step_list = steps(company)
    current = stage(company, step_list)
    care = _hypercare()
    # the plugin is the site's: switched off while the check was about another company counts too
    disabled_by = frappe.db.get_value("FAC Go-Live", {"tools_disabled_by": ("is", "set")}, "tools_disabled_by")
    state = {
        "company": company,
        "stage": current,
        "steps": step_list,
        "next_step": next_step(step_list),
        "hypercare": _hypercare_weeks(care) if care and current == "live_hypercare" else None,
        "plugin_on": MIGRATION_PLUGIN in get_plugin_manager().get_enabled_plugins(),
        "tools_disabled_by": disabled_by,
    }
    frappe.cache().set_value(key, state, expires_in_sec=CACHE_SECONDS)
    return state


def _erpnext_installed() -> bool:
    return "erpnext" in frappe.get_installed_apps()


def _site_hidden() -> bool:
    """S1-S4, in order: FAC on, ERPNext installed, its setup wizard finished, a company exists."""
    if not frappe.db.get_single_value("Assistant Core Settings", "server_enabled"):
        return True
    if not _erpnext_installed():
        return True
    if not frappe.is_setup_complete():
        return True
    return not _companies()


def _user_hidden(user: str) -> bool:
    """U1-U3: a Desk user, System Manager, who can read the records the checklist links to."""
    if not user or user == "Guest":
        return True
    if frappe.db.get_value("User", user, "user_type") != "System User":
        return True
    if "System Manager" not in frappe.get_roles(user):
        return True
    return not all(
        frappe.has_permission(doctype, "read", user=user)
        for doctype in ("FAC Setup Plan", "FAC Import Session", "FAC Hypercare")
    )


def _marked_live(company: str) -> Optional[Dict[str, Any]]:
    row = frappe.db.get_value(
        "FAC Go-Live",
        {"company": company},
        ["marked_live", "marked_live_by", "marked_live_on"],
        as_dict=True,
    )
    if not row or not row.marked_live:
        return None
    return {"by": row.marked_live_by, "on": row.marked_live_on}


def _snoozed(company: str, user: str) -> bool:
    parent = frappe.db.get_value("FAC Go-Live", {"company": company})
    if not parent:
        return False
    return bool(
        frappe.db.exists(
            "FAC Go-Live Snooze",
            {"parent": parent, "parenttype": "FAC Go-Live", "user": user, "until": (">", frappe.utils.now_datetime())},
        )
    )


def maybe_enable_tools(state: Dict[str, Any], user: str) -> bool:
    """Turn the onboarding tools on when nobody has said no (6.5): an administrator who switched the
    Migration plugin off in FAC Admin is recorded on FAC Go-Live, and then FAC asks instead."""
    if state["plugin_on"] or state["tools_disabled_by"]:
        return False
    # logged first: enable_plugin commits, and a GET request rolls back whatever comes after it
    frappe.get_doc(
        {
            "doctype": "Assistant Audit Log",
            "action": "enable plugin",
            "tool_name": "enable_plugin",
            "user": user,
            "status": "Success",
            "timestamp": frappe.utils.now(),
            "source_app": "go-live entry check",
            "target_name": MIGRATION_PLUGIN,
        }
    ).insert(ignore_permissions=True)
    get_plugin_manager().enable_plugin(MIGRATION_PLUGIN)
    clear_cache()
    return True


def _text(variant: str, current: str, company: str, state: Dict[str, Any], can_enable: bool):
    """The banner line, its primary button and the other buttons, as in section 5.1."""
    if variant == "enable_tools":
        if can_enable:
            return (
                _("FAC can help take {0} live, but its onboarding tools are off.").format(company),
                _("Turn on onboarding tools"),
                ["not_now", "already_live"],
            )
        return (
            _("FAC can help take {0} live. Ask an administrator to turn on its onboarding tools.").format(company),
            None,
            ["not_now"],
        )
    if variant == "hypercare":
        care = state["hypercare"] or {}
        return (
            _("Hypercare for {0}: week {1} of {2}.").format(company, care.get("week"), care.get("total")),
            _("Open hypercare summary"),
            ["not_now"],
        )
    others = ["not_now", "already_live"]
    if current == "not_started":
        return _("{0} isn't live yet. Want FACO to help take it live?").format(company), _("Start"), others
    if current == "not_started_soft":
        return _("Is {0} a new implementation? FACO can help take it live.").format(company), _("Start"), others
    if current == "ready_to_go_live":
        return (
            _("{0} passed its go-live test run. Switch on hypercare after go-live.").format(company),
            _("Continue"),
            others,
        )
    if current == "in_progress":
        done = sum(1 for s in state["steps"] if s["state"] in ("done", "skipped"))
        text = _("{0} isn't live yet: {1} of 6 go-live steps done.").format(company, done)
        if state["next_step"]:
            text += " " + _("Next: {0}.").format(STEP_LABELS[state["next_step"]])
        return text, _("Continue"), others
    return None, None, []


GO_LIVE_PROMPT = "go-live"


def step_instruction(step: Optional[str], company: str) -> str:
    """What the AI is asked to do for a go-live step: the tool to start with, so it begins where the
    checklist says and not wherever it guesses."""
    instructions = {
        "discovery": _(
            "Start with discovery: call setup_plan with action discover for {0} and ask me its questions."
        ),
        "setup_plan": _(
            "Continue with the setup plan of {0}: show me what it will set up, and run it with "
            "run_setup_plan once I approve it."
        ),
        "chart_of_accounts": _(
            "Continue with the chart of accounts of {0}: check the client's chart with dry_run_import, "
            "then put it on with import_chart_of_accounts, or keep ERPNext's chart if I say so."
        ),
        "data_migration": _(
            "Continue with the data migration of {0}: plan the client's files with plan_migration, and "
            "run them with run_migration_plan once I approve it."
        ),
        "go_live_test_run": _(
            "Continue with the go-live test run of {0}: run go_live_test_run and go through the Go-Live "
            "Readiness report with me."
        ),
        "hypercare": _(
            "Continue with hypercare for {0}: after go-live, switch it on with a FAC Hypercare record for "
            "the consultant."
        ),
    }
    return instructions.get(step or "discovery", instructions["discovery"]).format(company)


def _prompt(company: str, step: Optional[str]) -> str:
    """The go-live prompt template, filled in for the company and the step: the same text an AI client
    gets from the go-live prompt. A missing or broken template falls back to the instruction alone."""
    instruction = step_instruction(step, company)
    try:
        content = frappe.db.get_value(
            "Prompt Template", {"prompt_id": GO_LIVE_PROMPT, "status": "Published"}, "template_content"
        )
        if content:
            text = frappe.render_template(content, {"company": company, "step": instruction}).strip()
            if text:
                return text
    except Exception:
        frappe.log_error(title=ERROR_TITLE, message=frappe.get_traceback())
    return _("Take {0} live.").format(company) + " " + instruction


def _with_prompts(step_list: List[Dict[str, Any]], company: str) -> List[Dict[str, Any]]:
    """The checklist's steps, each one not yet done with the prompt that starts it: steps don't have
    to be done in order. Copies, as the list is the cached site state."""
    return [
        {**step, "prompt": None if step["state"] in ("done", "skipped") else _prompt(company, step["key"])}
        for step in step_list
    ]


def _continue(company: str, state: Dict[str, Any]) -> Dict[str, Any]:
    """Where Start/Continue goes (5.2): FAC Chat whenever it is on, else the user's own AI client.

    FAC Chat that is on but not ready (not connected to FAC Cloud, waitlisted, user not set up)
    shows its own screen for that, so the dialog is only for a site with FAC Chat off.
    """
    from frappe_assistant_core.chat.gate import is_chat_enabled

    return {
        "mode": "fac_chat" if is_chat_enabled() else "external",
        "prompt": _prompt(company, state["next_step"]),
    }


def _mcp_endpoint() -> str:
    # as FAC Admin shows it
    return frappe.db.get_single_value("Assistant Core Settings", "mcp_endpoint_url") or frappe.utils.get_url(
        "/api/method/frappe_assistant_core.api.fac_endpoint.handle_mcp"
    )


def mcp_instructions(user: str) -> Optional[str]:
    """The paragraph an MCP client gets in the initialize result while the company isn't live yet,
    so Claude or ChatGPT offers what the banner offers. None otherwise."""
    state = get_state(user, may_write=False)
    if not state.get("show") or state["stage"] not in ("not_started", "in_progress", "ready_to_go_live"):
        return None
    step = STEP_LABELS.get(state["next_step"]) if state["next_step"] else None
    text = _("This site's company {0} isn't live yet (go-live stage: {1}).").format(
        state["company"], state["stage"].replace("_", " ")
    )
    if step:
        text += " " + _("Next go-live step: {0}.").format(step)
    text += " " + _(
        "Offer to help take it live. The go-live prompt continues where it stopped: setup_plan with "
        "action discover first when the company has no setup plan."
    )
    return text


def get_state(
    user: str, view: str = "banner", company: Optional[str] = None, may_write: bool = True
) -> Dict[str, Any]:
    """What the Desk banner (view "banner") or the checklist (view "checklist") shows this user.

    Checks run in order and the first that fails hides everything: S1-S4, U1-U3, then for the banner
    S5 (marked live) and U4 (snoozed), then the stage. S6 and U5 only change the variant of a shown
    stage. The checklist, on the Go-Live Readiness report and FAC Admin, ignores S5, U4 and the stage,
    so "Already live" can be undone and an established site can still start. Never raises: any error
    hides the banner and is logged.

    may_write False only reads: the FAC Chat banner and the MCP server ask too, and only the go-live
    endpoint switches the tools on (two requests doing it at once deadlock).
    """
    try:
        return _get_state(user, view, company, may_write)
    except Exception:
        frappe.log_error(title=ERROR_TITLE, message=frappe.get_traceback())
        return dict(HIDDEN)


def _get_state(user: str, view: str, company: Optional[str], may_write: bool = True) -> Dict[str, Any]:
    if _site_hidden() or _user_hidden(user):
        return dict(HIDDEN)
    banner = view != "checklist"
    if not company or company not in _companies():
        company = pick_company(user)
    if not company:
        return dict(HIDDEN)
    marked_live = _marked_live(company)
    if banner and (marked_live or _snoozed(company, user)):
        return dict(HIDDEN)

    state = site_state(company)
    current = state["stage"]
    shown = current in SHOWN_STAGES
    if banner and not shown:
        return dict(HIDDEN)

    can_enable = bool(frappe.has_permission("Assistant Core Settings", "write", user=user))
    variant = None
    if shown:
        variant = "hypercare" if current == "live_hypercare" else "checklist"
        # the hypercare summary needs no onboarding tools; a company marked live never has them
        # switched on for it (the checklist view still shows one)
        if (
            variant == "checklist"
            and not state["plugin_on"]
            and (marked_live or not may_write or not maybe_enable_tools(state, user))
        ):
            variant = "enable_tools"
    text, primary, buttons = _text(variant, current, company, state, can_enable)

    return {
        "show": True,
        "variant": variant,
        "stage": "not_started" if current == "not_started_soft" else current,
        "soft": current == "not_started_soft",
        "company": company,
        "companies": frappe.get_all("FAC Go-Live", pluck="company", order_by="creation asc"),
        "steps": state["steps"] if banner else _with_prompts(state["steps"], company),
        "next_step": state["next_step"],
        "hypercare": state["hypercare"],
        "marked_live": marked_live,
        "text": text,
        "primary": primary,
        "buttons": buttons,
        # the checklist of an established site can still start (R7)
        "continue": _continue(company, state) if variant != "hypercare" else None,
        "can_enable_tools": can_enable,
        "can_enable_chat": can_enable,
        "mcp_endpoint": _mcp_endpoint(),
    }
