# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Migration plans (#33995): several import sessions of one migration, ordered so masters come
before the records that link to them, checked knowing what earlier files will create, and run
after one approval.

The order comes from the target DocTypes' Link fields, not a hand-written list: a file comes
after every other file of the plan whose DocType it links to. The opening imports come last,
stock, then invoices, then the trial balance, as migrate_data.md has always told FACO.
"""

import hashlib
import json
from typing import Any, Dict, List, Optional, Set, Tuple

import frappe
from frappe import _
from frappe.utils import cstr

from frappe_assistant_core.plugins.core import import_dry_run as dry_run

PLAN_DOCTYPE = "FAC Migration Plan"
ANSWERS_SAVEPOINT = "fac_plan_answers"
#: A session in one of these may join a plan: nothing of it is imported, undone or cancelled.
JOINABLE = ("File Read", "Mapped", "Dry Run Failed", "Ready to Import")
#: A plan in one of these takes new files; once it runs, its files are fixed.
OPEN = ("Planning", "Ready to Run")
#: A session in one of these has been imported, as far as it will go.
IMPORTED = ("Completed", "Partially Completed", "Failed")


class PlanProblem(Exception):
    """The plan can't do what was asked. The message is shown to the user."""


# -- the plan ----------------------------------------------------------------------------------


def load_plan(name: str):
    """The plan, if this user may change it. Raises DoesNotExistError or PermissionError."""
    doc = frappe.get_doc(PLAN_DOCTYPE, name)
    if not frappe.has_permission(PLAN_DOCTYPE, "write", doc=doc):
        raise frappe.PermissionError(_("You can't change migration plan {0}.").format(name))
    return doc


def positions(plan) -> Dict[str, int]:
    """Session → its place in the plan's order."""
    return {entry["session"]: entry["position"] for entry in frappe.parse_json(plan.order or "[]") or []}


def children(plan) -> List[Any]:
    """The plan's sessions in its order; one not ordered yet goes last, oldest first."""
    names = frappe.get_all(
        dry_run.SESSION_DOCTYPE, filters={"migration_plan": plan.name}, order_by="creation", pluck="name"
    )
    place = positions(plan)
    oldest = {name: i for i, name in enumerate(names)}
    names.sort(key=lambda name: place.get(name, len(place) + 1 + oldest[name]))
    return [frappe.get_doc(dry_run.SESSION_DOCTYPE, name) for name in names]


def join_problems(plan_name: str, sessions: List[Any]) -> List[str]:
    """Why any of ``sessions`` can't join the plan: in another plan, or past being planned."""
    problems = []
    for session in sessions:
        other = session.migration_plan if session.migration_plan != plan_name else None
        # A plan that stopped or ended lets its files go: the rest can be planned again.
        if other and frappe.db.get_value(PLAN_DOCTYPE, other, "status") in (*OPEN, "Running"):
            problems.append(_("{0} is already in plan {1}.").format(session.file_name, other))
        elif session.status not in JOINABLE:
            problems.append(
                _("{0} is {1}, so it can't join a plan.").format(session.file_name, _(session.status))
            )
    return problems


# -- what the plan's files create ----------------------------------------------------------------


def _created_names(session) -> List[str]:
    """The names the rows of the session's last check create, or mean to: a failing row whose
    name is one of its fields counts too, as its file is fixed or imported without it before any
    later file is."""
    from frappe_assistant_core.plugins.core import import_run

    return [
        row["name"]
        for row in import_run._checked_rows(session)
        if row.get("status") in ("ready", "failed") and row.get("name")
    ]


def masters(plan) -> List[Dict[str, Any]]:
    """The records the plan creates before any file is imported."""
    return frappe.parse_json(plan.masters or "[]") or []


def add_masters(session, items: List[Dict[str, Any]]) -> None:
    """Add masters the session's rows need to its plan, once each across the plan."""
    from frappe_assistant_core.plugins.core import import_fixes

    plan = load_plan(session.migration_plan)
    entries = masters(plan)
    known = {(entry["doctype"], entry["name"].casefold()) for entry in entries}
    for item in items:
        name = import_fixes._planned_name(item)
        if (item["doctype"], name.casefold()) not in known:
            known.add((item["doctype"], name.casefold()))
            entries.append(
                {"session": session.name, "doctype": item["doctype"], "name": name, "data": item["data"]}
            )
    plan.masters = frappe.as_json(entries)
    plan.save()


