# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Hypercare (#34426): for the first weeks after go-live, a weekly summary of what is going wrong on the
site, so the consultant finds problems before the client calls. A FAC Hypercare record switches it on
for a consultant, a start date and a length; a daily job sends each week's summary and ends the
period on its own.

The summary is counts and grouped messages only: error titles, failure messages, DocType names and
user ids, never a document's data. It is also the Script Report "Hypercare Summary", which FACO runs
with generate_report when the consultant opens it in FAC Chat.
"""

import re
from collections import defaultdict
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import add_days, cint, escape_html, get_datetime, get_url, getdate, md_to_html, nowdate

from frappe_assistant_core.utils.go_live_check import NUMBERED

WEEK = 7
#: A draft untouched this many days is one the user forgot, not one they are still working on
DRAFT_DAYS = 3
STUCK_DAYS = 3
SHOWN = 10
#: Rows read per source; a week with more than this is reported as "at least"
LIMIT = 5000
#: Training points need the same failure from this many users
TRAINING_USERS = 2

ERRORS, FAILURES, REFUSALS = "errors", "failures", "refusals"
DRAFTS, APPROVALS, LOGINS = "drafts", "approvals", "logins"
JOBS, EMAILS = "jobs", "emails"


def label(area: str) -> str:
    """An area's name in the user's language: translated per call, as a site may have users of several."""
    return {
        ERRORS: _("Errors"),
        FAILURES: _("Failures users hit"),
        REFUSALS: _("Permission refusals"),
        DRAFTS: _("Drafts never submitted"),
        APPROVALS: _("Approvals stuck"),
        LOGINS: _("Users not logging in"),
        JOBS: _("Failed scheduled jobs"),
        EMAILS: _("Failed emails"),
    }[area]


def _suggestions() -> tuple:
    """(pattern in the message, what fixes it). The first match wins."""
    return (
        (r"fiscal year", _("Create the Fiscal Year that covers these dates.")),
        (r"price list", _("Training point: show users which price list to pick, or set it on the customer.")),
        (
            r"needed in warehouse|insufficient stock|negative stock",
            _("Training point: receive the stock before delivering it, and check the warehouse picked."),
        ),
        (r"exchange rate", _("Add the missing Currency Exchange rate.")),
        (
            r"account.*(missing|not set|mandatory|required)|default .*account",
            _("Set the company's default accounts (the Go-Live Readiness report lists them)."),
        ),
        (r"cost center", _("Set a default cost center on the company.")),
        (r"tax", _("Check the tax template and tax category on the party or item.")),
        (r"email|smtp", _("Check the outgoing Email Account.")),
        (
            r"not permitted|permission|insufficient permission",
            _("Check the user's roles: ask FACO what they can do."),
        ),
        (r"mandatory|required|missing", _("Training point: show users the fields this form needs.")),
    )


def _fix(message: str, default: str) -> str:
    text = (message or "").lower()
    return next((fix for pattern, fix in _suggestions() if re.search(pattern, text)), default)


def _plain(message: str) -> str:
    """One line of a message, with record names and numbers taken out so the same failure on
    different records groups together, and no document data reaches the summary."""
    line = next((part.strip() for part in (message or "").splitlines() if part.strip()), "")
    line = re.sub(r"<[^>]+>", "", line)
    line = re.sub(r"[\"'“‘][^\"'”’]{1,140}[\"'”’]", "…", line)
    line = re.sub(r"\b[A-Z]{2,}[-/][\w./-]+", "…", line)
    line = re.sub(r"\d[\d,.:/-]*", "#", line)
    return line[:160] or _("(no message)")


def _row(area: str, item: str, count: int, fix: str, users: int = 0) -> Dict[str, Any]:
    return {"area": label(area), "item": item, "count": count, "users": users or None, "fix": fix}


def _between(from_date, to_date) -> List:
    return ["between", [get_datetime(from_date), get_datetime(f"{to_date} 23:59:59")]]


def _grouped(rows: List[Dict], key, user_key: Optional[str] = None) -> List[tuple]:
    """(key, count, distinct users) for each group, the biggest first."""
    counts, users = defaultdict(int), defaultdict(set)
    for row in rows:
        group = key(row)
        counts[group] += 1
        if user_key and row.get(user_key):
            users[group].add(row[user_key])
    return sorted(((g, counts[g], len(users[g])) for g in counts), key=lambda g: (-g[1], g[0]))


# -- each area ------------------------------------------------------------------------------


def errors(from_date, to_date) -> List[Dict[str, Any]]:
    """Error Log in the period grouped by title: the same error over and over is one row."""
    rows = frappe.get_all(
        "Error Log", filters={"creation": _between(from_date, to_date)}, fields=["method"], limit=LIMIT
    )
    default = _("Open the Error Log for the full trace, or ask FACO about recent errors.")
    return [
        _row(ERRORS, title, count, _fix(title, default))
        for title, count, _users in _grouped(rows, lambda r: _plain(r.method))[:SHOWN]
    ]


def failures(from_date, to_date) -> List[Dict[str, Any]]:
    """What users tried through FAC and was refused, grouped by message. The same failure from several
    users is a training point. (A form saved in Desk that fails validation is not logged anywhere, so
    only failures through FAC are counted.)"""
    rows = frappe.get_all(
        "Assistant Audit Log",
        filters={"timestamp": _between(from_date, to_date), "status": "Error"},
        fields=["error_message", "user"],
        limit=LIMIT,
    )
    out = []
    for message, count, users in _grouped(rows, lambda r: _plain(r.error_message), "user")[:SHOWN]:
        fix = _fix(message, _("Open the Assistant Audit Log for these failures."))
        if users >= TRAINING_USERS:
            fix = _("{0} users hit this. {1}").format(users, fix)
        out.append(_row(FAILURES, message, count, fix, users))
    return out


def refusals(from_date, to_date) -> List[Dict[str, Any]]:
    rows = frappe.get_all(
        "Assistant Audit Log",
        filters={"timestamp": _between(from_date, to_date), "status": "Permission Denied"},
        fields=["target_doctype", "tool_name", "user"],
        limit=LIMIT,
    )
    fix = _("Check the role or User Permission they need: ask FACO what the user can do.")
    return [
        _row(REFUSALS, doctype, count, fix, users)
        for doctype, count, users in _grouped(rows, lambda r: r.target_doctype or r.tool_name or "-", "user")[
            :SHOWN
        ]
    ]


def drafts(from_date, to_date) -> List[Dict[str, Any]]:
    """Day-one documents made in the period and left as drafts for more than a few days."""
    untouched = get_datetime(add_days(nowdate(), -DRAFT_DAYS))
    out = []
    for doctype in NUMBERED:
        if not frappe.db.exists("DocType", doctype):
            continue
        owners = frappe.get_all(
            doctype,
            filters={"docstatus": 0, "creation": _between(from_date, to_date), "modified": ["<", untouched]},
            pluck="owner",
            limit=LIMIT,
        )
        if owners:
            fix = _("Ask the people who made them to submit or delete them.")
            out.append(_row(DRAFTS, _(doctype), len(owners), fix, len(set(owners))))
    return sorted(out, key=lambda r: -r["count"])


def approvals(stuck_after_days: int) -> List[Dict[str, Any]]:
    """Workflow approvals waiting longer than the hypercare's limit, by document type and state."""
    rows = frappe.get_all(
        "Workflow Action",
        filters={"status": "Open", "creation": ["<", get_datetime(add_days(nowdate(), -stuck_after_days))]},
        fields=["reference_doctype", "workflow_state"],
        limit=LIMIT,
    )
    return [
        _row(
            APPROVALS,
            _("{0} waiting in {1}").format(_(doctype), state),
            count,
            _("Waiting more than {0} days: remind whoever approves {1}.").format(stuck_after_days, state),
        )
        for (doctype, state), count, _users in _grouped(
            rows, lambda r: (r.reference_doctype or "", r.workflow_state or "")
        )[:SHOWN]
    ]


