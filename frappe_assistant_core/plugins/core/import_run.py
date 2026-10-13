# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Importing a checked session through Frappe's own Data Import (#33993).

The file that passed the dry run is written as a private CSV of the session's sheet, with the
value maps applied and the rows left out blanked: Data Import skips a blank row and keeps the
others' numbers, so its log names the user's own rows. A Data Import is made from that file and
started the way Desk's Start Import does, so it shows in Desk with its file and log like any
other. Data Import commits each record and rolls back only a failed one, so a partial success
is normal; a re-run skips the rows already imported, which is how an interrupted import
continues without duplicates. That is why a session keeps its one Data Import and never makes
another file for it.
"""

import csv
import io
import json
import os
from typing import Any, Dict, List, Optional, Tuple

import frappe
from frappe import _
from frappe.core.doctype.data_import.importer import INSERT
from frappe.utils import cstr, strip_html
from frappe.utils.background_jobs import enqueue, is_job_enqueued
from frappe.utils.scheduler import is_scheduler_inactive

from frappe_assistant_core.plugins.core import import_dry_run as dry_run
from frappe_assistant_core.plugins.core import import_fixes
from frappe_assistant_core.plugins.core.import_session import import_target_problem, read_sheet_rows

READY = "Ready to Import"
IMPORTING = "Importing"
UNFINISHED = (IMPORTING, "Partially Completed", "Failed")  # continued, or tried again
FAILED_GROUPS_SHOWN = 5
START_SAVEPOINT = "fac_start_import"

#: The session's status once its Data Import has ended, by the Data Import's status.
FINISHED_STATUS = {"Success": "Completed", "Partial Success": "Partially Completed", "Error": "Failed"}


class ImportProblem(Exception):
    """The import can't start or continue. The message is shown to the user."""


# -- can it run --------------------------------------------------------------------------


def cannot_import(doctype: str) -> str:
    """Why the current user can't import into ``doctype`` now, or "" when they can."""
    problem = import_target_problem(doctype)
    if problem:
        return problem
    if not frappe.has_permission("Data Import", "create"):
        return _(
            "Importing goes through Data Import, which you can't use. A System Manager can import "
            "this file, or give you the Data Import permission."
        )
    if is_scheduler_inactive() and not _runs_now():
        return _("The scheduler is turned off, so imports can't run. A System Manager can turn it on.")
    return ""


def _runs_now() -> bool:
    """As Data Import decides: in tests and developer mode the import runs in the request."""
    return bool(frappe.flags.in_test or frappe.conf.developer_mode)


def check_card(session, doctype: str, rows: Any, import_type: str) -> None:
    """The approval card shows what FACO passed: refuse unless it is what the last check found."""
    summary = frappe.parse_json(session.validation_summary or "{}") or {}
    checked = (session.target_doctype, summary.get("ready"), session.import_type or INSERT)
    asked = (cstr(doctype).strip(), _int(rows), cstr(import_type).strip())
    if asked != checked:
        raise ImportProblem(
            _(
                "The last check found {1} {0} rows to import as {2}, not what was passed. Pass "
                "those, so the approval card shows them."
            ).format(*checked)
        )


def _int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# -- starting and continuing -------------------------------------------------------------------


def start(session) -> str:
    """Import a session that passed the dry run: the cleaned file, a Data Import, started."""
    if session.status != READY or not import_fixes.checked_as_is(session):
        raise ImportProblem(
            _(
                "The file isn't ready, or it changed since its last check. Check it with "
                "dry_run_import first: only a file that passed is imported."
            )
        )
    for link in session.data_imports:
        # A remap and a new check can make an imported session ready again; importing it again
        # would duplicate every record that went in.
        if frappe.db.exists("Data Import Log", {"data_import": link.data_import, "success": 1}):
            raise ImportProblem(
                _("This file was already imported, in Data Import {0}.").format(link.data_import)
            )

    frappe.db.savepoint(START_SAVEPOINT)
    file_doc = None
    try:
        file_doc = _cleaned_file(session)
        # Saving it attaches the file to it, as Frappe does for an Attach field.
        data_import = frappe.get_doc(
            {
                "doctype": "Data Import",
                "reference_doctype": session.target_doctype,
                "import_type": session.import_type or INSERT,
                "import_file": file_doc.file_url,
                "template_options": frappe.as_json(
                    {"column_to_field_map": dry_run.column_to_field_map(session)}
                ),
                "mute_emails": 1,
                "submit_after_import": session.submit_after_import,
            }
        ).insert()
        _record_start(session, data_import.name, "Import")
    except Exception:
        # Nothing half made stays behind: no Data Import without its session's record of it.
        frappe.db.rollback(save_point=START_SAVEPOINT)
        if file_doc:
            _remove_from_disk(file_doc)
        raise
    _launch(session, data_import.name)
    return data_import.name


