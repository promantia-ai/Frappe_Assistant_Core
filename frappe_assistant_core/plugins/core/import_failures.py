# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
The rows an import didn't get in (#33994): explained, tried again on their own, or handed
to the user as a file to correct.

A session's rows already imported are the ones in a success log of a Data Import made from
its current file. The dry run skips them and the next Data Import blanks them, so trying
again imports only the rest, from a new file with the fixes made since, and never touches a
record already in. A corrected file the user uploads (or a Google Sheet fetched again) is the
session's file from then on: a row of it that is the same as one an earlier file imported
counts as imported too, since a record with no key column (an Address) can't be stopped by
the dry run's duplicate check.

Every Data Import stays linked to the session, so the report covers every run.
"""

import csv
import io
import json
from collections import Counter
from typing import Any, Dict, List, Optional

import frappe
from frappe import _
from frappe.utils import cstr, get_url, strip_html

from frappe_assistant_core.plugins.core import import_dry_run as dry_run
from frappe_assistant_core.plugins.core import import_fixes
from frappe_assistant_core.plugins.core.import_session import read_sheet_rows, readable_session_file

#: The columns the failed-rows file adds after the user's own. A file sent back with them is
#: the same file: they are not imported.
ROW_COLUMN = "FACO Original Row"
REASON_COLUMN = "FACO Failure Reason"
ADDED_COLUMNS = (ROW_COLUMN, REASON_COLUMN)

#: Statuses whose failures come from the last Data Import, and from the last check.
AFTER_IMPORT = ("Partially Completed", "Failed")
AFTER_CHECK = ("Dry Run Failed", "Ready to Import")


class FailuresProblem(Exception):
    """There is no failed-rows file to give. The message is shown to the user."""


# -- what is already in -------------------------------------------------------------------


def imports_of_this_file(session) -> List[str]:
    """The session's Data Imports made from its current file, each once, oldest first."""
    names = []
    for link in session.data_imports:
        # A link from before source_file was kept is from the file the session had then.
        if (link.source_file or session.source_file) == session.source_file and link.data_import not in names:
            names.append(link.data_import)
    return names


def _success_rows(names: List[str]) -> set:
    rows = set()
    for indexes in frappe.get_all(
        "Data Import Log",
        filters={"data_import": ["in", names], "success": 1},
        pluck="row_indexes",
    ):
        rows.update(json.loads(indexes or "[]"))
    return rows


def imported_rows(session) -> set:
    """Rows of the session's current file already imported: in a success log of a Data
    Import made from it (every row of a record spread over several), or brought back from an
    earlier file of the session (see came_back)."""
    names = imports_of_this_file(session)
    return (_success_rows(names) if names else set()) | came_back(session)


def _row_key(header: List[str], row: List[str]) -> tuple:
    """A row as the user's own columns hold it: header and text, whatever their order."""
    return tuple(
        sorted(
            (cstr(name).strip(), cstr(row[i]).strip() if i < len(row) else "")
            for i, name in enumerate(own_columns(dry_run.unique_headers(header)))
        )
    )


def came_back(session) -> set:
    """Rows of the session's current file that a Data Import from an earlier file of the
    session already brought in: the same text in each of the user's columns, read from that
    import's own file (value maps applied, as here). A record with no key column, an Address
    say, can't be stopped as "Already in ERPNext", so without this a whole file sent again
    would import it twice. Each row imported counts once: two equal rows of the new file aren't
    both taken for one imported earlier."""
    earlier = [
        link.data_import
        for link in session.data_imports
        if link.source_file and link.source_file != session.source_file
    ]
    seen: Counter = Counter()
    for name in dict.fromkeys(earlier):
        success = _success_rows([name])
        rows = _import_file_rows(name) if success else []
        for number in success:
            if 2 <= number <= len(rows):
                seen[_row_key(rows[0], rows[number - 1])] += 1
    if not seen:
        return set()
    current = import_fixes.apply_value_maps(
        session, read_sheet_rows(readable_session_file(session), session.sheet or None)
    )
    rows = set()
    for number, row in enumerate(current[1:], start=2):
        key = _row_key(current[0], row)
        if seen[key] > 0 and any(text for _name, text in key):
            seen[key] -= 1
            rows.add(number)
    return rows