def logins(from_date, to_date) -> List[Dict[str, Any]]:
    """Desk users made before the period's end who haven't logged in during it."""
    users = frappe.get_all(
        "User",
        filters={
            "enabled": 1,
            "user_type": "System User",
            "name": ["not in", ["Administrator", "Guest"]],
            "creation": ["<", get_datetime(f"{to_date} 23:59:59")],
        },
        fields=["name", "last_login"],
    )
    since = get_datetime(from_date)
    idle = sorted(u.name for u in users if not u.last_login or get_datetime(u.last_login) < since)
    if not idle:
        return []
    shown = ", ".join(idle[:SHOWN]) + (
        " " + _("and {0} more").format(len(idle) - SHOWN) if len(idle) > SHOWN else ""
    )
    return [
        _row(
            LOGINS,
            shown,
            len(idle),
            _("Ask whether they need help getting started, or disable the users who left."),
            len(idle),
        )
    ]


def jobs(from_date, to_date) -> List[Dict[str, Any]]:
    rows = frappe.get_all(
        "Scheduled Job Log",
        filters={"creation": _between(from_date, to_date), "status": "Failed"},
        fields=["scheduled_job_type"],
        limit=LIMIT,
    )
    fix = _("Open the Scheduled Job Log for this job; the Error Log has the cause.")
    return [
        _row(JOBS, job, count, fix)
        for job, count, _users in _grouped(rows, lambda r: r.scheduled_job_type)[:SHOWN]
    ]