def create_planned(plan) -> List[Dict[str, Any]]:
    """Create the plan's masters not in ERPNext yet, each listed on the session whose rows need
    it, so undoing that session removes it. Raises PlanProblem if ERPNext refuses one."""
    from frappe_assistant_core.plugins.core import import_fixes

    made: List[Dict[str, Any]] = []
    by_session: Dict[str, List[Dict[str, Any]]] = {}
    for entry in masters(plan):
        if not frappe.db.exists(entry["doctype"], entry["name"]):
            by_session.setdefault(entry["session"], []).append(entry)
    # All or none: a master made before one ERPNext refuses would be listed on no session, so no
    # undo would remove it.
    frappe.db.savepoint(MASTERS_SAVEPOINT)
    try:
        for session_name, entries in by_session.items():
            results = []
            for entry in entries:
                result = import_fixes.create_master(entry)
                if not result.get("success"):
                    raise PlanProblem(
                        _("{0} '{1}' couldn't be created: {2}").format(
                            _(entry["doctype"]), entry["name"], result.get("error")
                        )
                    )
                results.append(result)
            import_fixes.link_created(frappe.get_doc(dry_run.SESSION_DOCTYPE, session_name), results)
            made += results
    except Exception:
        frappe.db.rollback(save_point=MASTERS_SAVEPOINT)
        raise
    return made


def _names(sessions: List[Any], plan=None) -> Dict[str, Dict[str, str]]:
    names: Dict[str, Dict[str, str]] = {}
    for session in sessions:
        if session.status in IMPORTED:  # its records are in ERPNext now, or genuinely missing
            continue
        for name in _created_names(session):
            names.setdefault(session.target_doctype, {}).setdefault(
                name.strip().casefold(), session.file_name
            )
    for entry in masters(plan) if plan else []:
        names.setdefault(entry["doctype"], {}).setdefault(
            entry["name"].strip().casefold(), _("the plan's new records")
        )
    return names


def _split(session) -> Tuple[Any, List[Any], List[Any]]:
    """The session's plan, and its files before and after the session."""
    if not session.get("migration_plan"):
        return None, [], []
    plan = frappe.get_doc(PLAN_DOCTYPE, session.migration_plan)
    files = children(plan)
    at = next((i for i, s in enumerate(files) if s.name == session.name), len(files))
    return plan, files[:at], files[at + 1 :]


def planned_names(session) -> Dict[str, Dict[str, str]]:
    """DocType → casefolded name → the file that creates it, for the records the files before the
    session in its plan, and the plan itself, will have created by the time it is imported."""
    plan, before, _after = _split(session)
    return _names(before, plan) if plan else {}


def later_names(session) -> Dict[str, Dict[str, str]]:
    """The same for the files imported after the session: a link to one is an order problem."""
    plan, _before, after = _split(session)
    return _names(after) if plan else {}


def arrange(plan) -> List[str]:
    """Order the plan's sessions and store the order. Returns the order's problems. An order the
    user set (plan_migration's order) is kept: files not in it go after, in their own order."""
    files = children(plan)
    fixed = [name for name in _progress(plan).get("fixed_order") or [] if name in {s.name for s in files}]
    if fixed:
        ordered, _problems = order([s for s in files if s.name not in fixed])
        ordered = [{"session": name, "reason": _("In the order you set.")} for name in fixed] + [
            {"session": entry["session"], "reason": entry["reason"]} for entry in ordered
        ]
        plan.order = frappe.as_json([{**entry, "position": i} for i, entry in enumerate(ordered, start=1)])
        return []
    ordered, problems = order(files)
    plan.order = frappe.as_json(ordered)
    return problems


