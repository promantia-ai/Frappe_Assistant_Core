# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Undo an import (#34002): what a session created, and whether it can be undone now.

The undo set is the session's Created Records plus, for an Insert New Records session, the
records its Data Imports logged as created. This module only reads; the blockers, the delete
order and the job are added on top of it.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional, Set, Tuple

import frappe
from frappe import _
from frappe.utils import get_datetime

INSERT = "Insert New Records"
UPDATE = "Update Existing Records"
UNDO_DAYS = 30  # default of the site config key fac_import_undo_days


@dataclass
class Record:
    doctype: str
    name: str
    parent: Optional[str] = None


class UndoProblem(Exception):
    """Something the user is told about, as it is worded."""


def finished_at(session) -> datetime:
    """When the session's last import step finished: its last successful Import step. A session
    with no Import step (created masters only) uses its latest step of any kind, and one with
    no steps its creation time. Step timestamps are written once, so this does not move when
    the session is saved."""
    imports = [
        get_datetime(s.timestamp)
        for s in session.steps
        if s.step == "Import" and s.outcome == "Success" and s.timestamp
    ]
    if imports:
        return max(imports)
    stamps = [get_datetime(s.timestamp) for s in session.steps if s.timestamp]
    return max(stamps) if stamps else get_datetime(session.creation)


def _days() -> int:
    return int(frappe.conf.get("fac_import_undo_days", UNDO_DAYS))


def window_ends(session) -> datetime:
    return finished_at(session) + timedelta(days=_days())


def refusal(session) -> str:
    """Why the session can't be undone now, or "" when it can."""
    if session.status == "Importing":
        return _("This session is still importing. Wait for the import to finish, then try again.")
    if session.status == "Undoing":
        return _("This import is already being undone.")
    if session.status == "Undone":
        return _("This import was already undone.")
    if not session.created_records and not session.data_imports:
        return _("This session has nothing to undo: it has not imported or created anything.")
    if frappe.utils.now_datetime() > window_ends(session):
        return _(
            "This import finished on {0}, more than {1} days ago, so it can no longer be undone here."
        ).format(finished_at(session).date(), _days())
    return ""


def _exists(doctype: str, name: str) -> bool:
    return bool(frappe.db.exists(doctype, name))


def undo_set(session) -> Tuple[List[Record], List[Record], Optional[str]]:
    """(present, already removed, note): the records the session created. A record that no
    longer exists is listed as already removed, never as an error."""
    found: List[Record] = []
    seen: Set[Tuple[str, str]] = set()

    def add(doctype: str, name: str, parent: Optional[str] = None) -> None:
        if doctype and name and (doctype, name) not in seen:
            seen.add((doctype, name))
            found.append(Record(doctype, name, parent or None))

    for row in session.created_records:
        add(row.record_doctype, row.record_name, row.parent_record)

    note = None
    if session.import_type == INSERT:
        done: Set[str] = set()
        for link in session.data_imports:
            if not link.data_import or link.data_import in done:
                continue
            done.add(link.data_import)
            doctype = frappe.db.get_value("Data Import", link.data_import, "reference_doctype")
            for docname in frappe.get_all(
                "Data Import Log",
                fields=["docname"],
                filters={"data_import": link.data_import, "success": 1},
                pluck="docname",
                order_by="creation asc",
                limit_page_length=0,
            ):
                add(doctype, docname)
    elif session.import_type == UPDATE and session.data_imports:
        note = _(
            "This session imported with Update Existing Records: those records existed before the "
            "import and are never deleted, so there is nothing to undo for them."
        )

    present: List[Record] = []
    removed: List[Record] = []
    for record in found:
        (present if _exists(record.doctype, record.name) else removed).append(record)
    return present, removed, note
