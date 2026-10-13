# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
The dry run (#33990): every row of an import session's file checked exactly the way
Data Import would import it, with nothing written.

Rows are parsed by Data Import's own ImportFile and inserted by its own
Importer.insert_record (or update_record), each inside a savepoint that is rolled
back. That runs mandatory, link, duplicate and controller checks, which Data Import's
preview does not. Emails, background jobs, realtime events, commits and commit
callbacks are held back while a row is tried.

The full per-row list is attached to the session as a file; the model gets only the
grouped summary.
"""

import copy
import hashlib
import json
import re
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import frappe
import frappe.realtime
import frappe.utils.background_jobs
from frappe import _
from frappe.core.doctype.data_import.importer import INSERT, Importer, ImportFile, get_autoname_field
from frappe.utils import cstr, now_datetime, strip_html

from frappe_assistant_core.plugins.core.import_session import (
    FileProblem,
    get_readable_file,
    import_target_problem,
    read_sheet_rows,
    read_spreadsheet,
)

SESSION_DOCTYPE = "FAC Import Session"
BACKGROUND_ROWS = 500  # larger files are checked in a background job
PROGRESS_EVERY = 100
EXAMPLES_PER_GROUP = 3
STALE_AFTER_SECONDS = 2 * 60 * 60  # a run this old without finishing is taken as dead
SAVEPOINT = "fac_dry_run_row"
RUNNABLE_STATUSES = ("Mapped", "Dry Run Failed", "Ready to Import")

#: DocTypes whose insert can't be held back or rolled back. Sites add more with the
#: ``fac_dry_run_unsafe_doctypes`` site config key (a list of DocType names).
UNSAFE_DOCTYPES = {
    "DocType": "creating it changes the database schema, which can't be rolled back",
    "Custom Field": "creating it changes the database schema, which can't be rolled back",
    "Payment Request": "it can contact the payment gateway as it is created",
}

CAUSE_MISSING_LINK = "Missing link"
CAUSE_BAD_OPTION = "Not an allowed option"
CAUSE_BAD_DATE = "Wrong date format"
CAUSE_BAD_VALUE = "Invalid value"
CAUSE_DUPLICATE_IN_FILE = "Duplicate in the file"
CAUSE_ALREADY_EXISTS = "Already in ERPNext"
CAUSE_REJECTED = "Rejected by ERPNext"


class DryRunProblem(Exception):
    """The dry run can't start. The message is shown to the user."""


# -- can it run ------------------------------------------------------------------------


def check_can_run(session) -> None:
    """Raise DryRunProblem when ``session`` can't be checked yet, saying what to do."""
    if session.status == "File Read":
        raise DryRunProblem(_("Map the columns first, with set_column_mapping."))
    if session.status not in RUNNABLE_STATUSES:
        raise DryRunProblem(_("This session is {0}, so it can't be checked again.").format(_(session.status)))
    if not column_to_field_map(session):
        raise DryRunProblem(_("Map the columns first, with set_column_mapping."))
    problem = import_target_problem(session.target_doctype)
    if problem:
        raise DryRunProblem(problem)
    reason = unsafe_reason(session.target_doctype)
    if reason:
        raise DryRunProblem(
            _("A dry run can't be done for {0}: {1}. Import it with care.").format(
                session.target_doctype, reason
            )
        )
    if not session.source_file:
        raise DryRunProblem(
            _(
                "The file {0} is only in the chat, so its rows can't be checked here. Attach it "
                "in FAC Chat (or upload it to this site) and call dry_run_import again with its "
                "file_url. The mapping is kept."
            ).format(session.file_name)
        )


def unsafe_reason(doctype: str) -> Optional[str]:
    if doctype in UNSAFE_DOCTYPES:
        return _(UNSAFE_DOCTYPES[doctype])
    if doctype in (frappe.conf.get("fac_dry_run_unsafe_doctypes") or []):
        return _("this site marks its side effects as ones that can't be held back")
    return None


def link_site_file(session, file_url: str) -> None:
    """Give a session whose file stayed in the chat the same file, uploaded to the site."""
    file_doc = get_readable_file(file_url)
    read = read_spreadsheet(file_doc, session.sheet or None)
    expected = _session_columns(session)
    if expected and read.chosen.columns != expected:
        raise FileProblem(
            _("This file's columns don't match the session's. Expected: {0}. Found: {1}.").format(
                ", ".join(expected), ", ".join(read.chosen.columns)
            )
        )
    session.source_file = file_doc.name
    session.file_source = "Site File"
    session.sheets = frappe.as_json([s.as_dict() for s in read.sheets])
    session.log_step(
        "Link File", "Success", _("Linked {0} from this site for checking.").format(file_doc.file_name)
    )
    session.save()


def row_count(session) -> int:
    for sheet in frappe.parse_json(session.sheets or "[]") or []:
        if sheet.get("name") == session.sheet:
            return int(sheet.get("row_count") or 0)
    return 0


def is_running(session) -> bool:
    progress = frappe.parse_json(session.progress or "{}") or {}
    if progress.get("step") != "Dry Run" or progress.get("state") not in ("queued", "running"):
        return False
    started = progress.get("started_on")
    if not started:
        return True
    age = (now_datetime() - frappe.utils.get_datetime(started)).total_seconds()
    return age < STALE_AFTER_SECONDS


def column_to_field_map(session) -> Dict[str, str]:
    options = frappe.parse_json(session.template_options or "{}") or {}
    return dict(options.get("column_to_field_map") or {})


def _session_columns(session) -> List[str]:
    for sheet in frappe.parse_json(session.sheets or "[]") or []:
        if sheet.get("name") == session.sheet:
            return [str(c) for c in sheet.get("columns") or []]
    return []


# -- background ------------------------------------------------------------------------


def enqueue(session) -> None:
    session.db_set("progress", _progress("queued", 0, row_count(session)), update_modified=False)
    frappe.enqueue(
        "frappe_assistant_core.plugins.core.import_dry_run.run_job",
        queue="long",
        timeout=4 * 60 * 60,
        job_id=f"fac_dry_run::{session.name}",
        deduplicate=True,
        enqueue_after_commit=True,
        session_name=session.name,
        user=frappe.session.user,
    )


def run_job(session_name: str, user: str) -> None:
    """Background job: the dry run as the user who asked for it."""
    frappe.set_user(user)
    try:
        run(session_name, commit_progress=True)
        frappe.db.commit()
    except Exception as e:
        frappe.db.rollback()
        session = frappe.get_doc(SESSION_DOCTYPE, session_name)
        session.progress = _progress("failed", 0, row_count(session))
        session.log_step("Dry Run", "Failed", _("The check stopped: {0}").format(_message_of(e)))
        session.save()
        frappe.db.commit()
        frappe.log_error(title=_("FAC Import dry run failed"), message=frappe.get_traceback())


def _progress(state: str, done: int, total: int) -> str:
    return frappe.as_json(
        {"step": "Dry Run", "state": state, "done": done, "total": total, "started_on": str(now_datetime())}
    )


# -- the dry run -----------------------------------------------------------------------


def run(session_name: str, commit_progress: bool = False) -> Dict[str, Any]:
    """Check every row of the session's file. Stores the result on the session and
    returns the summary, with up to three example rows per group for the model."""
    session = frappe.get_doc(SESSION_DOCTYPE, session_name)
    file_doc = frappe.get_doc("File", session.source_file)
    rows = read_sheet_rows(file_doc, session.sheet or None)
    import_file = _parse(session, file_doc, rows)
    payloads = import_file.get_payloads_for_import()

    checker = _Checker(session, import_file.header)
    excluded = _excluded_rows(session)
    total = len(payloads)
    for done, payload in enumerate(payloads, start=1):
        checker.check(payload, excluded)
        if commit_progress and done % PROGRESS_EVERY == 0:
            frappe.db.set_value(
                SESSION_DOCTYPE,
                session_name,
                "progress",
                _progress("running", done, total),
                update_modified=False,
            )
            frappe.db.commit()

    return _store(session_name, file_doc, checker, total)


def _parse(session, file_doc, rows) -> ImportFile:
    options = frappe._dict(frappe.parse_json(session.template_options or "{}") or {})
    options.column_to_field_map = frappe._dict(options.get("column_to_field_map") or {})
    try:
        return _SessionRows(session.target_doctype, file_doc.file_url, rows, options, session.import_type)
    except frappe.ValidationError as e:
        raise FileProblem(_message_of(e)) from e


class _SessionRows(ImportFile):
    """Data Import's own parser, given the rows of the session's chosen sheet."""

    def __init__(self, doctype, file_url, rows, template_options, import_type):
        self._rows = rows
        super().__init__(doctype, file_url, template_options, import_type)

    def get_data_from_template_file(self):
        return self._rows


def _excluded_rows(session) -> set:
    value = frappe.parse_json(session.excluded_rows or "[]") or []
    return {int(row) for row in value if isinstance(row, (int, str)) and str(row).isdigit()}


class _Checker:
    """Checks payloads one by one and collects the groups and the per-row list."""

    def __init__(self, session, header):
        self.doctype = session.target_doctype
        self.import_type = session.import_type or INSERT
        self.header = header
        # Importer's insert_record and update_record read only these attributes.
        self.importer = SimpleNamespace(
            doctype=self.doctype,
            import_type=self.import_type,
            data_import=frappe._dict(
                doctype=SESSION_DOCTYPE,
                name=session.name,
                submit_after_import=session.submit_after_import,
            ),
        )
        self.key_df = _key_field(session, header)
        self.seen_keys: Dict[str, int] = {}
        self.groups: Dict[Tuple, Dict[str, Any]] = {}
        self.rows: List[Dict[str, Any]] = []

    def check(self, payload, excluded: set) -> None:
        row_numbers = [row.row_number for row in payload.rows]
        if excluded.intersection(row_numbers):
            self.rows += [{"row": n, "status": "excluded"} for n in row_numbers]
            return

        problems = self._parse_problems(payload)
        if not problems:
            problems = self._duplicate_problems(payload)
        if not problems:
            problems = self._insert_problems(payload)

        if not problems:
            self.rows += [{"row": n, "status": "ready"} for n in row_numbers]
            return
        ids = [self._group(key, info, payload) for key, info in problems]
        self.rows += [{"row": n, "status": "failed", "groups": ids} for n in row_numbers]

    # Data Import's own value checks, made while it parsed the rows.
    def _parse_problems(self, payload) -> List[Tuple[Tuple, Dict]]:
        problems = []
        for row in payload.rows:
            for warning in row.warnings:
                if warning.get("type") == "info":
                    continue
                problems.append(self._from_warning(row, warning))
        return problems

    def _from_warning(self, row, warning) -> Tuple[Tuple, Dict]:
        field = frappe._dict(warning.get("field") or {})
        if not field.fieldname:
            message = strip_html(cstr(warning.get("message")))
            return (CAUSE_BAD_VALUE, message), {"cause": CAUSE_BAD_VALUE, "message": message}

        value = self._value_of(row, field)
        if field.fieldtype == "Link":
            cause = CAUSE_MISSING_LINK
            message = _("{0} '{1}' doesn't exist.").format(_(field.options), value)
        elif field.fieldtype == "Select":
            cause = CAUSE_BAD_OPTION
            message = _("'{0}' isn't one of the options for {1}.").format(value, _(field.label))
        elif field.fieldtype in ("Date", "Datetime"):
            cause = CAUSE_BAD_DATE
            message = strip_html(cstr(warning.get("message")))
        else:
            cause = CAUSE_BAD_VALUE
            message = strip_html(cstr(warning.get("message")))
        info = {
            "cause": cause,
            "field": field.fieldname,
            "label": _(field.label),
            "value": value,
            "message": message,
        }
        return (cause, field.parent, field.fieldname, value), info

    def _value_of(self, row, field) -> str:
        for column in self.header.columns:
            df = column.df
            if (
                df
                and not column.skip_import
                and df.fieldname == field.fieldname
                and df.parent == field.parent
            ):
                return cstr(row.data[column.index]).strip()
        return ""

    def _duplicate_problems(self, payload) -> List[Tuple[Tuple, Dict]]:
        if not self.key_df:
            return []
        value = cstr(payload.doc.get(self.key_df.fieldname)).strip()
        if not value:
            return []
        label = _(self.key_df.label)
        first = self.seen_keys.setdefault(value.casefold(), payload.rows[0].row_number)
        if first != payload.rows[0].row_number:
            message = _("{0} '{1}' is also in row {2}.").format(label, value, first)
            info = {"cause": CAUSE_DUPLICATE_IN_FILE, "field": self.key_df.fieldname, "label": label}
            return [((CAUSE_DUPLICATE_IN_FILE, self.key_df.fieldname), {**info, "message": message})]
        if self.import_type == INSERT and frappe.db.exists(self.doctype, {self.key_df.fieldname: value}):
            info = {"cause": CAUSE_ALREADY_EXISTS, "field": self.key_df.fieldname, "label": label}
            message = _("A {0} with {1} '{2}' already exists.").format(_(self.doctype), label, value)
            return [((CAUSE_ALREADY_EXISTS, self.key_df.fieldname), {**info, "message": message})]
        return []

    # The real insert (or update), rolled back.
    def _insert_problems(self, payload) -> List[Tuple[Tuple, Dict]]:
        doc = frappe._dict(copy.deepcopy(payload.doc))
        frappe.clear_messages()
        with held_back():
            frappe.db.savepoint(SAVEPOINT)
            try:
                if self.import_type == INSERT:
                    Importer.insert_record(self.importer, doc)
                else:
                    Importer.update_record(self.importer, doc)
                return []
            except Exception as e:
                message = _message_of(e)
                kind = type(e).__name__
                return [
                    (
                        (CAUSE_REJECTED, kind, message),
                        {"cause": CAUSE_REJECTED, "error": kind, "message": message},
                    )
                ]
            finally:
                frappe.db.rollback(save_point=SAVEPOINT)
                frappe.clear_messages()

    def _group(self, key: Tuple, info: Dict, payload) -> str:
        group = self.groups.get(key)
        if not group:
            group = {"id": f"G{len(self.groups) + 1}", **info, "count": 0, "rows": []}
            if info["cause"] in (CAUSE_DUPLICATE_IN_FILE, CAUSE_ALREADY_EXISTS):
                group["message"] = {
                    CAUSE_DUPLICATE_IN_FILE: _("{0} appears more than once in the file."),
                    CAUSE_ALREADY_EXISTS: _("A {1} with this {0} already exists."),
                }[info["cause"]].format(info["label"], _(self.doctype))
            self.groups[key] = group
        for row in payload.rows:
            group["count"] += 1
            group["rows"].append(row.row_number)
        return group["id"]


def _key_field(session, header):
    """The field that names a record, used to find duplicates, when a column maps to it."""
    meta = frappe.get_meta(session.target_doctype)
    autoname = get_autoname_field(session.target_doctype)
    mapped = {
        column.df.fieldname: column.df
        for column in header.columns
        if column.df and not column.skip_import and column.df.parent == session.target_doctype
    }
    for fieldname in (session.match_key, autoname.fieldname if autoname else None, meta.title_field):
        if fieldname and fieldname in mapped:
            return mapped[fieldname]
    return None


_TEMPORARY_NAME = re.compile(r"\bnew-[a-z0-9-]+-[a-z0-9]{6,}\b")


def _message_of(e: Exception) -> str:
    """The message ERPNext showed for ``e``, as plain text, without per-row temporary names."""
    messages = []
    for entry in frappe.local.message_log or []:
        if isinstance(entry, str):
            try:
                entry = json.loads(entry)
            except ValueError:
                entry = {"message": entry}
        if isinstance(entry, dict) and entry.get("message"):
            messages.append(cstr(entry["message"]))
    text = messages[-1] if messages else cstr(e) or type(e).__name__
    text = " ".join(strip_html(text).split())
    return _TEMPORARY_NAME.sub("", text).strip()


@contextmanager
def held_back():
    """Hold back what a row's insert would send out or commit while it is tried."""
    flags = frappe.local.flags
    saved_flags = {name: flags.get(name) for name in ("in_import", "mute_emails")}
    # Data Import sets in_import (which also stops webhooks); emails are always muted.
    flags.in_import = True
    flags.mute_emails = True

    callbacks = {
        name: list(getattr(frappe.db, name)._functions)
        for name in ("before_commit", "after_commit", "before_rollback", "after_rollback")
    }

    def swallow(*args, **kwargs):
        return None

    patched = [
        (frappe, "enqueue"),
        (frappe.utils.background_jobs, "enqueue"),
        (frappe, "publish_realtime"),
        (frappe.realtime, "publish_realtime"),
    ]
    originals = [(owner, name, getattr(owner, name)) for owner, name in patched]
    for owner, name in patched:
        setattr(owner, name, swallow)
    frappe.db.commit = swallow  # a controller that commits must not keep the row
    try:
        yield
    finally:
        del frappe.db.commit
        for owner, name, original in originals:
            setattr(owner, name, original)
        for name, functions in callbacks.items():
            manager = getattr(frappe.db, name)
            manager._functions.clear()
            manager._functions.extend(functions)
        for name, value in saved_flags.items():
            flags[name] = value


# -- storing the result ----------------------------------------------------------------


def _store(session_name: str, file_doc, checker: _Checker, total_payloads: int) -> Dict[str, Any]:
    session = frappe.get_doc(SESSION_DOCTYPE, session_name)
    counts = {"ready": 0, "failed": 0, "excluded": 0}
    for row in checker.rows:
        counts[row["status"]] += 1
    failed_rows = {row["row"] for row in checker.rows if row["status"] == "failed"}

    previous = _previous_failed_rows(session)
    groups = list(checker.groups.values())
    summary = {
        "checked_on": str(now_datetime()),
        "doctype": session.target_doctype,
        "import_type": session.import_type,
        "total_rows": len(checker.rows),
        "records": total_payloads,
        **counts,
        "duplicate_key": checker.key_df.fieldname if checker.key_df else None,
        "groups": [_summary_group(g) for g in groups],
    }
    if previous is not None:
        summary["previous"] = {"failed": len(previous), "fixed": len(previous - failed_rows)}

    _replace_results_file(
        session,
        {"session": session.name, "summary": summary, "groups": groups, "rows": checker.rows},
    )
    session.validation_summary = frappe.as_json(summary)
    session.validated_fingerprint = fingerprint(session, file_doc)
    session.status = "Dry Run Failed" if counts["failed"] else "Ready to Import"
    session.progress = frappe.as_json(
        {"step": "Dry Run", "state": "done", "done": total_payloads, "total": total_payloads}
    )
    session.log_step(
        "Dry Run",
        "Warning" if counts["failed"] else "Success",
        headline(summary),
        rows_ok=counts["ready"],
        rows_failed=counts["failed"],
    )
    session.save()
    return summary


def _summary_group(group: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: v for k, v in group.items() if k != "rows"}
    out["example_rows"] = group["rows"][:EXAMPLES_PER_GROUP]
    return out


def headline(summary: Dict[str, Any]) -> str:
    text = _("{0} rows ready, {1} would fail").format(summary["ready"], summary["failed"])
    if summary.get("excluded"):
        text += _(", {0} left out").format(summary["excluded"])
    return text + "."


def _previous_failed_rows(session) -> Optional[set]:
    file_doc = _results_file(session)
    if not file_doc:
        return None
    try:
        rows = json.loads(file_doc.get_content()).get("rows") or []
    except Exception:
        return None
    return {row["row"] for row in rows if row.get("status") == "failed"}


def _results_file(session):
    if not session.validation_results:
        return None
    name = frappe.db.get_value(
        "File",
        {
            "file_url": session.validation_results,
            "attached_to_doctype": SESSION_DOCTYPE,
            "attached_to_name": session.name,
        },
    )
    return frappe.get_doc("File", name) if name else None


def _replace_results_file(session, content: Dict[str, Any]) -> None:
    old = _results_file(session)
    if old:
        frappe.delete_doc("File", old.name, force=True, ignore_permissions=True)
    file_doc = frappe.get_doc(
        {
            "doctype": "File",
            "file_name": f"{session.name}-dry-run.json",
            "content": json.dumps(content, default=str, indent=1),
            "is_private": 1,
            "attached_to_doctype": SESSION_DOCTYPE,
            "attached_to_name": session.name,
            "attached_to_field": "validation_results",
        }
    ).insert(ignore_permissions=True)
    session.validation_results = file_doc.file_url


def fingerprint(session, file_doc) -> str:
    """Changes when anything the check depended on changes, so an import can tell
    whether it is importing what was checked."""
    content_hash = file_doc.content_hash or hashlib.sha256(_bytes(file_doc.get_content())).hexdigest()
    parts = [
        content_hash,
        session.sheet or "",
        session.target_doctype,
        session.import_type or "",
        str(session.submit_after_import or 0),
        json.dumps(frappe.parse_json(session.template_options or "{}"), sort_keys=True),
        json.dumps(frappe.parse_json(session.transformation_rules or "null"), sort_keys=True),
        json.dumps(sorted(_excluded_rows(session))),
    ]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def _bytes(content) -> bytes:
    return content.encode("utf-8") if isinstance(content, str) else content


def examples(session, groups: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Up to three example rows per group, read from the file for the reply only."""
    wanted = {row for group in groups for row in group["example_rows"]}
    if not wanted:
        return {}
    file_doc = frappe.get_doc("File", session.source_file)
    rows = read_sheet_rows(file_doc, session.sheet or None)
    header, mapped = rows[0], column_to_field_map(session)
    shown = [i for i in range(len(header)) if mapped.get(str(i)) not in (None, "Don't Import")]
    by_row = {}
    for number in wanted:
        if 1 <= number - 1 < len(rows):
            by_row[number] = {header[i]: rows[number - 1][i] for i in shown}
    return {
        group["id"]: [{"row": n, "values": by_row[n]} for n in group["example_rows"] if n in by_row]
        for group in groups
    }
