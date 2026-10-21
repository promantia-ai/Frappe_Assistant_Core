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
import threading
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import frappe
import frappe.realtime
import frappe.utils.background_jobs
from frappe import _
from frappe.core.doctype.data_import.importer import (
    INSERT,
    INVALID_VALUES,
    Importer,
    ImportFile,
    get_autoname_field,
)
from frappe.utils import cstr, now_datetime, strip_html

from frappe_assistant_core.plugins.core.import_session import (
    FileProblem,
    import_target_problem,
    read_sheet_rows,
    read_spreadsheet,
    readable_session_file,
    row_limit_problem,
)

SESSION_DOCTYPE = "FAC Import Session"
BACKGROUND_ROWS = 500  # larger files are checked in a background job
PROGRESS_EVERY = 100
EXAMPLES_PER_GROUP = 3
STALE_AFTER_SECONDS = 2 * 60 * 60  # a run this old without finishing is taken as dead
SAVEPOINT = "fac_dry_run_row"
SAMPLES_KEPT = 5  # records whose old and new values an update's check shows
# After an import, what it didn't get in is checked again on its own (#33994): rows that failed,
# or a corrected file of them, or of the rows left out of a completed one.
RUNNABLE_STATUSES = (
    "Mapped",
    "Dry Run Failed",
    "Ready to Import",
    "Partially Completed",
    "Failed",
    "Completed",
)

_UNSET = object()

#: DocTypes whose insert can't be held back or rolled back. Sites add more with the
#: ``fac_dry_run_unsafe_doctypes`` site config key (a list of DocType names).
UNSAFE_DOCTYPES = {
    "DocType": "creating it changes the database schema, which can't be rolled back",
    "Custom Field": "creating it changes the database schema, which can't be rolled back",
    "Payment Request": "it can contact the payment gateway as it is created",
    "File": "saving it can move or write the file on disk, which can't be rolled back",
    "Website Theme": "saving it deletes and writes the theme's CSS files, which can't be rolled back",
}

CAUSE_MISSING_LINK = "Missing link"
CAUSE_LATER_FILE = "Comes from a later file"  # a migration plan's file imported after this one
CAUSE_BAD_OPTION = "Not an allowed option"
CAUSE_BAD_DATE = "Wrong date format"
CAUSE_BAD_VALUE = "Invalid value"
CAUSE_DUPLICATE_IN_FILE = "Duplicate in the file"
CAUSE_ALREADY_EXISTS = "Already in ERPNext"
CAUSE_REJECTED = "Rejected by ERPNext"
# An update (#34005): the row's record isn't found, is found more than once, or another row has it.
CAUSE_NO_MATCH = "No match"
CAUSE_MANY_MATCHES = "More than one match"
CAUSE_SAME_RECORD = "Same record twice"


class DryRunProblem(Exception):
    """The dry run can't start. The message is shown to the user."""


# -- can it run ------------------------------------------------------------------------


def check_status(session) -> None:
    """Raise DryRunProblem when ``session``'s status lets nothing be checked, or a file linked."""
    if session.status == "File Read":
        raise DryRunProblem(_("Map the columns first, with set_column_mapping."))
    if session.status not in RUNNABLE_STATUSES:
        raise DryRunProblem(_("This session is {0}, so it can't be checked again.").format(_(session.status)))


def special_engine(session):
    """The engine of a session that isn't imported through Data Import (opening stock, opening
    invoices, opening balances), or None. Each keeps its settings under its own key in
    template_options."""
    from frappe_assistant_core.plugins.core import import_opening_balances as opening_balances
    from frappe_assistant_core.plugins.core import import_opening_invoices as opening_invoices
    from frappe_assistant_core.plugins.core import import_opening_stock as opening_stock

    for engine in (opening_stock, opening_invoices, opening_balances):
        if engine.settings(session):
            return engine
    return None


