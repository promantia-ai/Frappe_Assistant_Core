# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Plan the migration order across several files (#33995).

A user with several exports (customers, items, invoices, balances) needs them imported in an
order where every record a file links to is in before it. Each file's session joins one plan: a
FAC Import Session with no file of its own (file_source "Migration Plan"), which the files'
sessions point to through parent_session. The plan is worked out each time it is read, from the
sessions as they stand, so it never goes stale and coming back to it shows where each step is.

What a file needs comes from the Link fields its columns are mapped to (for the opening imports,
the Link fields of the rows they create), not from a written list; what isn't in yet, from its
last dry run's "Missing link" groups. A file goes after the files that bring what it links to;
otherwise masters come first, then transactions, then the opening figures.
"""

from typing import Any, Dict, List, Optional, Set

import frappe
from frappe import _
from frappe.core.doctype.data_import.importer import get_autoname_field
from frappe.utils import cstr

from frappe_assistant_core.plugins.core import import_dry_run as dry_run
from frappe_assistant_core.plugins.core.import_session import (
    FileProblem,
    read_sheet_rows,
    readable_session_file,
)

PLAN_SOURCE = "Migration Plan"
SESSION_DOCTYPE = dry_run.SESSION_DOCTYPE
MISSING_VALUES_SHOWN = 5

#: The rows each opening import creates, whose Link fields say what it needs.
ENGINE_ROWS = {
    "opening_stock": "Stock Reconciliation Item",
    "opening_invoices": "Opening Invoice Creation Tool Item",
    "opening_balances": "Journal Entry Account",
}
#: The order of kinds when nothing links them: accounts, masters, transactions, opening figures,
#: the trial balance last (it clears what the other opening imports put on Temporary Opening).
RANK = {"accounts": 0, "master": 1, "transaction": 2, "opening": 3, "trial balance": 4}

STATE = {
    "Completed": "done",
    "Importing": "running",
    "Undoing": "running",
    "Partially Completed": "needs attention",
    "Failed": "needs attention",
    "Undone": "undone",
    "Partially Undone": "undone",
}


class PlanProblem(Exception):
    """The plan can't be made or joined. The message is shown to the user."""


def is_plan(session) -> bool:
    return session.file_source == PLAN_SOURCE


def join(plan_with: str) -> str:
    """The plan of ``plan_with`` (a session of this user's, or a plan), made when it has none.
    Returns its name; the caller puts the new session's parent_session to it."""
    from frappe_assistant_core.plugins.core.import_mapping import load_session

    try:
        other = load_session(plan_with)
    except (frappe.DoesNotExistError, frappe.PermissionError):
        frappe.clear_last_message()
        raise PlanProblem(_("Import session {0} not found, or it isn't yours.").format(plan_with))
    if is_plan(other):
        return other.name
    # Read again under a row lock: an AI client opens several files at the same moment, each
    # "with" this one, and each would make its own plan. The lock makes the others wait until the
    # first has made the plan and linked it here, then they read that link and join it.
    parent = frappe.db.get_value(SESSION_DOCTYPE, other.name, "parent_session", for_update=True)
    if parent:
        return parent
    plan = frappe.get_doc(
        {
            "doctype": SESSION_DOCTYPE,
            "user": frappe.session.user,
            "chat_session_id": getattr(frappe.local, "ar_session_id", None),
            "file_name": _("Migration plan"),
            "file_source": PLAN_SOURCE,
        }
    )
    plan.log_step(
        "Plan", "Success", _("Planned {0} with the next file.").format(other.file_name or other.name)
    )
    plan.insert()
    # The link alone, as it is: the session may be in the middle of its import.
    frappe.db.set_value(SESSION_DOCTYPE, other.name, "parent_session", plan.name, update_modified=False)
    return plan.name


def refused(session) -> Optional[str]:
    """What a plan can't do: it has no file to map, check or import."""
    if is_plan(session):
        return _(
            "{0} is a migration plan, not a file: call dry_run_import on it to see the order, and "
            "map, check and import each file's own session."
        ).format(session.name)
    return None


