# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Explaining dry-run failures and fixing them together (#33991).

Each failure group from the dry run gets a plain explanation and the fixes that fit it:

- **map** a value to one that exists (a link's record, or a select option). The engine
  ranks the existing names; the model only presents them.
- **create** the missing record, with the existing create_document tool. The record then
  exists, so the next check passes the rows that need it.
- **leave out** the rows, with a reason. The import skips them and reports them.

Map and leave out are stored on the session, never in the file: the value maps and the
reasons in ``transformation_rules``, the row numbers in ``excluded_rows``.
"""

import difflib
import re
from typing import Any, Dict, List, Tuple

import frappe
from frappe import _
from frappe.utils import cstr

from frappe_assistant_core.plugins.core import import_dry_run as dry_run

MAP = "map"
CREATE = "create"
LEAVE_OUT = "leave_out"
ACTIONS = (MAP, LEAVE_OUT)  # applied by dry_run_import; create is done with create_document

CLOSEST_LIMIT = 5
MIN_SCORE = 0.5
FULL_SCAN = 2000  # rank every record when a DocType has at most this many
WORD_MATCHES = 200  # else only records sharing a word with the value, this many per word


class FixProblem(Exception):
    """A fix that can't be applied. The message is shown to the user."""


class FixProblems(FixProblem):
    """Some fixes were wrong, so none was saved. ``problems`` says which and why."""

    def __init__(self, problems: List[Dict[str, Any]]):
        super().__init__(_("No fix was saved. Correct these and call again."))
        self.problems = problems


# -- what the session holds ------------------------------------------------------------


def _rules(session) -> Dict[str, Any]:
    return frappe.parse_json(session.transformation_rules or "{}") or {}


def value_maps(session) -> List[Dict[str, Any]]:
    return list(_rules(session).get("value_maps") or [])


def left_out(session) -> List[Dict[str, Any]]:
    """Rows the user chose to leave out, each ``{"row", "reason"}``, in row order."""
    reasons = _rules(session).get("left_out") or {}
    rows = sorted(dry_run._excluded_rows(session))
    return [{"row": row, "reason": cstr(reasons.get(str(row)))} for row in rows]


def left_out_report(session) -> List[Dict[str, Any]]:
    """The left-out rows grouped by reason, for the user: reason, count and the row numbers."""
    by_reason: Dict[str, List[int]] = {}
    for entry in left_out(session):
        by_reason.setdefault(entry["reason"] or _("Left out by the user"), []).append(entry["row"])
    return [
        {"reason": reason, "count": len(rows), "rows": row_ranges(rows)} for reason, rows in by_reason.items()
    ]


def row_ranges(rows: List[int]) -> str:
    """[2, 3, 4, 9] → "2-4, 9"."""
    parts, start, previous = [], None, None
    for row in sorted(set(rows)):
        if start is None:
            start = previous = row
        elif row == previous + 1:
            previous = row
        else:
            parts.append(_range(start, previous))
            start = previous = row
    if start is not None:
        parts.append(_range(start, previous))
    return ", ".join(parts)


def _rows(count: int) -> str:
    return _("1 row") if count == 1 else _("{0} rows").format(count)


def _range(start: int, end: int) -> str:
    return str(start) if start == end else f"{start}-{end}"


# -- applying the value maps to the file's rows -------------------------------------------


def apply_value_maps(session, rows: List[List[str]]) -> List[List[str]]:
    """The file's rows (header first) with every value map applied to its columns."""
    maps = value_maps(session)
    if not maps or not rows:
        return rows
    by_column: Dict[int, Dict[str, str]] = {}
    for position, target in _column_targets(session).items():
        for rule in maps:
            if target == (rule.get("parent"), rule.get("field")):
                by_column.setdefault(position, {})[cstr(rule.get("from")).strip()] = cstr(rule.get("to"))
    if not by_column:
        return rows
    out = [rows[0]]
    for row in rows[1:]:
        row = list(row)
        for position, replace in by_column.items():
            if position < len(row) and cstr(row[position]).strip() in replace:
                row[position] = replace[cstr(row[position]).strip()]
        out.append(row)
    return out


def _column_targets(session) -> Dict[int, Tuple[str, str]]:
    """Column position → (DocType the field is on, fieldname), for mapped columns."""
    meta = frappe.get_meta(session.target_doctype)
    targets = {}
    for position, target in dry_run.column_to_field_map(session).items():
        if not str(position).isdigit() or not target or target == "Don't Import":
            continue
        if "." in target:
            table_field, fieldname = target.split(".", 1)
            table = meta.get_field(table_field)
            if not table:
                continue
            targets[int(position)] = (table.options, fieldname)
        else:
            targets[int(position)] = (session.target_doctype, target)
    return targets


def _with_target(session, group: Dict[str, Any]) -> Dict[str, Any]:
    """``group`` with the DocType its field is on and, for a missing link, the DocType it
    links to, both worked out from the session's mapping."""
    if not group.get("field"):
        return group
    # The dry run says which DocType the field is on; only a duplicate's key field, always
    # on the target DocType, comes without it.
    parent = group.get("parent")
    if not parent:
        parents = [p for p, field in _column_targets(session).values() if field == group["field"]]
        parent = session.target_doctype if session.target_doctype in parents else next(iter(parents), None)
    out = {**group, "parent": parent or session.target_doctype}
    if group.get("cause") == dry_run.CAUSE_MISSING_LINK:
        df = frappe.get_meta(out["parent"]).get_field(group["field"])
        out["link_doctype"] = df.options if df else None
    return out


# -- closest existing values -------------------------------------------------------------


def closest_names(doctype: str, value: str, limit: int = CLOSEST_LIMIT) -> List[str]:
    """Existing names of ``doctype`` closest to ``value``, best first, that the user can read.

    Disabled records and the group nodes of a tree are left out: a link to them fails.
    """
    if not frappe.has_permission(doctype, "read"):
        return []
    meta = frappe.get_meta(doctype)
    filters = {}
    if meta.has_field("disabled"):
        filters["disabled"] = 0
    if meta.is_tree and meta.has_field("is_group"):
        filters["is_group"] = 0

    if frappe.db.count(doctype) <= FULL_SCAN:
        names = frappe.get_list(doctype, filters=filters, pluck="name", limit_page_length=FULL_SCAN)
    else:
        names = []
        for word in _words(value)[:5]:
            names += frappe.get_list(
                doctype,
                filters={**filters, "name": ["like", f"%{word}%"]},
                pluck="name",
                limit_page_length=WORD_MATCHES,
            )
    return rank(value, names, limit)


def rank(value: str, candidates: List[str], limit: int = CLOSEST_LIMIT) -> List[str]:
    scored = [(score(value, c), c) for c in dict.fromkeys(cstr(c) for c in candidates) if c]
    scored = [(s, c) for s, c in scored if s >= MIN_SCORE]
    scored.sort(key=lambda pair: (-pair[0], len(pair[1]), pair[1]))
    return [c for _s, c in scored[:limit]]


def score(value: str, candidate: str) -> float:
    """How likely ``candidate`` is the record ``value`` meant, from 0 to 1.

    Spelling ("Retial" → "Retail"), containment ("Retail" → "Retail Customers") and shared
    words ("Cust Retail" → "Retail Customers") each count.
    """
    v, c = " ".join(_words(value)).casefold(), " ".join(_words(candidate)).casefold()
    if not v or not c:
        return 0.0
    if v == c:
        return 1.0
    best = difflib.SequenceMatcher(None, v, c).ratio()
    if v in c or c in v:
        best = max(best, 0.85)
    shared = set(v.split()) & set(c.split())
    if shared:
        best = max(best, 0.6 + 0.3 * len(shared) / len(set(v.split())))
    return round(best, 3)


def _words(text: str) -> List[str]:
    return re.findall(r"\w+", cstr(text))


# -- explaining a group ----------------------------------------------------------------


def advise(session, group: Dict[str, Any]) -> Dict[str, Any]:
    """A plain explanation of ``group`` and the fixes that fit it, with the closest names."""
    group = _with_target(session, group)
    cause, count = group.get("cause"), group.get("count") or 0
    label, value = _(group.get("label") or group.get("field") or ""), group.get("value")
    rows = _rows(count)
    leave_out = {"action": LEAVE_OUT, "rows": count}

    if cause == dry_run.CAUSE_MISSING_LINK and group.get("link_doctype"):
        doctype = group["link_doctype"]
        explanation = _("{0}: there is no {1} called '{2}' in ERPNext.").format(rows, _(doctype), value)
        fixes = [
            {"action": MAP, "closest": closest_names(doctype, value)},
            {"action": CREATE, "use": "create_document", "doctype": doctype, "name": value},
            leave_out,
        ]
        return {"explanation": explanation, "fixes": fixes}

    if cause == dry_run.CAUSE_BAD_OPTION:
        options = _select_options(session, group)
        explanation = _("{0}: '{1}' isn't one of the choices for {2} ({3}).").format(
            rows, value, label, ", ".join(options)
        )
        closest = rank(value, options) or options[:CLOSEST_LIMIT]
        return {"explanation": explanation, "fixes": [{"action": MAP, "closest": closest}, leave_out]}

    explanations = {
        dry_run.CAUSE_BAD_DATE: _("{0}: {1} isn't a date ERPNext can read. {2}"),
        dry_run.CAUSE_BAD_VALUE: _("{0}: a value ERPNext can't use. {2}"),
        dry_run.CAUSE_DUPLICATE_IN_FILE: _(
            "{0}: the {1} repeats one earlier in the file, and only the first can be imported."
        ),
        dry_run.CAUSE_ALREADY_EXISTS: _(
            "{0}: a record with this {1} is already in ERPNext, so importing would duplicate it."
        ),
        dry_run.CAUSE_REJECTED: _("{0} rejected by ERPNext: {2}"),
    }
    template = explanations.get(cause, _("{0} would fail. {2}"))
    explanation = template.format(rows, label, group.get("message") or "").strip()
    # Anything else is corrected in the file, which is then checked again.
    return {"explanation": explanation, "fixes": [leave_out], "fix_in_file": True}


def _select_options(session, group: Dict[str, Any]) -> List[str]:
    meta = frappe.get_meta(group.get("parent") or session.target_doctype)
    df = meta.get_field(group.get("field"))
    return [o for o in cstr(df.options if df else "").split("\n") if o]


# -- applying fixes ------------------------------------------------------------------


def apply_fixes(session, groups: Dict[str, Dict[str, Any]], fixes: List[Dict[str, Any]]) -> List[str]:
    """Check every fix, then store them all on the session, or none if any is wrong.

    ``groups`` are the last dry run's groups by id, with all their rows. Returns one plain
    sentence per fix applied. The caller saves the session and checks it again.
    """
    maps = value_maps(session)
    reasons = {str(entry["row"]): entry["reason"] for entry in left_out(session)}
    done, problems = [], []
    for fix in fixes:
        group = groups.get(cstr(fix.get("group")))
        try:
            if not group:
                raise FixProblem(
                    _("There is no group {0} in the last check. Groups: {1}.").format(
                        fix.get("group"), ", ".join(groups)
                    )
                )
            group = _with_target(session, group)
            action = fix.get("action")
            if action == MAP:
                maps = _map(session, group, fix, maps)
                done.append(
                    _("{0} '{1}' becomes '{2}' in {3}.").format(
                        _(group["label"]), group["value"], fix["to"], _rows(group["count"])
                    )
                )
            elif action == LEAVE_OUT:
                reason = cstr(fix.get("reason")).strip() or group.get("message") or group["cause"]
                for row in group["rows"]:
                    reasons.setdefault(str(row), reason)
                done.append(_("Left out {0}: {1}.").format(_rows(group["count"]), row_ranges(group["rows"])))
            elif action == CREATE:
                raise FixProblem(
                    _(
                        "Create the missing record with create_document, then call dry_run_import "
                        "again without fixes to check the rows."
                    )
                )
            else:
                raise FixProblem(_("action must be one of: {0}.").format(", ".join(ACTIONS)))
        except FixProblem as e:
            problems.append({"group": fix.get("group"), "error": str(e)})

    if problems:
        raise FixProblems(problems)

    rules = {"value_maps": maps, "left_out": reasons}
    rules = {key: value for key, value in rules.items() if value}
    session.transformation_rules = frappe.as_json(rules) if rules else None
    session.excluded_rows = frappe.as_json(sorted(int(row) for row in reasons)) if reasons else None
    session.log_step("Fix Errors", "Success", " ".join(done))
    return done


def forget_left_out(session) -> str:
    """Put back every row left out, for a different file: its rows aren't the ones that were
    left out. Value maps stay, as they follow values, not rows. Returns what to tell the user."""
    rows = dry_run._excluded_rows(session)
    if not rows:
        return ""
    rules = _rules(session)
    rules.pop("left_out", None)
    session.transformation_rules = frappe.as_json(rules) if rules else None
    session.excluded_rows = None
    note = _("This is a different file, so the {0} left out before are checked again.").format(
        _rows(len(rows))
    )
    session.log_step("Fix Errors", "Success", note)
    session.save()
    return note


def _map(session, group, fix, maps):
    if group["cause"] not in (dry_run.CAUSE_MISSING_LINK, dry_run.CAUSE_BAD_OPTION):
        raise FixProblem(
            _(
                "Only a missing link or a value that isn't an allowed option can be mapped. "
                "Leave these rows out, or correct them in the file."
            )
        )
    to = cstr(fix.get("to")).strip()
    if not to:
        raise FixProblem(_("Say which existing value to use, in 'to'."))
    if group["cause"] == dry_run.CAUSE_MISSING_LINK:
        doctype = group.get("link_doctype")
        if (
            not doctype
            or not frappe.db.exists(doctype, to)
            or not frappe.has_permission(doctype, "read", doc=to)
        ):
            closest = closest_names(doctype, to) if doctype else []
            raise FixProblem(
                _("There is no {0} called '{1}'.").format(_(doctype or group["label"]), to)
                + (" " + _("Closest: {0}.").format(", ".join(closest)) if closest else "")
            )
    elif to not in _select_options(session, group):
        raise FixProblem(
            _("'{0}' isn't one of the choices for {1}: {2}.").format(
                to, _(group["label"]), ", ".join(_select_options(session, group))
            )
        )
    rule = {
        "parent": group["parent"],
        "field": group["field"],
        "label": group["label"],
        "from": group["value"],
        "to": to,
    }
    # A new choice for the same value replaces the earlier one.
    same = (rule["parent"], rule["field"], cstr(rule["from"]))
    return [m for m in maps if (m.get("parent"), m.get("field"), cstr(m.get("from"))) != same] + [rule]


# -- the reply ------------------------------------------------------------------------


def check(session_name: str, commit_progress: bool = False) -> Dict[str, Any]:
    """The dry run, explained. The reply is also kept in validation_summary, so a check run
    in the background reads the same as one that came back at once. The session keeps no row
    of the file, so it keeps the example rows' numbers, not their values."""
    summary = dry_run.run(session_name, commit_progress=commit_progress)
    session = frappe.get_doc(dry_run.SESSION_DOCTYPE, session_name)
    reply = report(session, summary)
    kept = [
        {
            **{k: v for k, v in group.items() if k != "examples"},
            "example_rows": [e["row"] for e in group["examples"]],
        }
        for group in reply["groups"]
    ]
    session.db_set("validation_summary", frappe.as_json({**reply, "groups": kept}), update_modified=False)
    return reply


def last_groups(session) -> Dict[str, Dict[str, Any]]:
    """The last dry run's groups by id, each with all its rows, from the result file."""
    file_doc = dry_run._results_file(session)
    if not file_doc:
        return {}
    content = frappe.parse_json(file_doc.get_content()) or {}
    return {group["id"]: group for group in content.get("groups") or []}


def ready_message(summary: Dict[str, Any]) -> str:
    """What FACO says once nothing fails: the file is ready, with the final count."""
    text = _("The file is ready: {0} will be imported.").format(_rows(summary["ready"]))
    if summary.get("excluded"):
        text += " " + _("{0} left out; the import report will list them.").format(_rows(summary["excluded"]))
    return text


def report(session, summary: Dict[str, Any]) -> Dict[str, Any]:
    """The dry run's reply to the model: the headline, then each group with its examples,
    a plain explanation and the fixes that fit, and the ready message once nothing fails."""
    examples = dry_run.examples(session, summary["groups"])
    groups = []
    for group in summary["groups"]:
        shown = {k: v for k, v in group.items() if k != "example_rows"}
        shown["examples"] = examples.get(group["id"], [])
        shown.update(advise(session, group))
        groups.append(shown)

    message = dry_run.headline(summary) + " " + _("Nothing was written.")
    if not summary["failed"]:
        message += " " + ready_message(summary)
    out = {
        "session": session.name,
        "status": session.status,
        "message": message,
        **{k: v for k, v in summary.items() if k != "groups"},
        "groups": groups,
    }
    if summary.get("excluded"):
        out["left_out"] = left_out_report(session)
    return out