def check_can_run(session) -> None:
    """Raise DryRunProblem when ``session`` can't be checked yet, saying what to do."""
    problem = row_limit_problem(row_count(session))  # a corrected file can be larger
    if problem:
        raise DryRunProblem(problem)
    engine = special_engine(session)
    if engine:
        return engine.check_can_run(session)
    check_status(session)
    if not column_to_field_map(session):
        raise DryRunProblem(_("Map the columns first, with set_column_mapping."))
    problem = import_target_problem(session.target_doctype, session.import_type or INSERT)
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
                "The file {0} is only in the chat, so its rows can't be checked here. Upload it "
                "in File Manager (or attach it in FAC Chat) and call dry_run_import again with "
                "its file_id. The mapping is kept."
            ).format(session.file_name)
        )


def unsafe_reason(doctype: str) -> Optional[str]:
    if doctype in UNSAFE_DOCTYPES:
        return _(UNSAFE_DOCTYPES[doctype])
    if doctype in (frappe.conf.get("fac_dry_run_unsafe_doctypes") or []):
        return _("this site marks its side effects as ones that can't be held back")
    return None


def link_site_file(session, file_doc, sheet_url: Optional[str] = None) -> None:
    """Give a session whose file stayed in the chat the same file, uploaded to the site; or a
    corrected one, as the failed-rows file came back (#33994), or a fresh copy of its Google
    Sheet (#34006): the same columns, perhaps with the ones that file added after them, which
    are not imported. ``file_doc`` is a File the user may read (find_site_file), or the copy
    ``sheet_url`` was read into."""
    from frappe_assistant_core.plugins.core.import_failures import own_columns

    try:
        read = read_spreadsheet(file_doc, session.sheet or None)
    except FileProblem:
        # A corrected file saved from the CSV FACO gave has its own sheet name: its columns decide.
        read = read_spreadsheet(file_doc)
    expected = _session_columns(session)
    columns = read.chosen.columns
    if expected and own_columns(columns) != own_columns(expected):
        raise FileProblem(
            _("This file's columns don't match the session's. Expected: {0}. Found: {1}.").format(
                ", ".join(own_columns(expected)), ", ".join(columns)
            )
        )
    # Refused before the session changes: it keeps its file, and the rows it leaves out (#34003).
    problem = row_limit_problem(read.chosen.row_count)
    if problem:
        raise FileProblem(problem)
    if expected:
        options = frappe.parse_json(session.template_options or "{}") or {}
        mapping = {
            k: v for k, v in (options.get("column_to_field_map") or {}).items() if int(k) < len(columns)
        }
        for position in range(len(own_columns(columns)), len(columns)):
            mapping[str(position)] = "Don't Import"
        options["column_to_field_map"] = mapping
        session.template_options = frappe.as_json(options)
    for link in session.data_imports:
        # A Data Import recorded before source_file was kept is of the file the session had.
        link.source_file = link.source_file or session.source_file
    session.source_file = file_doc.name
    session.file_source = "Google Sheet" if sheet_url else "Site File"
    session.source_url = sheet_url
    session.sheet = read.chosen.name  # a workbook saved as CSV, say, has no sheet names
    session.sheets = frappe.as_json([s.as_dict() for s in read.sheets])
    if sheet_url:
        linked = _("Copied the Google Sheet again for checking.")
    else:
        linked = _("Linked {0} from this site for checking.").format(file_doc.file_name)
    session.log_step("Link File", "Success", linked)
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