def emails(from_date, to_date) -> List[Dict[str, Any]]:
    count = frappe.db.count("Email Queue", {"creation": _between(from_date, to_date), "status": "Error"})
    if not count:
        return []
    fix = _("Check the outgoing Email Account, then retry them from the Email Queue.")
    return [_row(EMAILS, _("Emails that could not be sent"), count, fix)]


def summary(from_date, to_date, stuck_after_days: int = STUCK_DAYS) -> List[Dict[str, Any]]:
    """Everything the week turned up, area by area."""
    return (
        errors(from_date, to_date)
        + failures(from_date, to_date)
        + refusals(from_date, to_date)
        + drafts(from_date, to_date)
        + approvals(stuck_after_days)
        + logins(from_date, to_date)
        + jobs(from_date, to_date)
        + emails(from_date, to_date)
    )


# -- the report -----------------------------------------------------------------------------


def check_access(user: Optional[str] = None) -> None:
    """The summary is for System Managers; a hypercare's consultant is one (FAC Hypercare checks it)."""
    if "System Manager" not in frappe.get_roles(user or frappe.session.user):
        frappe.throw(_("The hypercare summary is for System Managers."), frappe.PermissionError)


def _stuck_days() -> int:
    days = frappe.db.get_value(
        "FAC Hypercare", {"status": "Active"}, "stuck_after_days", order_by="start_date desc"
    )
    return cint(days) or STUCK_DAYS


def headline(rows: List[Dict[str, Any]], from_date, to_date) -> str:
    if not rows:
        return _("Nothing to look at between {0} and {1}.").format(from_date, to_date)
    areas = len({r["area"] for r in rows})
    return _("{0} things to look at in {1} areas between {2} and {3}.").format(
        len(rows), areas, from_date, to_date
    )


def _cell(value) -> str:
    return str(value if value is not None else "").replace("|", "/").replace("\n", " ")


def document(rows: List[Dict[str, Any]], from_date, to_date) -> str:
    """The summary as markdown, for the email and for generate_document."""
    lines = [f"# {_('Hypercare summary')}", "", headline(rows, from_date, to_date), ""]
    if rows:
        lines += [
            f"| {_('Area')} | {_('What')} | {_('How many')} | {_('Users')} | {_('Suggested fix')} |",
            "|---|---|---|---|---|",
        ]
        lines += [
            "| " + " | ".join(_cell(r[k]) for k in ("area", "item", "count", "users", "fix")) + " |"
            for r in rows
        ]
    return "\n".join(lines)


def columns() -> List[Dict[str, Any]]:
    return [
        {"fieldname": "area", "label": _("Area"), "fieldtype": "Data", "width": 180},
        {"fieldname": "item", "label": _("What"), "fieldtype": "Data", "width": 380},
        {"fieldname": "count", "label": _("How Many"), "fieldtype": "Int", "width": 90},
        {"fieldname": "users", "label": _("Users"), "fieldtype": "Int", "width": 70},
        {"fieldname": "fix", "label": _("Suggested Fix"), "fieldtype": "Data", "width": 420},
    ]