# -- the plan --------------------------------------------------------------------------------


def build(plan) -> Dict[str, Any]:
    """The plan of ``plan``: its files in the order to import them, each with what it needs,
    why it goes where it does, what isn't in yet and where it stands; and what to do next."""
    sessions = frappe.get_list(
        SESSION_DOCTYPE,
        filters={"parent_session": plan.name, "status": ["!=", "Cancelled"]},
        pluck="name",
        order_by="creation asc",
    )
    steps = [_step(frappe.get_doc(SESSION_DOCTYPE, name)) for name in sessions]
    brings: Dict[str, List[int]] = {}
    for i, step in enumerate(steps):
        if step["doctype"]:
            brings.setdefault(step["doctype"], []).append(i)
    waits: Dict[int, Set[int]] = {i: set() for i in range(len(steps))}
    for i, step in enumerate(steps):
        for doctype in step["needs"]:
            waits[i] |= {j for j in brings.get(doctype, []) if j != i}

    order = _order(steps, waits)
    number = {i: n for n, i in enumerate(order, start=1)}
    out = []
    for i in order:
        step = steps[i]
        after = sorted(number[j] for j in waits[i])
        needed_by = sorted(number[j] for j, w in waits.items() if i in w)
        missing = [_brought(m, i, brings, number, steps) for m in step["missing"]]
        out.append(
            {
                "step": number[i],
                "session": step["session"],
                "file": step["file"],
                "imports": step["label"],
                "kind": step["kind"],
                "state": step["state"],
                "detail": step["detail"],
                "after": after,
                "reason": _reason(step, after, needed_by, steps, order, waits, i),
                "missing": missing,
            }
        )

    done = sum(1 for s in out if s["state"] in ("done", "undone"))
    nxt = _next(out)
    result = {
        "plan_session": plan.name,
        "files": len(out),
        "done": done,
        "pending": len(out) - done,
        "steps": out,
        "next": nxt,
    }
    if not out:
        result["message"] = _("No file has joined this plan yet.")
    elif nxt:
        result["message"] = _("{0} files: {1} done, {2} to go. Next: step {3}, {4}: {5}").format(
            len(out), done, len(out) - done, nxt["step"], nxt["file"], nxt["do"]
        )
    else:
        result["message"] = _("{0} files: {1} done. Every file of the plan is in.").format(len(out), done)
    return result


def _brought(missing: Dict[str, Any], i: int, brings, number, steps) -> Dict[str, Any]:
    """``missing`` with the step whose file brings those records (in_step), and the ones no
    file of the plan brings (not_in_files): those must be created, or their file added."""
    in_step, left = None, list(missing["values"])
    for j in brings.get(missing["doctype"], []):
        if j == i:
            continue
        names = _names_in_file(steps[j])
        if any(value in names for value in missing["values"]):
            in_step = in_step or number[j]
            left = [value for value in left if value not in names]
    return {**missing, "in_step": in_step, "not_in_files": left}


def _names_in_file(step: Dict[str, Any]) -> Set[str]:
    """The names of the records a file brings: its column mapped to the field that names them
    (the autoname field, else the title field). Read once per plan."""
    if "names" not in step:
        step["names"] = set()
        session = frappe.get_doc(SESSION_DOCTYPE, step["session"])
        doctype = session.target_doctype
        if doctype and not _engine_key(session):
            autoname = get_autoname_field(doctype)
            wanted = [autoname.fieldname if autoname else None, frappe.get_meta(doctype).title_field, "name"]
            mapped = {
                field: int(pos)
                for pos, field in dry_run.column_to_field_map(session).items()
                if str(pos).isdigit()
            }
            position = next((mapped[f] for f in wanted if f and f in mapped), None)
            if position is not None:
                try:
                    rows = read_sheet_rows(readable_session_file(session), session.sheet or None)
                except FileProblem:
                    rows = []
                step["names"] = {cstr(row[position]).strip() for row in rows[1:] if position < len(row)} - {
                    ""
                }
    return step["names"]


