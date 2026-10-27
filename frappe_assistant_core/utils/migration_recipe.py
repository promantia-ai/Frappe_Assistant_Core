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
        "preset_target": preset.get("target"),
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


# -- running a recipe on new files ----------------------------------------------------------------

#: A file matches a step when at least this share of the step's headers is in it (RC-2).
MATCH_OVERLAP = 0.8


def _norm(header: Any) -> str:
    return re.sub(r"\s+", " ", str(header or "").strip().lower())


def _signature(step) -> List[str]:
    return [str(h) for h in (frappe.parse_json(step.header_signature or "[]") or [])]


def _overlap(step, headers: List[str]) -> float:
    saved = {_norm(h) for h in _signature(step)}
    return len(saved & {_norm(h) for h in headers}) / len(saved) if saved else 0.0


def _preset_ref(session) -> Dict[str, Any]:
    return (frappe.parse_json(session.template_options or "{}") or {}).get("preset") or {}


def _sheet_of(session) -> str:
    return _norm(session.sheet or os.path.splitext(session.file_name or "")[0])


def match_files(doc, sessions: List[Any], steps: Optional[List[Any]] = None) -> Dict[str, Any]:
    """Each new file's session matched to a step of the recipe (RC-2): a preset step by its preset and
    target, any other by at least MATCH_OVERLAP of its headers being in the file, the same sheet name
    first. A file and a step are used once. What matched nothing is listed. ``steps`` are the steps
    to match, all of them when left out."""
    steps = doc.steps if steps is None else steps
    pairs = []
    for step in steps:
        for session in sessions:
            ref = _preset_ref(session)
            if step.preset_id:
                if ref.get("id") != step.preset_id or ref.get("target") != step.preset_target:
                    continue
                score = 2.0
            else:
                if ref:  # a file a preset made is for a preset step
                    continue
                overlap = _overlap(step, session_headers(session))
                if overlap < MATCH_OVERLAP:
                    continue
                score = overlap + (0.5 if _norm(step.sheet_name) == _sheet_of(session) else 0)
                score += (
                    0.25
                    if _norm(os.path.splitext(step.original_file_name or "")[0])
                    == _norm(os.path.splitext(session.file_name or "")[0])
                    else 0
                )
            pairs.append((score, step.position, session.name))
    pairs.sort(key=lambda p: (-p[0], p[1], p[2]))
    steps_used, sessions_used, matched = set(), set(), []
    for score, position, name in pairs:
        if position in steps_used or name in sessions_used:
            continue
        steps_used.add(position)
        sessions_used.add(name)
        matched.append({"step": position, "session": name, "score": round(score, 2)})
    matched.sort(key=lambda m: m["step"])
    return {
        "matched": matched,
        "unmatched_sessions": [s.name for s in sessions if s.name not in sessions_used],
        "unmatched_steps": [
            {"step": s.position, "file": s.original_file_name, "target": s.target_doctype}
            for s in steps
            if s.position not in steps_used
        ],
    }


def _missing_on_site(step, mapping: Dict[str, str]) -> Dict[str, Any]:
    """What of the step isn't on this site (RC-9): its DocType, or the fields of its mapping."""
    if not step.target_doctype or not frappe.db.exists("DocType", step.target_doctype):
        return {"doctype": step.target_doctype, "fields": []}
    meta = frappe.get_meta(step.target_doctype)
    fields = []
    for field in dict.fromkeys(mapping.values()):
        if not field or field == "Don't Import":
            continue
        top = field.split(".", 1)[0]
        if not meta.has_field(top) and top != "name":
            fields.append(field)
        elif "." in field:
            table = meta.get_field(top)
            if (
                table
                and table.options
                and not frappe.get_meta(table.options).has_field(field.split(".", 1)[1])
            ):
                fields.append(field)
    return {"doctype": None, "fields": fields}


def _by_header(step) -> Dict[str, str]:
    """The step's mapping as header -> field: the file's columns may come in another order or with new
    ones between them."""
    headers = _signature(step)
    mapping = frappe.parse_json(step.column_map or "{}") or {}
    return {
        _norm(headers[int(position)]): field
        for position, field in mapping.items()
        if str(position).isdigit() and int(position) < len(headers)
    }