def execute(filters: Optional[Dict[str, Any]] = None):
    filters = frappe._dict(filters or {})
    check_access()
    to_date = getdate(filters.to_date or nowdate())
    from_date = getdate(filters.from_date or add_days(to_date, -(WEEK - 1)))
    if from_date > to_date:
        frappe.throw(_("From Date must be on or before To Date."))
    rows = summary(from_date, to_date, cint(filters.stuck_after_days) or _stuck_days())
    message = (
        document(rows, from_date, to_date) if cint(filters.document) else headline(rows, from_date, to_date)
    )
    shown = [{**r, "item": escape_html(r["item"]), "fix": escape_html(r["fix"])} for r in rows]
    return columns(), shown, message


# -- the weekly job -------------------------------------------------------------------------


def _links(from_date, to_date) -> str:
    report = get_url(f"/app/query-report/Hypercare%20Summary?from_date={from_date}&to_date={to_date}")
    prompt = _("Open the hypercare summary for {0} to {1} and help me act on it.").format(from_date, to_date)
    chat = get_url(f"/copilot/chat?prompt={quote(prompt)}")
    return f"[{_('Open the report')}]({report}) · [{_('Act on it in FAC Chat')}]({chat})"


EMAIL_SAVEPOINT = "fac_hypercare_email"


def send(hypercare, from_date, to_date) -> None:
    """The summary of one period to the hypercare's consultant: an email and a notification."""
    rows = summary(from_date, to_date, cint(hypercare.stuck_after_days) or STUCK_DAYS)
    subject = _("Hypercare summary {0} to {1}: {2}").format(
        from_date,
        to_date,
        _("nothing to look at") if not rows else _("{0} things to look at").format(len(rows)),
    )
    body = document(rows, from_date, to_date) + "\n\n" + _links(from_date, to_date)
    # A site without an outgoing email account refuses the email: the bell, and the summary being
    # done, must not go with it, or the same summary failed every day and the hypercare never ended
    frappe.db.savepoint(EMAIL_SAVEPOINT)
    try:
        frappe.sendmail(
            recipients=[hypercare.consultant],
            subject=subject,
            message=md_to_html(body),
            reference_doctype=hypercare.doctype,
            reference_name=hypercare.name,
        )
    except Exception:
        frappe.db.rollback(save_point=EMAIL_SAVEPOINT)
        frappe.log_error(
            title=_("Hypercare summary email not sent for {0}").format(hypercare.name),
            message=frappe.get_traceback(),
        )
    # An Alert shows in the bell without a second email
    frappe.get_doc(
        {
            "doctype": "Notification Log",
            "for_user": hypercare.consultant,
            "type": "Alert",
            "subject": subject,
            "email_content": headline(rows, from_date, to_date),
            "document_type": hypercare.doctype,
            "document_name": hypercare.name,
        }
    ).insert(ignore_permissions=True)
    hypercare.db_set("last_summary_on", to_date)


def send_due_summaries() -> None:
    """Daily: a summary for every hypercare with a week behind it, and end the ones whose period is
    over, after the summary of their last days."""
    if not frappe.db.table_exists("FAC Hypercare"):
        return  # the site isn't migrated yet: nothing can be switched on
    today = getdate(nowdate())
    for name in frappe.get_all("FAC Hypercare", filters={"status": "Active"}, pluck="name"):
        try:
            hypercare = frappe.get_doc("FAC Hypercare", name)
            end = getdate(hypercare.end_date)
            since = getdate(hypercare.last_summary_on or add_days(hypercare.start_date, -1))
            last_day = min(add_days(today, -1), end)
            if today > end or (getdate(last_day) - since).days >= WEEK:
                if getdate(last_day) > since:
                    send(hypercare, add_days(since, 1), last_day)
            if today > end:
                hypercare.db_set("status", "Ended")
                # db_set runs no doc_events: the go-live banner's cache is cleared here (#34419)
                from frappe_assistant_core.onboarding.entry import clear_cache

                clear_cache()
            frappe.db.commit()  # nosemgrep: frappe-manual-commit — a scheduled job commits its own work
        except Exception:
            frappe.db.rollback()
            frappe.log_error(
                title=_("Hypercare summary failed for {0}").format(name), message=frappe.get_traceback()
            )
