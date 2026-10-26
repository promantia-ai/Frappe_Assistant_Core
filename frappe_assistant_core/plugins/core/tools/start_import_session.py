# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Start Import Session Tool for Core Plugin.

Opens a FAC Import Session for a spreadsheet the user wants to import, so the
migration can be worked through over several messages without starting again.
The session records the file's details and every step. The sheet is read once into a
private CSV the session owns (#34405), with its headings found below any title row
(#34403) and its text decoded (#34404); every later step reads that copy. A Google
Sheets link is downloaded the same way (#34006).
"""

from typing import Any, Dict

import frappe
from frappe import _
from frappe.utils import cint

from frappe_assistant_core.assistant_core.doctype.fac_import_session.fac_import_session import engine_write
from frappe_assistant_core.core.base_tool import BaseTool
from frappe_assistant_core.utils.import_session import (
    HOLDING_STATUSES,
    FileProblem,
    attach_session_copy,
    discard_session_copy,
    discard_sheet_copy,
    file_from_google_sheet,
    find_site_file,
    import_target_problem,
    read_spreadsheet,
    row_limit_problem,
    save_session_copy,
)


class StartImportSession(BaseTool):
    """Open an import session for a spreadsheet the user wants to import."""

    def __init__(self):
        super().__init__()
        self.name = "start_import_session"
        self.description = (
            "Start a data import for a spreadsheet (.csv, .xlsx, .xls) the user wants to import. "
            "Opens an import session that records the file's details and every step, and keeps "
            "the mapping, fixes and validation results between messages. For a file on this site "
            "(uploaded in File Manager, or a FAC Chat attachment) pass its file_id, or its file_url, "
            "and the tool reads its sheets; the user must be allowed to read the file. When the "
            "user names a file instead, find its ID with list_documents on File (filter by "
            "file_name); if several match, show them and ask which one, never guess. A file only "
            "attached in this chat (Claude, ChatGPT) must be uploaded to the site first: ask the "
            "user to upload it in File Manager, then pass its file_id; never read its rows "
            "yourself. The sheet is read once into a private copy the session owns, so the file "
            "the user gave may be deleted later. The column headings may be below a title row "
            "(Tally, Busy, Zoho exports): they are found, and header_row says where. When the tool "
            "can't tell which row holds them it returns first_rows: show them, ask the user which "
            "row has the headings, and call again with header_row. For a Google Sheets link the "
            "user pastes, pass google_sheets_url: the sheet must be shared so anyone with the link "
            "can view it. Keep the returned session_id; read the "
            "session later with get_document (doctype 'FAC Import Session'). Next: work out the target DocType from the columns and tell the "
            "user why, call get_import_schema for its fields, then save the mapping with "
            "set_column_mapping. Writes no business data."
        )
        self.requires_permission = None  # file access and target permission are checked per call
        # what the user's file says reaches the model fenced (#34003)
        self.untrusted_fields = (
            "file_name",
            "sheet",
            "columns",
            "sheets",
            "sample_rows",
            "first_rows",
            "note",
            "error",
        )

        self.inputSchema = {
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "description": "ID of a file on this site: its name in File Manager, e.g. '8f3a2b1c9d'.",
                },
                "file_url": {
                    "type": "string",
                    "description": "File URL of a file on this site, e.g. '/private/files/customers.xlsx'.",
                },
                "google_sheets_url": {
                    "type": "string",
                    "description": "A Google Sheets link the user pasted, e.g. "
                    "'https://docs.google.com/spreadsheets/d/1AbC.../edit#gid=0'. The tab in the "
                    "link is read (else the first tab).",
                },
                "sheet": {
                    "type": "string",
                    "description": "Sheet to use for an .xlsx or .xls file. Default: the first sheet "
                    "with rows.",
                },
                "header_row": {
                    "type": "integer",
                    "description": "The row (counted from 1) that holds the column headings, when the "
                    "user said which: after the tool returned first_rows, or to correct the row it "
                    "found. Default: found by the tool.",
                },
                "target_doctype": {
                    "type": "string",
                    "description": "DocType the rows become, when known. The user's permission to import "
                    "into it is checked before anything else.",
                },
                "import_type": {
                    "type": "string",
                    "enum": ["Insert New Records", "Update Existing Records"],
                    "description": "Default: Insert New Records.",
                },
            },
        }

    def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        file_id = (arguments.get("file_id") or "").strip()
        file_url = (arguments.get("file_url") or "").strip()
        sheet_url = (arguments.get("google_sheets_url") or "").strip()
        target_doctype = arguments.get("target_doctype") or None
        import_type = arguments.get("import_type") or "Insert New Records"
        header_row = cint(arguments.get("header_row")) or None

        if not file_id and not file_url and not sheet_url:
            return {
                "success": False,
                "error": _(
                    "Give file_id or file_url for a file on this site, or google_sheets_url for a "
                    "Google Sheets link. A file only attached in this chat has to be on the site "
                    "first: ask the user to upload it in File Manager, then pass its file_id."
                ),
            }
        if sheet_url and (file_id or file_url):
            return {
                "success": False,
                "error": _("Give only the Google Sheets link, or only a file: not both."),
            }
        if header_row is not None and header_row < 1:
            return {"success": False, "error": _("header_row counts from 1, the sheet's first row.")}

        # Refuse before any work when the user could never import the result.
        if target_doctype:
            problem = import_target_problem(target_doctype, import_type)
            if problem:
                return {"success": False, "error": problem, "target_doctype": target_doctype}

        download = None  # a Google Sheet's download: the session keeps its own copy instead
        try:
            if sheet_url:
                file_doc = download = file_from_google_sheet(sheet_url)
            else:
                file_doc = find_site_file(file_id=file_id, file_url=file_url)
            read = read_spreadsheet(file_doc, arguments.get("sheet") or None, header_row, keep_rows=True)
            sheets, chosen = read.sheets, read.chosen
        except FileProblem as e:
            discard_sheet_copy(download)
            return {"success": False, "error": str(e)}

        if not chosen.header_sure:
            discard_sheet_copy(download)
            return {
                "success": False,
                "error": _(
                    "I can't tell which row holds the column headings: the rows above row {0} hold "
                    "several values too. Show the user first_rows, ask which row has the headings, "
                    "then call start_import_session again with header_row."
                ).format(chosen.header_row),
                "first_rows": chosen.first_rows,
                "header_row_guess": chosen.header_row,
            }

        problem = row_limit_problem(chosen.row_count)
        if problem:
            discard_sheet_copy(download)
            return {"success": False, "error": problem, "row_count": chosen.row_count}

        reused = None if sheet_url else self._open_session(file_doc, chosen, target_doctype, import_type)
        if reused:
            # the same file again in the same chat: carry on with its session, not a second one
            return {
                "success": True,
                "session_id": reused.name,
                "reused": True,
                "file_name": file_doc.file_name,
                "sheet": chosen.name,
                "columns": chosen.columns,
                "row_count": chosen.row_count,
                "sample_rows": chosen.sample_rows,
                "sheets": [s.as_dict() for s in sheets],
                "target_doctype": reused.target_doctype,
                "status": reused.status,
                "note": _(
                    "This file already has an open import session in this chat, {0} ({1}): carry on "
                    "with it. To start over, give another sheet or header_row."
                ).format(reused.name, _(reused.status)),
            }

        copy = None
        try:
            copy = save_session_copy(file_doc.file_name, chosen)
            session = self._create_session(
                file_doc, copy, sheets, chosen, target_doctype, import_type, sheet_url or None
            )
        except Exception:
            discard_session_copy(copy)  # no session holds it
            raise
        finally:
            discard_sheet_copy(download)

        result = {
            "success": True,
            "session_id": session.name,
            "file_name": file_doc.file_name,
            "sheet": chosen.name,
            "columns": chosen.columns,
            "row_count": chosen.row_count,
            "sample_rows": chosen.sample_rows,  # for the assistant to show; never stored
            "sheets": [s.as_dict() for s in sheets],
            "target_doctype": target_doctype,
            "status": session.status,
        }
        notes = []
        if chosen.header_row > 1:
            result["header_row"] = chosen.header_row
            notes.append(
                _(
                    "The column headings are on row {0}: the rows above them were left out. Later "
                    "replies count rows from the headings, so row 2 is the first row under them."
                ).format(chosen.header_row)
            )
        ignored = [s.name for s in sheets if s is not chosen and s.row_count]
        if ignored:
            notes.append(
                _(
                    "Only sheet '{0}' is used. These sheets also have rows and were not used: {1}. "
                    "Start another session with sheet set to use one of them."
                ).format(chosen.name, ", ".join(ignored))
            )
        if sheet_url:
            notes.append(
                _(
                    "Only one tab of the Google Sheet was read: the one open when the link was "
                    "copied, else the first. To import another tab, open it and copy the link again."
                )
            )
        if notes:
            result["note"] = " ".join(notes)
        return result

    def _open_session(self, file_doc, chosen, target_doctype, import_type):
        """The user's open session in this FAC Chat conversation for the same file, sheet,
        headings and import, which a second call carries on with rather than open another
        (#34413). Outside FAC Chat (no conversation to tell them apart), none."""
        chat = getattr(frappe.local, "ar_session_id", None)
        if not chat:
            return None
        for name in frappe.get_all(
            "FAC Import Session",
            filters={
                "user": frappe.session.user,
                "chat_session_id": chat,
                "original_file": file_doc.name,
                "status": ["in", HOLDING_STATUSES],
            },
            pluck="name",
            order_by="creation desc",
        ):
            doc = frappe.get_doc("FAC Import Session", name)
            stored = next(
                (s for s in frappe.parse_json(doc.sheets or "[]") or [] if s.get("name") == doc.sheet), {}
            )
            if (
                (doc.sheet or None) == (chosen.name or None)
                and stored.get("columns") == chosen.columns
                and (doc.import_type or "Insert New Records") == import_type
                and (not target_doctype or not doc.target_doctype or doc.target_doctype == target_doctype)
            ):
                return doc
        return None

    def _create_session(self, file_doc, copy, sheets, chosen, target_doctype, import_type, sheet_url):
        session = frappe.get_doc(
            {
                "doctype": "FAC Import Session",
                "user": frappe.session.user,
                # Set by fac_endpoint from FAC Chat's X-AR-Session-Id header.
                "chat_session_id": getattr(frappe.local, "ar_session_id", None),
                "file_name": file_doc.file_name,
                "source_file": copy.name,
                # A Google Sheet's download is gone once the session has its copy: the link stays.
                "original_file": None if sheet_url else file_doc.name,
                "file_source": "Google Sheet" if sheet_url else "Site File",
                "source_url": sheet_url,
                "sheet": chosen.name,
                "sheets": frappe.as_json([s.as_dict() for s in sheets]),
                "target_doctype": target_doctype,
                "import_type": import_type,
                "status": "File Read",
            }
        )
        where = " " + _("from sheet {0}").format(chosen.name) if chosen.name else ""
        read = _("Read {0} rows and {1} columns{2}.").format(chosen.row_count, len(chosen.columns), where)
        if chosen.header_row > 1:
            read += " " + _("The headings are on row {0}.").format(chosen.header_row)
        session.log_step("Read File", "Success", read, rows_ok=chosen.row_count)
        engine_write(session).insert()  # the engine's own session (#34400)
        attach_session_copy(copy, session.name)
        return session


# Make sure class name matches file name for discovery
start_import_session = StartImportSession