def _position_in(headers: List[str], header: str) -> Optional[int]:
    wanted = _norm(header)
    return next((i for i, h in enumerate(headers) if _norm(h) == wanted), None)


def whats_new(step, session) -> List[Dict[str, Any]]:
    """What differs between the step and the new file, as questions for the plan (RC-3): columns the
    recipe never saw, columns it had that the file doesn't, and what isn't on this site (RC-9). The
    rest isn't asked again: it is applied as it was."""
    headers = session_headers(session)
    saved = {_norm(h): h for h in _signature(step)}
    here = {_norm(h): h for h in headers}
    about = {"session": session.name, "file_name": session.file_name}
    out: List[Dict[str, Any]] = []

    def ask(kind: str, message: str, **extra) -> None:
        out.append(
            {
                "id": f"{RECIPE_PREFIX}{session.name}:{kind}",
                "kind": "recipe_new",
                "what": kind,
                **about,
                "message": message,
                **extra,
            }
        )

    new = [here[h] for h in here if h not in saved]
    if new:
        ask(
            "new_columns",
            _(
                "{0} has columns the recipe didn't know: {1}. They aren't imported. Map one with "
                "set_column_mapping if it is wanted, then answer ok."
            ).format(session.file_name, ", ".join(new)),
            columns=new,
        )
    gone = [saved[h] for h in saved if h not in here]
    if gone:
        ask(
            "gone_columns",
            _("{0} no longer has: {1}. Those fields stay empty. Answer ok to go on.").format(
                session.file_name, ", ".join(gone)
            ),
            columns=gone,
        )
    missing = _missing_on_site(step, _by_header(step))
    if missing["doctype"]:
        ask(
            "not_on_site",
            _(
                "{0} isn't on this site, so this file can't be imported by the recipe. Nothing was guessed."
            ).format(missing["doctype"]),
        )
    elif missing["fields"]:
        ask(
            "not_on_site",
            _("These fields of {0} aren't on this site, so their columns are left unmapped: {1}.").format(
                step.target_doctype, ", ".join(missing["fields"])
            ),
            fields=missing["fields"],
        )
    return out


def apply_step(step, session_name: str) -> Dict[str, Any]:
    """Write the step's mapping, value maps, source key and old ID translations onto the new file's
    session, then report what was new (RC-3). An opening step is not mapped here: its settings are
    handed back for set_opening_stock, set_opening_invoices or set_opening_balances, which ask for the
    cut-off date again (M-D1 keeps none). A preset step's file was mapped by the preset already."""
    from frappe_assistant_core.utils import import_mapping

    session = import_mapping.load_session(session_name)
    result: Dict[str, Any] = {"step": step.position, "session": session.name, "target": step.target_doctype}
    questions = whats_new(step, session)
    if step.opening_engine and step.opening_engine != "none":
        result["opening"] = {
            "engine": step.opening_engine,
            "settings": frappe.parse_json(step.engine_settings or "{}") or {},
        }
        result["questions"] = questions
        return result

    headers = session_headers(session)
    rules = frappe.parse_json(step.transformation_rules or "{}") or {}
    saved = _signature(step)
    if rules.get("value_maps"):
        merged = frappe.parse_json(session.transformation_rules or "{}") or {}
        merged["value_maps"] = rules["value_maps"]
        session.transformation_rules = frappe.as_json(merged)

    if not step.preset_id and step.target_doctype and not _missing_on_site(step, {})["doctype"]:
        missing = set(_missing_on_site(step, _by_header(step))["fields"])
        by_header = _by_header(step)
        mapping = {
            str(i): by_header[_norm(h)]
            for i, h in enumerate(headers)
            if _norm(h) in by_header and by_header[_norm(h)] not in missing
        }
        session.import_type = step.import_type or session.import_type
        import_mapping.save_mapping(
            session,
            step.target_doctype,
            mapping,
            _("Mapped by recipe {0}, step {1}: {2} columns.").format(
                step.parent, step.position, len(mapping)
            ),
        )
        result["mapped_columns"] = len(mapping)

    # the old ID column and its translations follow the header to its new position
    key = rules.get(import_keys.SOURCE_KEY)
    if isinstance(key, dict) and str(key.get("position", "")).isdigit() and int(key["position"]) < len(saved):
        place = _position_in(headers, saved[int(key["position"])])
        if place is not None and not step.preset_id:
            import_keys.set_source_key(session, place, key["system"])
    maps = {}
    for position, spec in (rules.get(import_keys.KEY_MAPS) or {}).items():
        if str(position).isdigit() and int(position) < len(saved):
            place = _position_in(headers, saved[int(position)])
            if place is not None:
                maps[place] = spec
    if maps:
        import_keys.set_key_maps(session, maps)
    session.save()
    result["questions"] = questions
    return result