def describe(plan) -> Dict[str, Any]:
    """The plan as FACO shows it: its files in order, each with why it's there and its state."""
    reasons = {entry["session"]: entry for entry in frappe.parse_json(plan.order or "[]") or []}
    files = []
    for session in children(plan):
        entry = reasons.get(session.name) or {}
        files.append(
            {
                "session": session.name,
                "file_name": session.file_name,
                "doctype": session.target_doctype,
                "position": entry.get("position"),
                "reason": entry.get("reason"),
                "status": session.status,
            }
        )
    return {
        "plan": plan.name,
        "status": plan.status,
        "failure_rule": plan.failure_rule,
        "files": files,
    }


# -- the order ---------------------------------------------------------------------------------


def dependencies(doctype: str) -> Set[str]:
    """The DocTypes ``doctype``'s records link to, its child tables' links included."""
    meta = frappe.get_meta(doctype)
    linked = {df.options for df in meta.fields if df.fieldtype == "Link" and df.options}
    for table_df in meta.get_table_fields():
        child = frappe.get_meta(table_df.options)
        linked |= {df.options for df in child.fields if df.fieldtype == "Link" and df.options}
    linked.discard(doctype)  # a tree's parent is in the same file: import_run takes it a level at a time
    return linked


def _links(session) -> Set[str]:
    """The DocTypes the session's mapped columns link to; all its DocType's links before it is
    mapped. A column mapped to Don't Import doesn't make its file wait for another."""
    from frappe_assistant_core.plugins.core.import_mapping import DONT_IMPORT, session_mapping

    mapping = session_mapping(session)
    if not mapping:
        return dependencies(session.target_doctype)
    meta = frappe.get_meta(session.target_doctype)
    linked = set()
    for field in mapping.values():
        if not field or field == DONT_IMPORT:
            continue
        table, _dot, fieldname = field.rpartition(".")
        on = meta
        if table:
            table_df = meta.get_field(table)
            if not table_df:
                continue
            on = frappe.get_meta(table_df.options)
        df = on.get_field(fieldname)
        if df and df.fieldtype == "Link" and df.options:
            linked.add(df.options)
    linked.discard(session.target_doctype)
    return linked


def _engine_rank(session) -> int:
    """0 for a file imported through Data Import; 1, 2, 3 for opening stock, invoices, balances."""
    from frappe_assistant_core.plugins.core import import_opening_balances as balances
    from frappe_assistant_core.plugins.core import import_opening_invoices as invoices
    from frappe_assistant_core.plugins.core import import_opening_stock as stock

    engine = dry_run.special_engine(session)
    return {None: 0, stock: 1, invoices: 2, balances: 3}.get(engine, 3)