def resume(session) -> Tuple[str, str]:
    """Continue the session's Data Import: "running" if it still is; "finished" if it had
    ended (its result is recorded); else it is started again ("continued"), skipping the
    records already imported and trying the failed ones again."""
    if not session.data_imports:
        raise ImportProblem(_("This session has no import to continue."))
    name = session.data_imports[-1].data_import
    if is_job_enqueued(_job_id(name)):
        return name, "running"
    di = frappe.get_doc("Data Import", name)
    ended = di.status in FINISHED_STATUS and _log_count(name) >= (di.payload_count or 0)
    if di.status == "Success" or (ended and session.status == IMPORTING):
        # Run to its end from Desk, or its result never recorded: record it, don't run again.
        record_result(session.name, name)
        return name, "finished"
    _record_start(session, name, "Retry")
    _launch(session, name)
    return name, "continued"


def _record_start(session, data_import: str, purpose: str) -> None:
    session.append("data_imports", {"data_import": data_import, "purpose": purpose, "status": "Pending"})
    session.status = IMPORTING
    session.progress = frappe.as_json({"step": "Import", "state": "running", "data_import": data_import})
    message = (
        _("Started Data Import {0}.") if purpose == "Import" else _("Continued Data Import {0}.")
    ).format(data_import)
    session.log_step("Import", "Success", message)
    session.save()


def _launch(session, data_import: str) -> None:
    """Run the Data Import as Desk's Start Import does: same job, queue and timeout."""
    if _runs_now():
        # The import runs in this request, and the importer undoes a failed record with a full
        # rollback: what this request wrote must be saved first, as Desk's Start Import has it.
        frappe.db.commit()  # nosemgrep: frappe-manual-commit — see the comment above
    enqueue(
        "frappe_assistant_core.plugins.core.import_run.run_job",
        queue="default",
        timeout=10000,
        event="data_import",
        job_id=_job_id(data_import),
        enqueue_after_commit=True,  # the job reads the Data Import this request saved
        now=_runs_now(),
        data_import=data_import,
        session_name=session.name,
    )


def _job_id(data_import: str) -> str:
    return f"data_import::{data_import}"  # Data Import's own, so Desk sees it running


def run_job(data_import: str, session_name: str) -> None:
    """Background job: Data Import's own run, then its result recorded on the session."""
    from frappe.core.doctype.data_import.data_import import start_import

    start_import(data_import)
    record_result(session_name, data_import)
    frappe.db.commit()  # nosemgrep: frappe-manual-commit — a background job commits its own work


def _log_count(data_import: str) -> int:
    return frappe.db.count("Data Import Log", {"data_import": data_import})


def _remove_from_disk(file_doc) -> None:
    """A File rolled back with its savepoint leaves its content on disk."""
    path = file_doc.get_full_path()
    if os.path.exists(path) and not frappe.db.exists("File", {"file_url": file_doc.file_url}):
        os.remove(path)


# -- the cleaned file -----------------------------------------------------------------------


def _cleaned_file(session):
    """The session's sheet as a private CSV: value maps applied, records left out blanked (every
    row of them, so no child row is left to join another record). It is saved unattached, for
    the Data Import made from it to take."""
    source = frappe.get_doc("File", session.source_file)
    rows = import_fixes.apply_value_maps(session, read_sheet_rows(source, session.sheet or None))
    excluded = dry_run._excluded_rows(session)
    left_out = set()
    for payload in dry_run._parse(session, source, rows).get_payloads_for_import():
        numbers = {row.row_number for row in payload.rows}
        if numbers & excluded:
            left_out |= numbers
    width = len(rows[0])
    cleaned = [rows[0]] + [
        [""] * width if number in left_out else row for number, row in enumerate(rows[1:], start=2)
    ]

    # Data Import refuses the whole file for one value it can't use, so stop here if any row
    # would now fail that way (a master deleted since the check, say).
    warnings = [w for w in dry_run._parse(session, source, cleaned).get_warnings() if w.get("type") != "info"]
    if warnings:
        raise ImportProblem(
            _(
                "Since the last check, ERPNext would refuse some rows ({0}). Check the file again with dry_run_import."
            ).format(_warning_text(warnings[0]))
        )

    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerows([[cstr(value) for value in row] for row in cleaned])
    return frappe.get_doc(
        {
            "doctype": "File",
            "file_name": f"{session.name}-import.csv",
            "content": buffer.getvalue().encode("utf-8"),
            "is_private": 1,
        }
    ).insert()


def _warning_text(warning: Dict[str, Any]) -> str:
    text = " ".join(strip_html(cstr(warning.get("message"))).split())
    row = warning.get("row")
    return _("row {0}: {1}").format(row, text) if row else text


# -- the result ------------------------------------------------------------------------------