# -- a delta run: the transactions after the cut-off ---------------------------------------------

LEFT_OUT_BEFORE = "Before the cut-off: the opening migration brought it in."
LEFT_OUT_OPENING = "An opening journal entry: a delta run never posts an opening entry."
YES = ("yes", "1", "true", "y")


def _parse_date(value: Any):
    try:
        return frappe.utils.getdate(value) if str(value or "").strip() else None
    except Exception:
        return None  # a date nobody can read is the dry run's to report, not a reason to leave the row out


def leave_out_before(session, since: str) -> Dict[str, int]:
    """Leave out the rows of ``session``'s file dated on or before the cut-off, with the reason (RC-5,
    M-D5). The date is the parent row's, and the child rows under it follow it (M-D6); a row whose date
    can't be read stays for the dry run to report. A journal entry of the opening is left out too.
    Raises RecipeProblem when the file's date column isn't mapped: nothing could be told apart."""
    from frappe_assistant_core.utils import import_fixes

    mapping = {int(p): f for p, f in dry_run.column_to_field_map(session).items() if str(p).isdigit()}
    date_at = next((p for p, f in mapping.items() if f == "posting_date"), None)
    if date_at is None:
        raise RecipeProblem(
            _(
                "{0} has no column mapped to Posting Date, so the rows after the cut-off can't be told apart. "
                "Map it with set_column_mapping."
            ).format(session.file_name)
        )
    opening_at = next((p for p, f in mapping.items() if f == "is_opening"), None)
    cut_off = frappe.utils.getdate(since)
    rows = dry_run.read_sheet_rows(dry_run.readable_session_file(session), session.sheet or None)
    reasons = {str(entry["row"]): entry["reason"] for entry in import_fixes.left_out(session)}
    counts = {"before": 0, "opening": 0}
    leaving = None  # the reason the current parent row's rows are left out for, or None
    for index, row in enumerate(rows[1:], start=2):  # row 1 is the header
        cell = row[date_at] if date_at < len(row) else ""
        if str(cell or "").strip():  # a parent row: its date decides for it and the rows under it
            date = _parse_date(cell)
            opening = opening_at is not None and str(row[opening_at]).strip().lower() in YES
            leaving = None
            if opening:
                leaving = LEFT_OUT_OPENING
            elif date and date <= cut_off:
                leaving = LEFT_OUT_BEFORE
        if leaving:
            reasons.setdefault(str(index), _(leaving))
            counts["opening" if leaving == LEFT_OUT_OPENING else "before"] += 1
    rules = frappe.parse_json(session.transformation_rules or "{}") or {}
    if reasons:
        rules["left_out"] = reasons
        session.excluded_rows = frappe.as_json(sorted(int(row) for row in reasons))
    session.transformation_rules = frappe.as_json(rules) if rules else None
    session.submit_after_import = 1  # every delta document is submitted (M-D6)
    session.save()
    return counts