def check_running() -> str:
    return _(
        "This file is being checked in the background. Wait for the check to finish (call "
        "dry_run_import to see where it is), then try again."
    )


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
    from frappe_assistant_core.plugins.core.import_fixes import check

    frappe.set_user(user)  # nosemgrep: frappe-setuser — the job runs as the user who asked for the check
    try:
        check(session_name, commit_progress=True)  # the run, explained for the model to read later
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
    from frappe_assistant_core.plugins.core.import_failures import imported_rows
    from frappe_assistant_core.plugins.core.import_fixes import apply_value_maps

    session = frappe.get_doc(SESSION_DOCTYPE, session_name)
    file_doc = readable_session_file(session)
    # What is checked is the session as it is now: a change saved while the check runs (a remap
    # in another request) must not be stamped as checked when the result is stored.
    _checking()[session_name] = fingerprint(session, file_doc)
    engine = special_engine(session)
    if engine:
        return engine.run(session_name, commit_progress=commit_progress)
    from frappe_assistant_core.plugins.core import import_update

    rows = apply_value_maps(session, read_sheet_rows(file_doc, session.sheet or None))
    matches, column_map = None, None
    if import_update.is_update(session) and import_update.match_fields(session):
        matches = import_update.match_records(session, rows)
        single = {n: names[0] for n, names in matches.items() if len(names) == 1}
        rows, column_map = import_update.with_ids(session, rows, single)
    import_file = _parse(session, file_doc, rows, column_map)
    payloads = import_file.get_payloads_for_import()

    from frappe_assistant_core.plugins.core import import_plan

    checker = _Checker(
        session,
        import_file.header,
        matches,
        coming=import_plan.planned_names(session),
        later=import_plan.later_names(session),
    )
    excluded = _excluded_rows(session)
    imported = imported_rows(session)
    total = len(payloads)
    for done, payload in enumerate(payloads, start=1):
        checker.check(payload, excluded, imported)
        if commit_progress and done % PROGRESS_EVERY == 0:
            frappe.db.set_value(
                SESSION_DOCTYPE,
                session_name,
                "progress",
                _progress("running", done, total),
                update_modified=False,
            )
            frappe.db.commit()  # nosemgrep: frappe-manual-commit — a background check commits its progress

    return _store(session_name, file_doc, checker, total)


def only_mapped(column_map: Dict[str, str], width: int) -> Dict[str, str]:
    """``column_map`` with every one of ``width`` columns it doesn't map set to Don't Import.
    Data Import maps a column it isn't told about by its header, so a column the user left
    unmapped would otherwise be checked and imported anyway."""
    out = dict(column_map)
    for position in range(width):
        if not out.get(str(position)):
            out[str(position)] = "Don't Import"
    return out


def unique_headers(header: List[Any]) -> List[str]:
    """The heading row with a repeated heading made unique ("Name", "Name (2)"): Data Import
    skips a column whose heading it has seen, even one mapped by its position. Positions, and
    so the mapping, are unchanged."""
    seen, out = set(), []
    for title in header:
        text = cstr(title).strip()
        unique, n = text, 1
        while unique in seen:
            n += 1
            unique = f"{text} ({n})"
        seen.add(unique)
        out.append(unique if unique != text else title)
    return out