def record_result(session_name: str, data_import: str) -> Dict[str, Any]:
    """Where the Data Import ended, recorded on the session: its status, the counts on its
    Data Import row, and the step log. Returns the result, as FACO reports it."""
    session = frappe.get_doc(dry_run.SESSION_DOCTYPE, session_name)
    result = result_of(session, data_import)
    link = next((d for d in reversed(session.data_imports) if d.data_import == data_import), None)
    if link:
        link.status, link.success_count, link.failed_count = (
            result["data_import_status"],
            result["created"],
            result["failed"],
        )
    session.status = result["session_status"]
    session.progress = frappe.as_json(
        {"step": "Import", "state": result["state"], "data_import": data_import}
    )
    outcome = {"finished": "Success" if not result["failed"] else "Warning"}.get(result["state"], "Failed")
    session.log_step(
        "Import", outcome, result["message"], rows_ok=result["created"], rows_failed=result["failed"]
    )
    session.save()
    return result


def result_of(session, data_import: str) -> Dict[str, Any]:
    """The Data Import's result in the session's terms: created, failed and skipped counts, the
    failed rows by reason, the rows left out, and what FACO says."""
    di = frappe.get_doc("Data Import", data_import)
    logs = frappe.get_all(
        "Data Import Log",
        filters={"data_import": data_import},
        fields=["success", "row_indexes", "messages"],
        order_by="log_index",
    )
    created = sum(1 for log in logs if log.success)
    failed = _failed_by_reason([log for log in logs if not log.success])
    failed_count = sum(group["count"] for group in failed)
    left_out = import_fixes.left_out_report(session)
    skipped = sum(group["count"] for group in left_out)
    ended = di.status in FINISHED_STATUS and len(logs) >= (di.payload_count or 0)

    doctype = _(di.reference_doctype)
    done = _("Imported") if (di.import_type or INSERT) == INSERT else _("Updated")
    # Data Import's last run refused the whole file before importing a record (it clears these
    # at the start of every run).
    warnings = [w for w in (frappe.parse_json(di.template_warnings or "[]") or []) if w.get("type") != "info"]
    if warnings and not created:
        state, session_status = "refused", "Mapped"
        message = _(
            "ERPNext refused the file before importing anything ({0}). Check it again with dry_run_import."
        ).format(_warning_text(warnings[0]))
    elif warnings:
        state, session_status = "refused", IMPORTING
        message = _(
            "ERPNext now refuses some rows ({0}), so the import can't continue. Fix that (restore "
            "a deleted record, say), then ask to continue: the {1} {2} records imported stay."
        ).format(_warning_text(warnings[0]), created, doctype)
    elif ended:
        state, session_status = "finished", FINISHED_STATUS[di.status]
        message = _("{0} {1} {2} records into ERPNext.").format(done, created, doctype)
    else:
        state, session_status = "interrupted", IMPORTING
        message = _(
            "The import stopped after {0} of {1} {2} records ({3}). Ask to continue: records already imported are skipped."
        ).format(len(logs), di.payload_count or 0, doctype, _(di.status))
    if failed:
        reasons = "; ".join(
            _("{0}: {1}").format(_rows_text(group["rows"]), group["reason"])
            for group in failed[:FAILED_GROUPS_SHOWN]
        )
        message += " " + _("{0} failed ({1}).").format(failed_count, reasons)
    if skipped:
        message += " " + _("{0} left out ({1}).").format(
            _("1 row") if skipped == 1 else _("{0} rows").format(skipped),
            "; ".join(
                "{}: {}".format(
                    group["reason"],
                    (_("row {0}") if group["count"] == 1 else _("rows {0}")).format(group["rows"]),
                )
                for group in left_out
            ),
        )
    message += " " + _("Data Import {0}.").format(data_import)

    return {
        "state": state,
        "session_status": session_status,
        "data_import": data_import,
        "data_import_status": di.status,
        "desk_url": f"/app/data-import/{data_import}",
        "total": di.payload_count or 0,
        "created": created,
        "failed": failed_count,
        "skipped": skipped,
        "failed_rows": failed,
        "left_out": left_out,
        "message": message,
    }


def _failed_by_reason(logs) -> List[Dict[str, Any]]:
    """The failed records by reason: ``count`` records (as ``created`` counts them), and the
    rows they were on."""
    by_reason: Dict[str, Dict[str, Any]] = {}
    for log in logs:
        group = by_reason.setdefault(_reason(log.messages), {"count": 0, "rows": []})
        group["count"] += 1
        group["rows"].extend(json.loads(log.row_indexes or "[]"))
    return [
        {"reason": reason, **group}
        for reason, group in sorted(by_reason.items(), key=lambda pair: -pair[1]["count"])
    ]


def _reason(messages: Optional[str]) -> str:
    """The last message ERPNext showed for a failed record, as plain text."""
    texts = []
    for entry in json.loads(messages or "[]") or []:
        if isinstance(entry, str):
            try:
                entry = json.loads(entry)
            except ValueError:
                entry = {"message": entry}
        if isinstance(entry, dict) and entry.get("message"):
            texts.append(" ".join(strip_html(cstr(entry["message"])).split()))
    return texts[-1] if texts else _("ERPNext refused it")


def _rows_text(rows: List[int]) -> str:
    return (_("row {0}") if len(rows) == 1 else _("rows {0}")).format(import_fixes.row_ranges(rows))