def run(
    recipe_name: str,
    session_names: List[str],
    since: Optional[str] = None,
    masters: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """Match the new files to the recipe, write each step onto its file, and make the migration plan of
    the matched files in the recipe's order and with its failure rule (RC-2 to RC-4). Nothing is
    imported: the plan's questions, then its one approval, come next.

    With ``since`` (the cut-off date) it is a delta run (RC-5): only the transaction steps are run, and
    the masters at the positions in ``masters`` (insert only), rows dated on or before the cut-off are
    left out, and no opening step is run. A file of a delta run goes in the order of M-D6."""
    from frappe_assistant_core.plugins.core.tools.plan_migration import PlanMigration
    from frappe_assistant_core.utils import import_mapping

    doc = frappe.get_doc(RECIPE, recipe_name)
    doc.check_permission("read")
    sessions = [import_mapping.load_session(name) for name in dict.fromkeys(session_names)]
    skipped: List[Dict[str, Any]] = []
    wanted = None
    if since:
        since = import_plan.as_date(since)
        ticked = {int(p) for p in masters or []}
        wanted = []
        for step in doc.steps:
            if step.opening_engine and step.opening_engine != "none":
                reason = _("an opening step: a delta run never posts an opening entry")
            elif step.is_transaction_step or step.position in ticked:
                wanted.append(step)
                continue
            else:
                reason = _(
                    "a master: ask the user, and pass its position in masters to bring in the new ones"
                )
            skipped.append({"step": step.position, "file": step.original_file_name, "reason": reason})
    found = match_files(doc, sessions, wanted)
    if not found["matched"]:
        raise RecipeProblem(
            _(
                "None of these files matches a step of this recipe (at least {0}% of a step's columns must be in the file)."
            ).format(int(MATCH_OVERLAP * 100))
        )
    steps = {step.position: step for step in doc.steps}
    applied = [apply_step(steps[m["step"]], m["session"]) for m in found["matched"]]
    if since:
        for entry in applied:
            step = steps[entry["step"]]
            session = import_mapping.load_session(entry["session"])
            if step.target_doctype in TRANSACTION_DOCTYPES:
                entry["left_out"] = leave_out_before(session, since)
            else:  # a master: only the new ones, and nothing is submitted
                session.import_type = "Insert New Records"
                session.save()
    # An opening step is set up by its own tool, so it can't be planned yet: it joins when it is set.
    planned = [a["session"] for a in applied if "opening" not in a]
    out: Dict[str, Any] = {"recipe": doc.name, **found, "applied": applied}
    if since:
        out["since"] = since
        out["skipped_steps"] = skipped
    if not planned:
        return out
    made = PlanMigration().execute(
        {
            "sessions": planned,
            # a delta run goes in the order of M-D6, which the plan works out
            **({} if since else {"order": planned}),
            "failure_rule": doc.failure_rule or "leave_out",
        }
    )
    if not made.get("success"):
        raise RecipeProblem(made.get("error") or _("The plan could not be made."))
    plan = import_plan.get_plan(made["plan"])
    progress = frappe.parse_json(plan.progress or "{}") or {}
    progress["recipe"] = {
        "name": doc.name,
        "questions": [q for a in applied for q in a["questions"]],
        "answered": [],
    }
    if since:
        progress["delta_since"] = since
    plan.progress = frappe.as_json(progress)
    plan.log_step("Plan", "Success", _("From recipe {0}.").format(doc.name))
    if since:
        plan.log_step("Plan", "Success", _("Delta run: what happened after {0}.").format(since))
    plan.save_open()
    if since:
        import_plan.arrange(plan)  # again, now it is known as a delta run: the order is not an opening's
    out["plan"] = plan.name
    out["problems"] = made.get("problems") or []
    return out


def record_run(plan_name: str, status: str, message: str) -> None:
    """A plan that came from a recipe ended: the recipe keeps when, where and how it went (RC-7)."""
    try:
        recipe = (frappe.parse_json(import_plan.get_plan(plan_name).progress or "{}") or {}).get("recipe")
        if recipe and frappe.db.exists(RECIPE, recipe.get("name")):
            frappe.db.set_value(
                RECIPE,
                recipe["name"],
                {
                    "last_run_on": frappe.utils.now_datetime(),
                    "last_run_site": frappe.local.site,
                    "last_run_result": f"{status}: {message}",
                },
                update_modified=False,
            )
    except Exception:
        frappe.log_error(title="Recipe run record failed", message=frappe.get_traceback())


RECIPE_PREFIX = import_plan.RECIPE_PREFIX