def _step(session) -> Dict[str, Any]:
    engine = _engine_key(session)
    doctype, label, kind = _what(session, engine)
    state, detail = _state(session)
    return {
        "session": session.name,
        "file": session.file_name or session.name,
        "doctype": doctype,
        "label": label,
        "kind": kind,
        "state": state,
        "detail": detail,
        "status": session.status,
        "needs": _needs(session, engine, doctype),
        "missing": _missing(session, engine),
    }


def _engine_key(session) -> Optional[str]:
    options = frappe.parse_json(session.template_options or "{}") or {}
    return next((key for key in ENGINE_ROWS if options.get(key)), None)


def _what(session, engine):
    """The DocType a session brings in, how to name it, and its kind."""
    if engine == "opening_stock":
        return "Stock Reconciliation", _("Opening Stock"), "opening"
    if engine == "opening_invoices":
        party = _settings(session, engine).get("party_type") or "Customer"
        invoice = "Sales Invoice" if party == "Customer" else "Purchase Invoice"
        return invoice, _("Opening {0}").format(_(invoice)), "opening"
    if engine == "opening_balances":
        return "Journal Entry", _("Opening Balances"), "trial balance"
    doctype = session.target_doctype
    if not doctype:
        return None, _("Not chosen yet"), "master"
    if doctype == "Account":
        kind = "accounts"
    elif frappe.get_meta(doctype).is_submittable:
        kind = "transaction"
    else:
        kind = "master"
    return doctype, _(doctype), kind


def _settings(session, engine) -> Dict[str, Any]:
    options = frappe.parse_json(session.template_options or "{}") or {}
    return dict(options.get(engine) or {})


def _needs(session, engine, doctype) -> List[str]:
    """The DocTypes the file's mapped columns link to: the records it needs in first."""
    fields = []  # (DocType the field is on, fieldname)
    if engine:
        row = ENGINE_ROWS[engine]
        mapped = set(_settings(session, engine).get("columns") or {})
        # The engines keep their mapped columns in either place: opening stock as "items.item_code",
        # opening invoices the party as "customer" or "supplier" in the session's own column map.
        mapped |= {
            cstr(target).split(".", 1)[-1]
            for target in dry_run.column_to_field_map(session).values()
            if target and target != "Don't Import"
        }
        party = _settings(session, engine).get("party_type")
        for field in mapped:
            if engine == "opening_invoices" and field in ("party", "customer", "supplier"):
                if party:
                    fields.append((None, party))
                continue
            if engine == "opening_stock" and field == "uom":
                fields.append(("Item", "stock_uom"))
                continue
            fields.append((row, field))
    elif doctype:
        from frappe_assistant_core.plugins.core.import_fixes import _column_targets

        fields = list(_column_targets(session).values())

    needs = []
    for parent, field in fields:
        if parent is None:  # a party type, already a DocType
            target = field
        else:
            df = frappe.get_meta(parent).get_field(field)
            target = df.options if df and df.fieldtype == "Link" else None
        if target and target != doctype and target not in needs:
            needs.append(target)
    return needs


def _missing(session, engine) -> List[Dict[str, Any]]:
    """What the last check found missing, by DocType: the records it links to that aren't in."""
    from frappe_assistant_core.plugins.core import import_fixes

    summary = frappe.parse_json(session.validation_summary or "{}") or {}
    by_doctype: Dict[str, Dict[str, Any]] = {}
    for group in summary.get("groups") or []:
        if group.get("cause") != dry_run.CAUSE_MISSING_LINK or not group.get("field"):
            continue
        doctype = group.get("link_doctype")
        if not doctype and engine:
            df = frappe.get_meta(ENGINE_ROWS[engine]).get_field(group["field"])
            doctype = df.options if df and df.fieldtype == "Link" else None
        if not doctype and not engine:
            try:
                doctype = import_fixes._with_target(session, group).get("link_doctype")
            except Exception:
                doctype = None
        if not doctype:
            continue
        held = by_doctype.setdefault(doctype, {"doctype": doctype, "count": 0, "values": []})
        held["count"] += int(group.get("count") or 0)
        value = cstr(group.get("value"))
        if value and value not in held["values"] and len(held["values"]) < MISSING_VALUES_SHOWN:
            held["values"].append(value)
    return list(by_doctype.values())


