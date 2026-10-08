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
The session records the file's details and every step; the file itself stays
where the user attached it.
"""

from typing import Any, Dict, List, Optional

import frappe
from frappe import _

from frappe_assistant_core.core.base_tool import BaseTool
from frappe_assistant_core.plugins.core.import_session import (
    FileProblem,
    SheetSummary,
    get_readable_file,
    import_target_problem,
    read_spreadsheet,
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
            "(a FAC Chat attachment) pass file_url and the tool reads its sheets. For a file "
            "attached in this chat that is not on the site, read it yourself and pass file_name, "
            "sheet, columns and row_count; never pass its rows. The session stores no row of the "
            "file. Keep the returned session_id; read the session later with get_document (doctype "
            "'FAC Import Session'). Next: work out the target DocType from the columns and tell the "
            "user why, call get_import_schema for its fields, then save the mapping with "
            "set_column_mapping. Writes no business data."
        )
        self.requires_permission = None  # file access and target permission are checked per call

        self.inputSchema = {
            "type": "object",
            "properties": {
                "file_url": {
                    "type": "string",
                    "description": "File URL of a file on this site, e.g. '/private/files/customers.xlsx'.",
                },
                "file_name": {
                    "type": "string",
                    "description": "Name of a file attached in this chat that is not on the site.",
                },
                "sheet": {
                    "type": "string",
                    "description": "Sheet to use for an .xlsx or .xls file. Default for file_url: the first "
                    "sheet with rows.",
                },
                "columns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "With file_name: the column headers of the sheet, in order.",
                },
                "row_count": {
                    "type": "integer",
                    "description": "With file_name: the number of data rows, not counting the header.",
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
        file_url = (arguments.get("file_url") or "").strip()
        file_name = (arguments.get("file_name") or "").strip()
        target_doctype = arguments.get("target_doctype") or None
        import_type = arguments.get("import_type") or "Insert New Records"

        if not file_url and not file_name:
            return {
                "success": False,
                "error": _(
                    "Give file_url for a file on this site, or file_name with the columns and "
                    "row_count of a file attached in the chat."
                ),
            }

        # Refuse before any work when the user could never import the result.
        if target_doctype:
            problem = import_target_problem(target_doctype)
            if problem:
                return {"success": False, "error": problem, "target_doctype": target_doctype}

        try:
            if file_url:
                file_doc = get_readable_file(file_url)
                read = read_spreadsheet(file_doc, arguments.get("sheet") or None)
                sheets, chosen = read.sheets, read.chosen
                file_name = file_doc.file_name
            else:
                file_doc = None
                chosen = _summary_from_chat(arguments)
                sheets = [chosen]
        except FileProblem as e:
            return {"success": False, "error": str(e)}

        session = self._create_session(file_doc, file_name, sheets, chosen, target_doctype, import_type)

        result = {
            "success": True,
            "session_id": session.name,
            "file_name": file_name,
            "sheet": chosen.name,
            "columns": chosen.columns,
            "row_count": chosen.row_count,
            "sheets": [s.as_dict() for s in sheets],
            "target_doctype": target_doctype,
            "status": session.status,
        }
        if file_doc:
            # Read from the site file for the assistant to show; never stored.
            result["sample_rows"] = chosen.sample_rows
        ignored = [s.name for s in sheets if s is not chosen and s.row_count]
        if ignored:
            result["note"] = _(
                "Only sheet '{0}' is used. These sheets also have rows and were not used: {1}. "
                "Start another session with sheet set to use one of them."
            ).format(chosen.name, ", ".join(ignored))
        return result

    def _create_session(self, file_doc, file_name, sheets, chosen, target_doctype, import_type):
        session = frappe.get_doc(
            {
                "doctype": "FAC Import Session",
                "user": frappe.session.user,
                # Set by fac_endpoint from FAC Chat's X-AR-Session-Id header.
                "chat_session_id": getattr(frappe.local, "ar_session_id", None),
                "file_name": file_name,
                "source_file": file_doc.name if file_doc else None,
                "file_source": "Site File" if file_doc else "Chat Attachment",
                "sheet": chosen.name,
                "sheets": frappe.as_json([s.as_dict() for s in sheets]),
                "target_doctype": target_doctype,
                "import_type": import_type,
                "status": "File Read",
            }
        )
        where = _(" from sheet {0}").format(chosen.name) if chosen.name else ""
        session.log_step(
            "Read File",
            "Success",
            _("Read {0} rows and {1} columns{2}.").format(chosen.row_count, len(chosen.columns), where),
            rows_ok=chosen.row_count,
        )
        session.insert()
        return session


def _summary_from_chat(arguments: Dict[str, Any]) -> SheetSummary:
    """The sheet summary of a file that stays in the chat, as the assistant read it."""
    columns: Optional[List[Any]] = arguments.get("columns")
    row_count = arguments.get("row_count")
    if not columns:
        raise FileProblem(_("Give the columns of the file attached in the chat."))
    if row_count is None:
        raise FileProblem(_("Give the row_count of the file attached in the chat."))

    return SheetSummary(
        name=arguments.get("sheet") or None,
        columns=[str(c) for c in columns],
        row_count=int(row_count),
    )


# Make sure class name matches file name for discovery
start_import_session = StartImportSession