def order(sessions: List[Any]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """The sessions in the order they are imported, each with its position and the reason, and a
    problem for every pair of files that link to each other. Among files free to go, the one given
    first goes first."""
    plain = [s for s in sessions if not _engine_rank(s)]
    opening = sorted((s for s in sessions if _engine_rank(s)), key=_engine_rank)

    needs = {}
    for session in plain:
        wanted = _links(session) if session.target_doctype else set()
        needs[session.name] = [
            other for other in plain if other.name != session.name and other.target_doctype in wanted
        ]

    ordered, placed, problems = [], set(), []
    waiting = list(plain)
    while waiting:
        free = next((s for s in waiting if all(o.name in placed for o in needs[s.name])), None)
        if free is None:  # every file left waits on another: they link to each other
            first = waiting[0]
            other = next(o for o in needs[first.name] if o.name not in placed)
            problems.append(
                _(
                    "{0} and {1} link to each other: map the linking columns of one to Don't Import "
                    "(and import them later), or set the order with plan_migration's order."
                ).format(first.file_name, other.file_name)
            )
            free = first
        waiting.remove(free)
        placed.add(free.name)
        before = needs[free.name]
        if before:
            reason = _("After {0}: it links to their {1} records.").format(
                ", ".join(o.file_name for o in before),
                ", ".join(sorted({_(o.target_doctype) for o in before})),
            )
        else:
            reason = _("Nothing in the plan has to come first.")
        ordered.append((free, reason))

    for session in opening:
        ordered.append((session, _("Opening balances come after the records they refer to.")))
    return [
        {"session": session.name, "position": position, "reason": reason}
        for position, (session, reason) in enumerate(ordered, start=1)
    ], problems


# -- the questions -----------------------------------------------------------------------------
#
# Every question of the plan comes in one list, before anything is imported: the files still to
# map, the files that link to each other, each failure group with the engine's suggested fixes,
# and the failure rule. Answers come back in one call.

FAILURE_RULE_ID = "plan:failure_rule"
CHECKED = ("Dry Run Failed", "Ready to Import")


def _bring_up_to_date(plan, session) -> Optional[Dict[str, Any]]:
    """Check the session again unless its last check is of it as it is now, in a plan whose files
    create what they did then. A question when it can't be checked here: still running, too
    large for this request, or not checkable yet. Keeps what the check depended on in the plan
    (the caller saves it)."""
    from frappe_assistant_core.plugins.core import import_fixes

    if dry_run.is_running(session):
        return {"kind": "checking", "message": _("Still being checked. Ask again in a moment.")}
    if session.status == "File Read":
        return None
    digests = dict(_progress(plan).get("digests") or {})
    digest = plan_digest(session)
    # A file that failed is checked again whatever its fingerprint: an earlier file of the plan may
    # have changed what it will create.
    if (
        session.status == "Ready to Import"
        and import_fixes.checked_as_is(session)
        and digests.get(session.name) == digest
    ):
        return None
    try:
        dry_run.check_can_run(session)
    except dry_run.DryRunProblem as e:
        return {"kind": "blocked", "message": str(e)}
    digests[session.name] = digest
    plan.progress = frappe.as_json({**_progress(plan), "digests": digests})
    if dry_run.row_count(session) > dry_run.BACKGROUND_ROWS:
        dry_run.enqueue(session)
        return {"kind": "checking", "message": _("Being checked in the background. Ask again in a moment.")}
    import_fixes.check(session.name)
    return None


#: A file with one of these questions isn't checked yet: the files after it wait for it.
UNCHECKED = ("checking", "blocked", "structure")


def _file_questions(plan, session) -> List[Dict[str, Any]]:
    from frappe_assistant_core.plugins.core import import_fixes
    from frappe_assistant_core.plugins.core.import_mapping import session_headers

    about = {"session": session.name, "file_name": session.file_name}
    waiting = _bring_up_to_date(plan, session)
    session.reload()
    if waiting:
        return [{"id": f"{session.name}:check", **about, **waiting}]
    if session.status == "File Read" or not session.target_doctype:
        return [
            {
                "id": f"{session.name}:structure",
                **about,
                "kind": "structure",
                "message": _(
                    "Map this file (get_import_schema, set_column_mapping), or set it up as opening "
                    "stock, invoices or balances."
                ),
                "columns": session_headers(session),
            }
        ]
    if session.status != "Dry Run Failed":
        return []
    questions = []
    for group in import_fixes.last_groups(session).values():
        advice = import_fixes.advise(session, group)
        questions.append(
            {
                "id": f"{session.name}:{group['id']}",
                **about,
                "kind": "fix",
                "cause": group.get("cause"),
                "message": group.get("message"),
                "count": group.get("count"),
                **advice,
            }
        )
    return questions


def open_questions(plan) -> Dict[str, Any]:
    """Every open question of the plan, the files checked first in the plan's order (an earlier
    file's records count for the later ones). The plan is Ready to Run once none is left and every
    file is ready."""
    questions: List[Dict[str, Any]] = []
    for problem in arrange(plan):
        questions.append({"id": f"plan:order:{len(questions)}", "kind": "order", "message": problem})
    first_unchecked = None
    for session in children(plan):
        if first_unchecked:
            # What an earlier file creates isn't known until it is checked: checked now, this file
            # would ask about records that file brings.
            questions.append(
                {
                    "id": f"{session.name}:waiting",
                    "session": session.name,
                    "file_name": session.file_name,
                    "kind": "waiting",
                    "message": _("Checked once {0} is.").format(first_unchecked),
                }
            )
            continue
        asked = _file_questions(plan, session)
        if any(q["kind"] in UNCHECKED for q in asked):
            first_unchecked = session.file_name
        questions += asked
    progress = frappe.parse_json(plan.progress or "{}") or {}
    if not progress.get("rule_confirmed"):
        questions.append(
            {
                "id": FAILURE_RULE_ID,
                "kind": "failure_rule",
                "message": _(
                    "If rows still fail when their file comes up (a record changed meanwhile, or the "
                    "row their parent is in failed): leave them out with their reason, or stop the "
                    "plan before that file?"
                ),
                "suggested": plan.failure_rule or "leave_out",
                "choices": ["leave_out", "stop"],
            }
        )
    files = children(plan)
    ready = bool(files) and not questions and all(s.status == "Ready to Import" for s in files)
    plan.status = "Ready to Run" if ready else "Planning"
    plan.save()
    return {**describe(plan), "questions": questions, "ready": ready}


def apply_answers(plan, answers: List[Dict[str, Any]]) -> List[str]:
    """Apply the user's answers, each to the question it names, all or none. Returns what was done.
    Raises PlanProblem, or import_fixes.FixProblem(s) for a fix the session refuses."""
    from frappe_assistant_core.plugins.core import import_fixes

    files = {session.name: session for session in children(plan)}
    fixes: Dict[str, List[Dict[str, Any]]] = {}
    rule = None
    for answer in answers:
        if not isinstance(answer, dict):
            raise PlanProblem(_("Each answer is an object with the id of its question."))
        question = cstr(answer.get("id"))
        if question == FAILURE_RULE_ID:
            rule = answer.get("rule")
            if rule not in ("leave_out", "stop"):
                raise PlanProblem(_("The failure rule is leave_out or stop."))
            continue
        session_name, _sep, group = question.rpartition(":")
        if session_name not in files or not group.startswith("G"):
            raise PlanProblem(_("There is no question {0} in this plan.").format(question))
        fixes.setdefault(session_name, []).append(
            {"group": group, **{k: v for k, v in answer.items() if k != "id"}}
        )

    done: List[str] = []
    frappe.db.savepoint(ANSWERS_SAVEPOINT)
    try:
        for session_name, session_fixes in fixes.items():
            session = files[session_name]
            done += import_fixes.apply_fixes(session, import_fixes.last_groups(session), session_fixes)
            session.save()
        if rule:
            plan.reload()
            plan.failure_rule = rule
            progress = frappe.parse_json(plan.progress or "{}") or {}
            plan.progress = frappe.as_json({**progress, "rule_confirmed": True})
            plan.log_step("Questions", "Success", _("Failure rule: {0}.").format(rule))
            plan.save()
            done.append(_("Rows that fail when their file comes up: {0}.").format(rule))
    except Exception:
        frappe.db.rollback(save_point=ANSWERS_SAVEPOINT)
        raise
    return done


# -- the run -------------------------------------------------------------------------------------
#
# One approval, then a chain: the plan's masters, then each file in order, checked again just before
# its import (a file before it may have failed rows) with the failure rule applied, the next file
# starting when the last one ends. A file imported in a background job continues the chain from
# that job's end (child_finished); one imported in the request continues the loop.


PLAN_JOB = "migration_plan::{0}"
STEP_SAVEPOINT = "fac_plan_step"
MASTERS_SAVEPOINT = "fac_plan_masters"


def _progress(plan) -> Dict[str, Any]:
    return frappe.parse_json(plan.progress or "{}") or {}


def _update(plan_name: str, status: Optional[str] = None, step: Optional[Tuple[str, str]] = None, **progress):
    """Change the plan as it is in the database now. The run and stop_migration_plan both write to
    it, so nothing saves a copy read before a long step."""
    plan = frappe.get_doc(PLAN_DOCTYPE, plan_name)
    if progress:
        plan.progress = frappe.as_json({**_progress(plan), **progress})
    if status:
        plan.status = status
    if step:
        plan.log_step("Run", *step)
    plan.save()
    return plan


def plan_digest(session) -> str:
    """What a ready file's check depends on in its plan, besides the file itself: the records its
    earlier files (and the plan) will create. (What later files create only matters to rows that
    fail, and a file with failing rows is always checked again.)"""
    return hashlib.sha256(json.dumps(planned_names(session), sort_keys=True).encode()).hexdigest()


def card_figures(plan) -> Dict[str, int]:
    """What the approval card shows: files, rows ready to import, masters to create."""
    files = children(plan)
    rows = sum((frappe.parse_json(s.validation_summary or "{}") or {}).get("ready") or 0 for s in files)
    new = [m for m in masters(plan) if not frappe.db.exists(m["doctype"], m["name"])]
    return {"files": len(files), "rows": rows, "masters": len(new)}


def run_problem(plan) -> Optional[str]:
    """Why the plan can't run now, or None."""
    from frappe_assistant_core.plugins.core import import_fixes, import_run
    from frappe_assistant_core.plugins.core.import_session import import_target_problem

    if plan.status != "Ready to Run":
        return _("The plan is {0}: run plan_questions until it is Ready to Run.").format(_(plan.status))
    digests = _progress(plan).get("digests") or {}
    for session in children(plan):
        if (
            session.status != "Ready to Import"
            or not import_fixes.checked_as_is(session)
            or digests.get(session.name) != plan_digest(session)
        ):
            return _(
                "{0} changed since it was checked. Run plan_questions again: the approval is for what was checked."
            ).format(session.file_name)
        if dry_run.special_engine(session):
            problem = import_target_problem(session.target_doctype) if session.target_doctype else None
        else:
            problem = import_run.cannot_import(
                session.target_doctype, session.import_type or "Insert New Records"
            )
        if problem:
            return _("{0}: {1}").format(session.file_name, problem)
    for entry in masters(plan):
        if frappe.db.exists(entry["doctype"], entry["name"]):
            continue
        reason = import_fixes.cannot_create(entry["doctype"])
        if not reason and not frappe.has_permission(entry["doctype"], "create"):
            reason = _("you can't create {0} records").format(_(entry["doctype"]))
        if reason:
            return _("{0} '{1}' can't be created: {2}.").format(_(entry["doctype"]), entry["name"], reason)
    return None


def start(plan) -> None:
    """Run the approved plan in the background, as the user who approved it. Each file's
    fingerprint is kept: a file that changes after the approval isn't imported."""
    approved = {s.name: s.validated_fingerprint for s in children(plan)}
    frappe.db.set_value(PLAN_DOCTYPE, plan.name, "stop_requested", 0, update_modified=False)
    _update(
        plan.name,
        status="Running",
        step=("Success", _("Approved: {0} files.").format(len(approved))),
        done=[],
        left_out={},
        current=None,
        approved=approved,
        user=frappe.session.user,
    )
    _enqueue(plan.name, "run_job", user=frappe.session.user)


def _enqueue(plan_name: str, method: str, **kwargs) -> None:
    from frappe_assistant_core.plugins.core import import_run

    frappe.enqueue(
        f"{__name__}.{method}",
        queue="long",
        timeout=4 * 60 * 60,
        job_id=PLAN_JOB.format(plan_name),
        enqueue_after_commit=True,
        now=import_run._runs_now(),
        plan_name=plan_name,
        **kwargs,
    )


def alive(plan) -> bool:
    """Whether a job of the plan, or of the file it is importing, is queued or running."""
    from frappe.utils.background_jobs import is_job_enqueued

    from frappe_assistant_core.plugins.core import import_run

    if is_job_enqueued(PLAN_JOB.format(plan.name)):
        return True
    current = _progress(plan).get("current")
    if not current or frappe.db.get_value(dry_run.SESSION_DOCTYPE, current, "status") != "Importing":
        return False
    session = frappe.get_doc(dry_run.SESSION_DOCTYPE, current)
    engine = dry_run.special_engine(session)
    if engine and getattr(engine, "JOB", None):
        return is_job_enqueued(engine.JOB.format(current))
    return bool(session.data_imports) and is_job_enqueued(
        import_run._job_id(session.data_imports[-1].data_import)
    )


def _guarded(plan_name: str, work) -> None:
    """Run a step of the plan. Whatever goes wrong in it ends the plan, saying why, instead of
    leaving it Running with nothing to carry it on."""
    frappe.db.savepoint(STEP_SAVEPOINT)
    try:
        work()
    except Exception as e:
        try:
            frappe.db.rollback(save_point=STEP_SAVEPOINT)
        except Exception:
            if frappe.flags.in_test:
                raise
            frappe.db.rollback()  # a commit inside the step released the savepoint
        frappe.log_error(title=_("FAC migration plan stopped"), message=frappe.get_traceback())
        _end(plan_name, "Failed" if isinstance(e, PlanProblem) else "Stopped", str(e) or type(e).__name__)
    finally:
        frappe.local.fac_plan_running = None


def run_job(plan_name: str, user: str) -> None:
    """Background job: the plan's masters, then its files."""
    frappe.set_user(user)  # nosemgrep: frappe-setuser — the job runs as the user who approved the plan

    def masters_then_files():
        create_planned(frappe.get_doc(PLAN_DOCTYPE, plan_name))
        _next_file(plan_name)

    _guarded(plan_name, masters_then_files)
    if not frappe.flags.in_test:
        frappe.db.commit()  # nosemgrep: frappe-manual-commit — a background job commits its own work


def child_finished(session_name: str) -> None:
    """A session's import ended in a background job: its plan goes on in a job of its own, queued
    once this one has committed, so the plan's next file never runs inside a file's job."""
    if getattr(frappe.local, "fac_plan_running", None):
        return  # the plan's own loop is running and goes on itself
    plan_name = frappe.db.get_value(dry_run.SESSION_DOCTYPE, session_name, "migration_plan")
    if not plan_name:
        return
    plan = frappe.get_doc(PLAN_DOCTYPE, plan_name)
    if plan.status != "Running" or _progress(plan).get("current") != session_name:
        return
    _enqueue(
        plan_name,
        "continue_job",
        session_name=session_name,
        user=_progress(plan).get("user") or frappe.session.user,
    )


def continue_job(plan_name: str, session_name: str, user: str) -> None:
    """Background job: record the file that ended, then the plan's next files."""
    frappe.set_user(user)  # nosemgrep: frappe-setuser — the job runs as the user who approved the plan

    def next_files():
        plan = frappe.get_doc(PLAN_DOCTYPE, plan_name)
        if plan.status != "Running" or _progress(plan).get("current") != session_name:
            return
        if _file_done(plan_name, session_name):
            _next_file(plan_name)

    _guarded(plan_name, next_files)
    if not frappe.flags.in_test:
        frappe.db.commit()  # nosemgrep: frappe-manual-commit — a background job commits its own work


def _next_file(plan_name: str) -> None:
    """Import the plan's files from the first not done, until one waits on a background job, the
    plan stops, or none is left."""
    frappe.local.fac_plan_running = plan_name
    try:
        while True:
            plan = frappe.get_doc(PLAN_DOCTYPE, plan_name)
            done = set(_progress(plan).get("done") or [])
            child = next((s for s in children(plan) if s.name not in done), None)
            if child is None:
                return _finish(plan_name)
            if plan.stop_requested:
                return _end(plan_name, "Stopped", _("Stopped before {0}, as asked.").format(child.file_name))
            # An error from here on rolls back this file's step only (_guarded): the files before it
            # stay imported, also when one job imports several (in the request, in developer mode).
            frappe.db.savepoint(STEP_SAVEPOINT)
            stop = _prepare(plan, child)
            if stop:
                return _end(plan_name, "Stopped", stop)
            _update(plan_name, current=child.name)
            try:
                waiting = _import(child)
            except Exception as e:
                return _end(
                    plan_name, "Stopped", _("{0} couldn't be imported: {1}").format(child.file_name, e)
                )
            if waiting:
                return  # its job calls child_finished when it ends
            if not _file_done(plan_name, child.name):
                return
    finally:
        frappe.local.fac_plan_running = None


def _prepare(plan, child) -> Optional[str]:
    """Check the file again just before its import and apply the failure rule. Returns why the plan
    stops, or None."""
    from frappe_assistant_core.plugins.core import import_fixes

    approved = (_progress(plan).get("approved") or {}).get(child.name)
    if child.status != "Ready to Import" or child.validated_fingerprint != approved:
        return _(
            "{0} changed after the plan was approved ({1} now), so it isn't imported. Check the plan "
            "again with plan_questions."
        ).format(child.file_name, _(child.status))
    import_fixes.check(child.name)
    child.reload()
    if child.status == "Dry Run Failed":
        groups = import_fixes.last_groups(child)
        count = sum(g.get("count") or 0 for g in groups.values())
        if plan.failure_rule == "stop":
            return _("{0}: {1} rows would fail now, and the plan stops when that happens.").format(
                child.file_name, count
            )
        fixes = [
            {
                "group": group_id,
                "action": import_fixes.LEAVE_OUT,
                "reason": _("Not imported: {0}").format(group.get("message") or group.get("cause")),
            }
            for group_id, group in groups.items()
        ]
        import_fixes.apply_fixes(child, groups, fixes)
        child.save()
        left_out = dict(_progress(plan).get("left_out") or {})
        left_out[child.name] = left_out.get(child.name, 0) + count
        _update(
            plan.name,
            step=(
                "Warning",
                _("{0}: {1} rows left out, as they would fail now.").format(child.file_name, count),
            ),
            left_out=left_out,
        )
        import_fixes.check(child.name)
        child.reload()
    if child.status not in ("Ready to Import", "Completed"):
        return _("{0} isn't ready to import ({1}).").format(child.file_name, _(child.status))
    return None


def _import(child) -> bool:
    """Start the file's import. True while it runs in a background job."""
    from frappe_assistant_core.plugins.core import import_run

    summary = frappe.parse_json(child.validation_summary or "{}") or {}
    if child.status == "Completed" or not summary.get("ready"):
        return False  # nothing left to import in it
    engine = dry_run.special_engine(child)
    if engine:
        engine.create(child)
    else:
        import_run.start(child)
    child.reload()
    return child.status == "Importing"


def _file_done(plan_name: str, session_name: str) -> bool:
    """Record the file as done. False when its import didn't end, and the plan stops."""
    child = frappe.get_doc(dry_run.SESSION_DOCTYPE, session_name)
    summary = frappe.parse_json(child.validation_summary or "{}") or {}
    if child.status not in IMPORTED and summary.get("ready"):
        last = child.steps[-1].message if child.steps else ""
        _end(plan_name, "Stopped", _("{0} didn't finish importing: {1}").format(child.file_name, last))
        return False
    plan = frappe.get_doc(PLAN_DOCTYPE, plan_name)
    done = list(_progress(plan).get("done") or [])
    if child.name not in done:
        done.append(child.name)
    _update(
        plan_name,
        step=("Success", _("{0}: {1}.").format(child.file_name, _(child.status))),
        done=done,
        current=None,
    )
    return True


def _finish(plan_name: str) -> None:
    plan = frappe.get_doc(PLAN_DOCTYPE, plan_name)
    files = children(plan)
    left_out = sum((_progress(plan).get("left_out") or {}).values())
    if all(s.status == "Failed" for s in files):
        status = "Failed"
    elif left_out or any(s.status != "Completed" for s in files):
        status = "Partially Completed"
    else:
        status = "Completed"
    message = _("{0} files imported.").format(len(files))
    if left_out:
        message += " " + _("{0} rows left out, as they would have failed.").format(left_out)
    _end(plan_name, status, message)


def _end(plan_name: str, status: str, message: str) -> None:
    outcome = {"Completed": "Success", "Partially Completed": "Warning"}.get(status, "Failed")
    _update(plan_name, status=status, step=(outcome, message), current=None)


def result(plan) -> Dict[str, Any]:
    """The run as FACO reports it: each file's state and counts, and what the plan says."""
    from frappe_assistant_core.plugins.core import import_failures

    left_out = _progress(plan).get("left_out") or {}
    files = []
    for session in children(plan):
        created = sum(run["created"] for run in import_failures.runs(session)) or len(session.created_records)
        files.append(
            {
                "session": session.name,
                "file_name": session.file_name,
                "doctype": session.target_doctype,
                "status": session.status,
                "created": created,
                "left_out": left_out.get(session.name, 0),
            }
        )
    return {
        "plan": plan.name,
        "status": plan.status,
        "files": files,
        "message": plan.steps[-1].message if plan.steps else "",
    }