def _import_file_rows(data_import: str) -> List[List[str]]:
    """The rows of the file a Data Import imported: the session's sheet as FAC cleaned it, with
    the rows it didn't import blanked, so its row numbers are the success logs'."""
    file_url = frappe.db.get_value("Data Import", data_import, "import_file")
    files = frappe.get_all(
        "File",
        filters={"file_url": file_url, "attached_to_doctype": "Data Import", "attached_to_name": data_import},
        pluck="name",
        limit=1,
    )
    if not file_url or not files:
        return []
    return read_sheet_rows(frappe.get_doc("File", files[0]), None)


# -- explaining what failed -----------------------------------------------------------------


def failure_groups(session, logs) -> List[Dict[str, Any]]:
    """An import's failed records grouped by what ERPNext said, each explained as the dry run
    explains its groups: ``reason``, ``error``, ``count`` records, ``rows`` and ``explanation``."""
    by_key: Dict[tuple, Dict[str, Any]] = {}
    for log in logs:
        reason, error = log_reason(log.messages), _error_of(log.get("exception"))
        group = by_key.setdefault(
            (error, reason),
            {"cause": dry_run.CAUSE_REJECTED, "error": error, "message": reason, "count": 0, "rows": []},
        )
        group["count"] += 1
        group["rows"].extend(json.loads(log.row_indexes or "[]"))
    groups = []
    for group in sorted(by_key.values(), key=lambda g: -g["count"]):
        rows = sorted(set(group["rows"]))
        groups.append(
            {
                "reason": group["message"],
                "error": group["error"],
                "count": group["count"],
                "rows": rows,
                # Said in rows, as the dry run says it: the rows are what the user sees.
                "explanation": import_fixes.advise(session, {**group, "count": len(rows)})["explanation"],
            }
        )
    return groups


def log_reason(messages: Optional[str]) -> str:
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


def _error_of(traceback: Optional[str]) -> str:
    """The exception's class from a traceback's last line, as the dry run names it."""
    for line in reversed(cstr(traceback).strip().splitlines()):
        head = line.split(":", 1)[0].strip()
        if head and " " not in head:
            return head.rsplit(".", 1)[-1]
    return "Exception"


def next_steps() -> str:
    """What FACO offers once rows failed."""
    return _(
        "Explain each reason and fix what can be fixed in chat (create a missing record with "
        "create_document, or correct one with update_document). Then call dry_run_import: it "
        "checks only the rows not imported yet, and start_import imports just those. For rows "
        "the user would rather correct in the file, call dry_run_import with failed_rows_file."
    )


def runs(session) -> List[Dict[str, Any]]:
    """Every Data Import of the session once, oldest first, with its counts from its log."""
    names, purposes = [], {}
    for link in session.data_imports:
        if link.data_import not in purposes:
            names.append(link.data_import)
            purposes[link.data_import] = link.purpose
    if not names:
        return []
    out = {
        name: {"data_import": name, "purpose": purposes[name], "created": 0, "failed": 0} for name in names
    }
    # Counted here, not with "count(*)" in the query: Frappe v16 refuses SQL functions in fields.
    for row in frappe.get_all(
        "Data Import Log", filters={"data_import": ["in", names]}, fields=["data_import", "success"]
    ):
        out[row.data_import]["created" if row.success else "failed"] += 1
    return [out[name] for name in names]


# -- the failed-rows file -------------------------------------------------------------------