def _parse(session, file_doc, rows, column_map=None) -> ImportFile:
    if rows:
        rows = [unique_headers(rows[0]), *rows[1:]]
    options = frappe._dict(frappe.parse_json(session.template_options or "{}") or {})
    if column_map is not None:
        options.column_to_field_map = column_map
    options.column_to_field_map = frappe._dict(
        only_mapped(options.get("column_to_field_map") or {}, len(rows[0]) if rows else 0)
    )
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

    def __init__(self, session, header, matches=None, coming=None, later=None):
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
        self.matches = matches  # None unless updating on match fields: row → matching records
        self.seen_records: Dict[str, int] = {}
        # A row may link to a record a row higher up the file creates (a tree's parent): Data
        # Import is run a level at a time for such a file (import_run), so that link is no problem.
        self.self_links = _self_links(self.doctype) if self.import_type == INSERT and matches is None else []
        self.planned: Dict[str, Dict[str, Any]] = {}  # casefolded name → the ready row that creates it
        # In a migration plan: DocType → casefolded name → the earlier file (or the plan) that creates
        # it, and the same for the files imported after this one (import_plan).
        self.coming: Dict[str, Dict[str, str]] = coming or {}
        self.later: Dict[str, Dict[str, str]] = later or {}
        self.preview = {"records_changing": 0, "fields_changing": 0, "by_field": {}, "samples": []}
        if matches is not None:
            from frappe_assistant_core.plugins.core import import_update

            self.key_label = import_update.key_label(session)

    def check(self, payload, excluded: set, imported: set = frozenset()) -> None:
        row_numbers = [row.row_number for row in payload.rows]
        if imported.intersection(row_numbers):  # already in ERPNext, from an earlier import
            self.rows += [{"row": n, "status": "imported"} for n in row_numbers]
            return
        if excluded.intersection(row_numbers):
            self.rows += [{"row": n, "status": "excluded"} for n in row_numbers]
            return

        needs = self._needs(payload)
        from_files = self._from_earlier_files(payload)
        problems = self._match_problems(payload) if self.matches is not None else []
        if not problems:
            problems = self._parse_problems(payload, needs, from_files)
        if not problems:
            problems = self._duplicate_problems(payload)
        record, changed = self._record(payload), []
        if not problems and record:
            from frappe_assistant_core.plugins.core import import_update

            changed = import_update.changes(self.doctype, record, payload.doc)
            if not changed:  # Data Import would fail it: "No changes to update"
                self.rows += [{"row": n, "status": "unchanged", "record": record} for n in row_numbers]
                return
        created = None
        if not problems:
            problems, created = self._insert_problems(payload, needs, from_files)
        if record and _no_changes(problems):  # Frappe saw no change the plan did: nothing to do
            self.rows += [{"row": n, "status": "unchanged", "record": record} for n in row_numbers]
            return

        if not problems:
            extra = {"record": record} if record else {}
            if needs:  # imported once the rows it needs are (import_run)
                extra["after"] = sorted(entry["row"] for entry in needs)
            if from_files:  # imported once those files are (import_plan)
                extra["after_files"] = sorted({link["file"] for link in from_files.values()})
            if created:  # what a later file of a migration plan may link to
                extra["name"] = created
            self.rows += [{"row": n, "status": "ready", **extra} for n in row_numbers]
            if created and self.self_links:
                self.planned[created.casefold()] = {
                    "row": row_numbers[0],
                    "name": created,
                    "doc": _with_links(payload.doc, needs),
                    "needs": needs,
                }
            if record:
                self._add_to_preview(payload.rows[0].row_number, record, changed)
            return
        ids = [self._group(key, info, payload) for key, info in problems]
        # A failing row still names the record it means to create, when its name comes from one of
        # its fields: a later file of the plan counts it as coming. Whether it comes is settled when
        # its file is imported (the fix the user picks, else the plan's failure rule).
        named = self._intended_name(payload)
        extra = {"name": named} if named else {}
        self.rows += [{"row": n, "status": "failed", "groups": ids, **extra} for n in row_numbers]

    # An update: the row's record, found on the match fields.
    def _record(self, payload) -> Optional[str]:
        if self.matches is None:
            return None
        names = self.matches.get(payload.rows[0].row_number) or []
        return names[0] if len(names) == 1 else None

    def _match_problems(self, payload) -> List[Tuple[Tuple, Dict]]:
        names = self.matches.get(payload.rows[0].row_number) or []
        if len(names) == 1:
            return []
        info = {"label": self.key_label}
        if not names:
            message = _("No {0} has this {1}.").format(_(self.doctype), self.key_label)
            return [((CAUSE_NO_MATCH,), {**info, "cause": CAUSE_NO_MATCH, "message": message})]
        key = tuple(sorted(names))
        message = _("{0} {1} records have this {2}: {3}.").format(
            len(names), _(self.doctype), self.key_label, ", ".join(key)
        )
        return [
            (
                (CAUSE_MANY_MATCHES, *key),
                {**info, "cause": CAUSE_MANY_MATCHES, "records": list(key), "message": message},
            )
        ]

    def _add_to_preview(self, row: int, record: str, changed: List[Dict]) -> None:
        self.preview["records_changing"] += 1
        self.preview["fields_changing"] += len(changed)
        for change in changed:
            self.preview["by_field"][change["label"]] = self.preview["by_field"].get(change["label"], 0) + 1
        if len(self.preview["samples"]) < SAMPLES_KEPT:
            self.preview["samples"].append({"row": row, "record": record, "changes": changed})

    # Data Import's own value checks, made while it parsed the rows.
    def _parse_problems(self, payload, needs=(), from_files=None) -> List[Tuple[Tuple, Dict]]:
        problems = []
        planned = {entry["name"].casefold() for entry in needs}
        for row in payload.rows:
            for warning in row.warnings:
                if (
                    warning.get("type") == "info"
                    or self._from_the_file(row, warning, planned)
                    or self._link_key(row, warning) in (from_files or {})
                ):
                    continue
                problems.append(self._from_warning(row, warning))
        return problems

    def _from_warning(self, row, warning) -> Tuple[Tuple, Dict]:
        field = frappe._dict(warning.get("field") or {})
        if not field.fieldname:
            message = strip_html(cstr(warning.get("message")))
            return (CAUSE_BAD_VALUE, message), {"cause": CAUSE_BAD_VALUE, "message": message}

        value = self._value_of(row, field)
        later = self.later.get(field.options, {}).get(value.casefold()) if field.fieldtype == "Link" else None
        if later:
            cause = CAUSE_LATER_FILE
            message = _(
                "{0} '{1}' comes from {2}, which is imported after this file. Move that file first "
                "(plan_migration's order)."
            ).format(_(field.options), value, later)
        elif field.fieldtype == "Link":
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
            "parent": field.parent,  # the DocType the field is on: a child table's, for its columns
            "field": field.fieldname,
            "label": _(field.label),
            "value": value,
            "message": message,
        }
        return (cause, field.parent, field.fieldname, value), info

    def _from_the_file(self, row, warning, planned: set) -> bool:
        """Whether ``warning`` is Data Import's "missing" for a record a row higher up the file
        creates, which is in ERPNext by the time this row is imported."""
        field = frappe._dict(warning.get("field") or {})
        return bool(
            planned
            and field.fieldtype == "Link"
            and field.options == self.doctype
            and field.parent == self.doctype
            and self._value_of(row, field).casefold() in planned
        )

    def _intended_name(self, payload) -> Optional[str]:
        """The name an insert of this row would give its record, when it is a field of the row."""
        if self.import_type != INSERT or self.matches is not None:
            return None
        autoname = get_autoname_field(self.doctype)
        if not autoname:
            return None
        for column in self.header.columns:
            df = column.df
            if (
                df
                and not column.skip_import
                and df.parent == self.doctype
                and df.fieldname == autoname.fieldname
            ):
                return cstr(payload.rows[0].data[column.index]).strip() or None
        return None

    # Links to records the earlier files of a migration plan create.
    def _from_earlier_files(self, payload) -> Dict[Tuple[str, str], Dict[str, str]]:
        """(DocType, casefolded value) → the earlier file of the plan that creates that record and
        the field that links to it, for each of this record's links to one. Read from the rows
        themselves: Data Import leaves a link it found missing out of the record it parsed."""
        found: Dict[Tuple[str, str], Dict[str, str]] = {}
        if not self.coming:
            return found
        for row in payload.rows:
            for column in self.header.columns:
                df = column.df
                if not df or column.skip_import or df.fieldtype != "Link" or df.options not in self.coming:
                    continue
                value = cstr(row.data[column.index]).strip()
                source = self.coming[df.options].get(value.casefold()) if value else None
                if source:
                    found[(df.options, value.casefold())] = {
                        "doctype": df.options,
                        "file": source,
                        "field": df.fieldname,
                        "parent": df.parent,
                        "value": value,
                    }
        return found

    def _put_back(self, payload, doc, from_files) -> None:
        """Put back in ``doc`` the links to records earlier files of the plan create, which Data
        Import's parse left out: on the record, and on each child row they are in."""
        counters: Dict[str, int] = {}
        for row in payload.rows:
            for doctype, table_df in self.header.doctypes:
                indexes = self.header.get_column_indexes(doctype, table_df)
                if all(value in INVALID_VALUES for value in row.get_values(indexes)):
                    continue  # no record of this table on this row
                target = doc
                if table_df:
                    at = counters.get(table_df.fieldname, 0)
                    counters[table_df.fieldname] = at + 1
                    rows = doc.get(table_df.fieldname) or []
                    target = rows[at] if at < len(rows) else None
                if target is None:
                    continue
                for column in self.header.get_columns(indexes):
                    df = column.df
                    if df.fieldtype != "Link":
                        continue
                    value = cstr(row.data[column.index]).strip()
                    if (df.options, value.casefold()) in from_files:
                        target[df.fieldname] = value

    def _link_key(self, row, warning) -> Optional[Tuple[str, str]]:
        field = frappe._dict(warning.get("field") or {})
        if field.fieldtype != "Link":
            return None
        return (field.options, self._value_of(row, field).casefold())

    # Links to records rows higher up the file create.
    def _needs(self, payload) -> List[Dict[str, Any]]:
        """The ready rows higher up the file whose records this row links to, each with the field
        that links. Read from the row itself: Data Import leaves a link it found missing out of
        the record it parsed."""
        if not self.planned:
            return []
        needs, rows = [], set()
        for fieldname in self.self_links:
            field = frappe._dict(fieldname=fieldname, parent=self.doctype)
            value = self._value_of(payload.rows[0], field)
            entry = self.planned.get(value.casefold()) if value else None
            if entry and (fieldname, entry["row"]) not in rows:
                rows.add((fieldname, entry["row"]))
                needs.append({**entry, "field": fieldname})
        return needs

    @staticmethod
    def _higher_up(needs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """The records of ``needs`` and of the rows they need in turn, the topmost first."""
        ordered, seen = [], set()

        def visit(entry):
            if entry["row"] in seen:
                return
            seen.add(entry["row"])
            for parent in entry["needs"]:
                visit(parent)
            ordered.append(entry["doc"])

        for entry in needs:
            visit(entry)
        return ordered

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
        if self.matches is not None:  # an update: two rows for one record
            record = self._record(payload)
            if not record:
                return []
            first = self.seen_records.setdefault(record, payload.rows[0].row_number)
            if first == payload.rows[0].row_number:
                return []
            message = _("Row {0} updates the same {1}, {2}.").format(first, _(self.doctype), record)
            return [
                (
                    (CAUSE_SAME_RECORD,),
                    {"cause": CAUSE_SAME_RECORD, "label": self.key_label, "message": message},
                )
            ]
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

    # The real insert (or update), rolled back. Returns the problems and the name it created.
    def _insert_problems(
        self, payload, needs=(), from_files=None
    ) -> Tuple[List[Tuple[Tuple, Dict]], Optional[str]]:
        doc = frappe._dict(copy.deepcopy(_with_links(payload.doc, needs)))
        frappe.clear_messages()
        with held_back():
            frappe.db.savepoint(SAVEPOINT)
            try:
                if self.import_type != INSERT:
                    Importer.update_record(self.importer, doc)
                    return [], None
                # The records rows higher up the file create are in ERPNext when this row is
                # imported: put them in first, inside the same savepoint.
                for earlier in self._higher_up(list(needs)):
                    Importer.insert_record(self.importer, frappe._dict(copy.deepcopy(earlier)))
                if from_files:
                    # Records an earlier file of the plan creates aren't in ERPNext yet: stand-ins
                    # take their place inside the savepoint, and the links Data Import's parse left
                    # out are put back.
                    for link in from_files.values():
                        _stand_in(link["doctype"], link["value"])
                    self._put_back(payload, doc, from_files)
                    # The parse cached them as missing; the rollback clears this cache too.
                    frappe.db.value_cache.clear()
                return [], Importer.insert_record(self.importer, doc).name
            except Exception as e:
                message = _message_of(e)
                kind = type(e).__name__
                return [
                    (
                        (CAUSE_REJECTED, kind, message),
                        {"cause": CAUSE_REJECTED, "error": kind, "message": message},
                    )
                ], None
            finally:
                frappe.db.rollback(save_point=SAVEPOINT)
                frappe.clear_messages()

    def _group(self, key: Tuple, info: Dict, payload) -> str:
        group = self.groups.get(key)
        if not group:
            group = {"id": self._group_id(key), **info, "count": 0, "rows": []}
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

    def _group_id(self, key: Tuple) -> str:
        """The same failure gets the same id in every check, so a fix picked from an earlier
        reply still finds its rows, and is refused once they no longer fail."""
        digest = hashlib.sha256(json.dumps(key, default=str).encode()).hexdigest()
        taken = {group["id"] for group in self.groups.values()}
        size = 6
        while f"G{digest[:size]}" in taken:
            size += 2
        return f"G{digest[:size]}"


def _stand_in(doctype: str, name: str) -> None:
    """A bare ``doctype`` record called ``name``, for a row's check to link to while an earlier file
    of its migration plan, which creates the real one, isn't imported yet. Only ever made inside the
    row's savepoint, which is rolled back."""
    if frappe.db.exists(doctype, name):
        return
    from frappe_assistant_core.plugins.core import import_fixes

    meta = frappe.get_meta(doctype)
    doc = frappe.new_doc(doctype)
    name_field = import_fixes._name_field(meta)
    if name_field:
        doc.set(name_field, name)
    parent_field = import_fixes._parent_field(meta)
    if parent_field and not doc.get(parent_field):
        doc.set(parent_field, import_fixes.default_parent(doctype))
    doc.insert(ignore_permissions=True, ignore_links=True, ignore_mandatory=True, set_name=name)


def _with_links(doc, needs) -> Dict[str, Any]:
    """``doc`` with the links to records rows higher up the file create put back in."""
    return {**doc, **{entry["field"]: entry["name"] for entry in needs}}


def _self_links(doctype: str) -> List[str]:
    """The fields of ``doctype`` that link to ``doctype`` itself, as a tree's parent does."""
    meta = frappe.get_meta(doctype)
    return [df.fieldname for df in meta.fields if df.fieldtype == "Link" and df.options == doctype]


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


#: What a row's insert could send out at once: held back by held_back(), in its request only.
_HELD_FUNCTIONS = (
    (frappe, "enqueue"),
    (frappe.utils.background_jobs, "enqueue"),
    (frappe, "publish_realtime"),
    (frappe.realtime, "publish_realtime"),
)
_HOLD_LOCK = threading.Lock()


def _hold_aware(original):
    """``original``, doing nothing while this request holds back (held_back)."""

    def hold_aware(*args, **kwargs):
        if getattr(frappe.local, "fac_holding_back", 0):
            return None
        return original(*args, **kwargs)

    hold_aware.__wrapped__ = original
    hold_aware.fac_hold_aware = True
    return hold_aware


def _install_hold_aware() -> None:
    """Put the hold-aware stand-ins in place, once: any other request still gets the real thing."""
    with _HOLD_LOCK:
        for owner, name in _HELD_FUNCTIONS:
            current = getattr(owner, name)
            if getattr(current, "fac_hold_aware", False) is not True:  # a Mock answers any attribute
                setattr(owner, name, _hold_aware(current))


@contextmanager
def held_back():
    """Hold back what a row's insert would send out or commit while it is tried. The caller rolls
    back to a savepoint inside it; the work the row registered for a rollback (a File moved back,
    a cache cleared) runs on the way out, as a full rollback would run it."""
    flags = frappe.local.flags
    saved_flags = {name: flags.get(name) for name in ("in_import", "mute_emails")}
    # Data Import sets in_import (which also stops webhooks); emails are always muted.
    flags.in_import = True
    flags.mute_emails = True
    # A saved document queues its search entry in Redis, which no rollback reaches.
    conf = frappe.local.conf
    search_setting = conf.get("disable_global_search", _UNSET)
    conf["disable_global_search"] = True

    callbacks = {
        name: list(getattr(frappe.db, name)._functions)
        for name in ("before_commit", "after_commit", "before_rollback", "after_rollback")
    }

    def swallow(*args, **kwargs):
        return None

    # Jobs and realtime events are module functions, shared by every request a worker serves at
    # once: they are held back for this request only, never swapped out for the whole process.
    _install_hold_aware()
    frappe.local.fac_holding_back = getattr(frappe.local, "fac_holding_back", 0) + 1
    # A controller that commits must not keep the row (frappe.db is this request's own). What was
    # in place before goes back after: the class's commit, or a stand-in set around this check.
    before = frappe.db.__dict__.get("commit")
    frappe.db.commit = swallow
    try:
        yield
    finally:
        if before is None:
            del frappe.db.commit
        else:
            frappe.db.commit = before
        frappe.local.fac_holding_back -= 1
        rolled_back = list(frappe.db.after_rollback._functions)[len(callbacks["after_rollback"]) :]
        for name, functions in callbacks.items():
            manager = getattr(frappe.db, name)
            manager._functions.clear()
            manager._functions.extend(functions)
        for callback in rolled_back:
            try:
                callback()
            except Exception:
                frappe.log_error(title=_("Dry Run Rollback Error"), message=frappe.get_traceback())
        if search_setting is _UNSET:
            conf.pop("disable_global_search", None)
        else:
            conf["disable_global_search"] = search_setting
        for name, value in saved_flags.items():
            flags[name] = value


# -- storing the result ----------------------------------------------------------------


def _checking() -> Dict[str, str]:
    """Session -> the fingerprint of what this request's check started from."""
    if not hasattr(frappe.local, "fac_dry_run_checking"):
        frappe.local.fac_dry_run_checking = {}
    return frappe.local.fac_dry_run_checking


def _store(session_name: str, file_doc, checker: _Checker, total_payloads: int) -> Dict[str, Any]:
    session = frappe.get_doc(SESSION_DOCTYPE, session_name)
    counts = {"ready": 0, "failed": 0, "excluded": 0, "imported": 0, "unchanged": 0}
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
    if getattr(checker, "matches", None) is not None:  # the opening engines' checkers have none
        summary["update"] = checker.preview
    if previous is not None:
        summary["previous"] = {"failed": len(previous), "fixed": len(previous - failed_rows)}

    _replace_results_file(
        session,
        {"session": session.name, "summary": summary, "groups": groups, "rows": checker.rows},
    )
    session.validation_summary = frappe.as_json(summary)
    session.validated_fingerprint = _checking().pop(session_name, None) or fingerprint(session, file_doc)
    session.status = "Dry Run Failed" if counts["failed"] else "Ready to Import"
    if counts["imported"] and not counts["ready"] and not counts["failed"]:
        session.status = "Completed"  # the rest is left out or already so: nothing left to do
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


def _no_changes(problems) -> bool:
    """Whether ``problems`` is Data Import's refusal of an update that changes nothing."""
    return len(problems) == 1 and problems[0][1].get("message") == _("No changes to update")


def _summary_group(group: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: v for k, v in group.items() if k != "rows"}
    out["example_rows"] = group["rows"][:EXAMPLES_PER_GROUP]
    return out


def headline(summary: Dict[str, Any]) -> str:
    text = _("{0} rows ready, {1} would fail").format(summary["ready"], summary["failed"])
    if summary.get("excluded"):
        text += _(", {0} left out").format(summary["excluded"])
    if summary.get("imported"):
        text += _(", {0} already imported").format(summary["imported"])
    if summary.get("unchanged"):
        text += _(", {0} unchanged").format(summary["unchanged"])
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
        session.match_key or "",
        str(session.submit_after_import or 0),
        json.dumps(_checked_options(session), sort_keys=True),
        json.dumps(frappe.parse_json(session.transformation_rules or "null"), sort_keys=True),
        json.dumps(sorted(_excluded_rows(session))),
    ]
    from frappe_assistant_core.plugins.core.import_failures import imported_rows

    imported = imported_rows(session)
    if imported:  # an import since the check makes it stale: other rows are left to import
        parts.append(json.dumps(sorted(imported)))
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


#: What other steps keep in template_options that no check reads: the undo's plan and result
#: (preview_import_undo), and the failures of the last opening invoices import (reset as an
#: import starts). The records an import made stay in: the check leaves those rows out.
_NOT_CHECKED = {"undo": None, "opening_invoices": ("failed",)}


def _checked_options(session) -> Dict[str, Any]:
    """The session's template_options as far as the check depends on them."""
    options = dict(frappe.parse_json(session.template_options or "{}") or {})
    for key, inner in _NOT_CHECKED.items():
        if inner is None:
            options.pop(key, None)
        elif isinstance(options.get(key), dict):
            options[key] = {k: v for k, v in options[key].items() if k not in inner}
    return options


def _bytes(content) -> bytes:
    return content.encode("utf-8") if isinstance(content, str) else content


def examples(session, groups: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Up to three example rows per group, read from the file for the reply only."""
    wanted = {row for group in groups for row in group["example_rows"]}
    if not wanted:
        return {}
    file_doc = readable_session_file(session)
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