def _state(session):
    """Where a file stands: done, running, needs attention, undone or pending, and in words."""
    if dry_run.is_running(session):
        return "running", _("Being checked.")
    state = STATE.get(session.status, "pending")
    detail = {
        "File Read": _("Read. Map its columns next."),
        "Mapped": _("Mapped. Check it next."),
        "Dry Run Failed": _("Checked: some rows need fixing."),
        "Ready to Import": _("Checked and ready to import."),
        "Importing": _("Importing."),
        "Undoing": _("Being undone."),
        "Completed": _("Imported."),
        "Partially Completed": _("Imported, but some rows failed."),
        "Failed": _("The import failed."),
        "Undone": _("Imported, then undone."),
        "Partially Undone": _("Imported, then partly undone."),
    }.get(session.status, _(session.status or ""))
    return state, detail


# -- the order and the words -----------------------------------------------------------------


def _order(steps: List[Dict[str, Any]], waits: Dict[int, Set[int]]) -> List[int]:
    """Every file after the files it waits for; among the files free to go, by kind, then in
    the order they came. Files that wait for each other go by kind, then in the order they came."""
    left = set(range(len(steps)))
    order: List[int] = []
    while left:
        free = [i for i in left if not (waits[i] & left)] or list(left)  # a cycle: by kind
        nxt = min(free, key=lambda i: (RANK[steps[i]["kind"]], i))
        order.append(nxt)
        left.remove(nxt)
    return order


def _reason(step, after, needed_by, steps, order, waits, i) -> str:
    if after:
        brought = sorted({steps[j]["label"] for j in waits[i]})
        return _("After step {0}: its columns link to {1}, which that file brings in.").format(
            ", ".join(str(n) for n in after), ", ".join(brought)
        )
    if needed_by:
        return _("{0} first: step {1} links to it.").format(
            step["label"], ", ".join(str(n) for n in needed_by)
        )
    if step["kind"] == "accounts":
        return _("Accounts first: every entry posts to them.")
    if step["kind"] == "trial balance":
        return _("Last: the trial balance clears what the other opening imports put on Temporary Opening.")
    if step["kind"] == "opening":
        return _("Opening figures after the masters they use.")
    if step["kind"] == "transaction":
        return _("Transactions after the masters they use.")
    return _("Nothing in the other files links here, so it can go any time.")


def _next(steps: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The first step not in yet whose files it waits for are in, with what to do for it."""
    finished = {s["step"] for s in steps if s["state"] in ("done", "undone")}
    for step in steps:
        if step["state"] in ("done", "undone") or not set(step["after"]) <= finished:
            continue
        do = {
            "running": _("wait for it to finish (dry_run_import on its session shows where it is)."),
            "needs attention": _("fix the rows that failed (dry_run_import on its session)."),
        }.get(step["state"])
        if not do:
            do = {
                "File Read": _("map its columns (set_column_mapping)."),
                "Mapped": _("check it (dry_run_import)."),
                "Dry Run Failed": _("fix its rows (dry_run_import with fixes)."),
                "Ready to Import": _("import it (start_import)."),
            }.get(_status_of(step), _("check it (dry_run_import)."))
        return {"step": step["step"], "session": step["session"], "file": step["file"], "do": do}
    return None


def _status_of(step) -> str:
    return frappe.db.get_value(SESSION_DOCTYPE, step["session"], "status") or ""
