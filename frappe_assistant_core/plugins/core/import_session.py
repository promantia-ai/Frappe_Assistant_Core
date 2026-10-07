# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Helpers for start_import_session: checking file access, checking that the user
may import into a target DocType, and reading a spreadsheet.

Cells are read as text, the way Data Import will see them. Only the headers,
the row counts and a few sample rows are kept and returned to the assistant; no
copy of the file is saved.
"""

import datetime
import io
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

import frappe
from frappe import _

SUPPORTED_EXTENSIONS = ("csv", "xlsx", "xls")
SAMPLE_SIZE = 10
MAX_FILE_SIZE = 50 * 1024 * 1024  # same ceiling as chat uploads


@dataclass
class SheetSummary:
    """What the assistant gets to see of one sheet."""

    name: Optional[str]  # None for a .csv file
    columns: List[str] = field(default_factory=list)
    row_count: int = 0
    sample_rows: List[List[str]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "columns": self.columns,
            "row_count": self.row_count,
            "sample_rows": self.sample_rows,
        }


@dataclass
class SpreadsheetRead:
    sheets: List[SheetSummary]  # every sheet, in workbook order
    chosen: SheetSummary  # the sheet the session works on


class FileProblem(Exception):
    """A file that cannot be opened as a session. The message is shown to the user."""


# -- files -----------------------------------------------------------------------


def get_readable_file(file_url: str):
    """Return the File the current user may read at ``file_url``.

    Identical uploads share one ``file_url`` across several File records (Frappe
    stores the content once), so every record behind the URL is tried, and the
    user gets the first one they may read.
    """
    for name in frappe.get_all("File", filters={"file_url": file_url}, pluck="name"):
        file_doc = frappe.get_doc("File", name)
        if frappe.has_permission("File", "read", doc=file_doc):
            return file_doc
    # One message for "missing" and "not yours", so file names cannot be probed.
    raise FileProblem(_("File not found, or you don't have access to it."))


def read_spreadsheet(file_doc, sheet: Optional[str] = None) -> SpreadsheetRead:
    """Summarise every sheet of ``file_doc`` and choose the one to work on.

    The chosen sheet is ``sheet`` when given, else the first sheet with rows.
    """
    extension = (file_doc.get_extension()[1] or "").lstrip(".").lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise FileProblem(_("Only .csv, .xlsx or .xls files can be imported."))
    if (file_doc.file_size or 0) > MAX_FILE_SIZE:
        raise FileProblem(_("The file is larger than 50 MB."))

    try:
        content = file_doc.get_content()
        if isinstance(content, str):
            content = content.encode("utf-8")
        # Sheets stream past, so a large workbook is never held in memory as rows.
        sheets = [_summarise(name, rows) for name, rows in _iter_sheets(content, extension)]
        chosen = _choose(sheets, sheet if extension != "csv" else None)
    except FileProblem:
        raise
    except Exception as e:
        raise FileProblem(_("Could not read the file: {0}").format(_describe_read_error(e)))

    return SpreadsheetRead(sheets=sheets, chosen=chosen)


def _choose(sheets: List[SheetSummary], sheet: Optional[str]) -> SheetSummary:
    if sheet:
        for summary in sheets:
            if summary.name == sheet:
                if not summary.row_count:
                    raise FileProblem(_("Sheet '{0}' has no rows to import.").format(sheet))
                return summary
        names = ", ".join(s.name for s in sheets)
        raise FileProblem(_("Sheet '{0}' not found. Sheets in this file: {1}.").format(sheet, names))

    for summary in sheets:
        if summary.columns and summary.row_count:
            return summary
    raise FileProblem(_("The file has no rows to import. Row 1 must hold the column headers."))


def _describe_read_error(e: Exception) -> str:
    import zipfile

    if isinstance(e, zipfile.BadZipFile):
        return _("it is not a valid Excel workbook")
    return str(e) or type(e).__name__


def _iter_sheets(content: bytes, extension: str) -> Iterator[Tuple[Optional[str], Iterable]]:
    """Yield (sheet name, rows) pairs. Rows hold raw cell values."""
    if extension == "csv":
        from frappe.utils.csvutils import read_csv_content

        yield None, read_csv_content(content)
    elif extension == "xlsx":
        from openpyxl import load_workbook

        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        try:
            for worksheet in workbook.worksheets:
                yield worksheet.title, worksheet.iter_rows(values_only=True)
        finally:
            workbook.close()
    else:
        import xlrd

        book = xlrd.open_workbook(file_contents=content)
        for xls_sheet in book.sheets():
            yield xls_sheet.name, (_xls_row(book, xls_sheet, i) for i in range(xls_sheet.nrows))


def _xls_row(book, xls_sheet, index: int) -> list:
    import xlrd

    values = []
    for cell in xls_sheet.row(index):
        if cell.ctype == xlrd.XL_CELL_DATE:
            values.append(xlrd.xldate_as_datetime(cell.value, book.datemode))
        elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
            values.append(bool(cell.value))
        elif cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
            values.append(None)
        else:
            values.append(cell.value)
    return values


def _text_rows(rows: Iterable) -> Iterator[List[str]]:
    """The header, then every non-blank data row, as text cut to the header's width.

    Trailing empty header cells are formatting, not columns.
    """
    width = None
    for raw in rows:
        values = [_as_text(v) for v in raw]
        if width is None:
            width = max((i + 1 for i, v in enumerate(values) if v), default=0)
            yield values[:width]
            continue
        if not any(values):
            continue
        row = values[:width]
        yield row + [""] * (width - len(row))


def _summarise(name: Optional[str], rows: Iterable) -> SheetSummary:
    summary = SheetSummary(name=name)
    for index, row in enumerate(_text_rows(rows)):
        if index == 0:
            summary.columns = row
            continue
        summary.row_count += 1
        if len(summary.sample_rows) < SAMPLE_SIZE:
            summary.sample_rows.append(row)
    if not summary.columns:
        summary.row_count = 0
        summary.sample_rows = []
    return summary


def _as_text(value: Any) -> str:
    """A cell as Data Import would read it, written as plain text."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, datetime.datetime):
        if value.time() == datetime.time(0, 0):
            return value.date().isoformat()
        return value.isoformat(sep=" ")
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


# -- target DocType ----------------------------------------------------------------


def import_target_problem(doctype: str) -> Optional[str]:
    """Why the current user cannot import into ``doctype``, or None when they can.

    Data Import itself requires the DocType to allow import and the user to hold
    Import permission on it; inserting each row also needs Create.
    """
    if not frappe.db.exists("DocType", doctype):
        return _("DocType {0} does not exist.").format(doctype)

    meta = frappe.get_meta(doctype)
    if meta.istable:
        return _("{0} is a child table. Import it through its parent DocType.").format(doctype)
    if meta.issingle:
        return _("{0} is a single settings record, not a list, so it can't be imported.").format(doctype)
    if not meta.allow_import:
        return _("{0} can't be imported: Allow Import is turned off for it.").format(doctype)
    if not frappe.has_permission(doctype, "create"):
        return _("You don't have permission to create {0} records, so you can't import them.").format(doctype)
    if not frappe.has_permission(doctype, "import"):
        return _(
            "You can create {0} records but don't have Import permission on {0}. "
            "A System Manager can grant it in Role Permissions Manager."
        ).format(doctype)
    return None