def failed_rows_file(session) -> Dict[str, Any]:
    """The rows still not imported, failed or left out, with the user's columns (the value
    maps applied), their row number and the reason, as a private CSV on the session."""
    if session.status == "Importing":
        raise FailuresProblem(_("The import is still running. Ask for the rows once it has ended."))
    reasons = _reasons(session)
    if not reasons:
        raise FailuresProblem(
            _("No row of this file is failing or left out, so there is nothing to download.")
        )

    source = readable_session_file(session)
    rows = import_fixes.apply_value_maps(session, read_sheet_rows(source, session.sheet or None))
    width = len(own_columns(rows[0]))
    out = [rows[0][:width] + list(ADDED_COLUMNS)]
    for number in sorted(reasons):
        if 1 <= number - 1 < len(rows):
            out.append((rows[number - 1] + [""] * width)[:width] + [str(number), reasons[number]])

    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerows([[cstr(value) for value in row] for row in out])
    file_name = f"{session.name}-failed-rows.csv"
    for old in frappe.get_all(
        "File",
        filters={
            "attached_to_doctype": dry_run.SESSION_DOCTYPE,
            "attached_to_name": session.name,
            "file_name": file_name,
        },
        pluck="name",
    ):
        frappe.delete_doc("File", old, force=True, ignore_permissions=True)
    file_doc = frappe.get_doc(
        {
            "doctype": "File",
            "file_name": file_name,
            "content": buffer.getvalue().encode("utf-8"),
            "is_private": 1,
            "attached_to_doctype": dry_run.SESSION_DOCTYPE,
            "attached_to_name": session.name,
        }
    ).insert(ignore_permissions=True)  # the session's own file, made for its user

    count = len(out) - 1
    session.log_step(
        "Failed Rows File",
        "Success",
        _("Gave {0} not imported as a file to correct: {1}.").format(
            _("1 row") if count == 1 else _("{0} rows").format(count), file_doc.file_name
        ),
    )
    session.save()
    return {
        "session": session.name,
        "rows": count,
        "file_url": file_doc.file_url,
        "download_url": get_url(file_doc.file_url),
        "message": _(
            "{0} rows, with the user's columns, then '{1}' (the row's number in the file) and "
            "'{2}'. Once corrected, the file can come back as it is: once uploaded, pass it to "
            "dry_run_import as file_id, and it is checked before anything is imported."
        ).format(count, ROW_COLUMN, REASON_COLUMN),
    }


def _reasons(session) -> Dict[int, str]:
    """Each row still not imported, failed or left out, with why: from the last Data Import
    once the import ended, from the last check before (or after a check of what is left)."""
    reasons: Dict[int, str] = {}
    engine = dry_run.special_engine(session)
    if engine:
        # Opening stock and opening invoices keep no Data Import Logs: they say themselves
        # where each row of the file stands.
        for outcome in engine.outcomes(session):
            if outcome["status"] == "excluded":
                reasons[outcome["row"]] = _("Left out: {0}").format(outcome["reason"])
            elif outcome["status"] == "failed":
                reasons[outcome["row"]] = outcome["reason"]
        return reasons
    if session.status in AFTER_IMPORT:
        names = imports_of_this_file(session)
        if names:
            for log in frappe.get_all(
                "Data Import Log",
                filters={"data_import": names[-1], "success": 0},
                fields=["row_indexes", "messages"],
                order_by="log_index",
            ):
                for row in json.loads(log.row_indexes or "[]"):
                    reasons[row] = log_reason(log.messages)
    elif session.status in AFTER_CHECK:
        results = dry_run._results_file(session)
        content = frappe.parse_json(results.get_content()) if results else {}
        messages = {
            group["id"]: group.get("message") or group["cause"] for group in content.get("groups") or []
        }
        for row in content.get("rows") or []:
            if row.get("status") == "failed":
                reasons[row["row"]] = "; ".join(messages.get(g, "") for g in row.get("groups") or [])
    for entry in import_fixes.left_out(session):
        reasons.setdefault(entry["row"], _("Left out: {0}").format(entry["reason"] or _("by the user")))
    for row in imported_rows(session):
        reasons.pop(row, None)
    return reasons


def own_columns(columns: List[str]) -> List[str]:
    """The user's columns: a file this module gave ends with its added ones."""
    columns = list(columns)
    while columns and cstr(columns[-1]).strip() in ADDED_COLUMNS:
        columns.pop()
    return columns
