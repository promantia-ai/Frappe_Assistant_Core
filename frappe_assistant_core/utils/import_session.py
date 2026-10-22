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

Cells are read as text, the way Data Import will see them. The assistant gets the
headers, row counts and a few sample rows; the session stores only the headers and
row counts. No row of the file is saved.
"""

import datetime
import io
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple
from urllib.parse import urlparse

import frappe
import requests
from frappe import _
from frappe.core.api.file import get_max_file_size
from frappe.utils import cint

SUPPORTED_EXTENSIONS = ("csv", "xlsx", "xls")
SAMPLE_SIZE = 10
MAX_FILE_SIZE = 50 * 1024 * 1024  # same ceiling as chat uploads
# A Google Sheet's download: seconds to connect and between two pieces, and in all.
DOWNLOAD_TIMEOUT = (10, 30)
DOWNLOAD_SECONDS = 120


@dataclass
class SheetSummary:
    """What the assistant gets to see of one sheet."""

    name: Optional[str]  # None for a .csv file
    columns: List[str] = field(default_factory=list)
    row_count: int = 0
    sample_rows: List[List[str]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        """What the session stores: no sample rows, so no data from the file."""
        return {"name": self.name, "columns": self.columns, "row_count": self.row_count}


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


def get_readable_file_by_id(file_id: str):
    """Return the File named ``file_id`` (its ID in File Manager), if the current user may read it."""
    if frappe.db.exists("File", {"name": file_id, "is_folder": 0}):
        file_doc = frappe.get_doc("File", file_id)
        if frappe.has_permission("File", "read", doc=file_doc):
            return file_doc
    raise FileProblem(_("File not found, or you don't have access to it."))


def find_site_file(file_id: str = "", file_url: str = ""):
    """The File the current user may read, by its ID or its URL. Both may be given when they name
    the same file, as a chat attachment gives them."""
    if file_id:
        file_doc = get_readable_file_by_id(file_id)
        if file_url and file_doc.file_url != file_url:
            raise FileProblem(_("file_id and file_url name different files: give one of them."))
        return file_doc
    return get_readable_file(file_url)


def file_from_google_sheet(url: str):
    """A private .csv copy, owned by the user, of the Google Sheets tab ``url`` links to (#34006).

    The tab in the link is downloaded, else the first. The session works on the copy like on any
    file of this site, so what the dry run checks is what gets imported, whatever is changed in
    the sheet meanwhile.
    """
    from frappe.utils.csvutils import validate_google_sheets_url

    url = (url or "").strip()
    try:
        validate_google_sheets_url(url)
    except frappe.ValidationError:
        frappe.clear_messages()  # Frappe's pop-up says the same, less helpfully
        raise FileProblem(
            _(
                "This isn't a Google Sheets link. Open the sheet in Google Sheets and copy the link "
                "from the browser's address bar."
            )
        )
    try:
        content = _download_sheet(url)
    except (requests.ConnectionError, requests.Timeout):
        raise FileProblem(_cant_reach_google())
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code >= 500:
            raise FileProblem(_cant_reach_google())
        raise FileProblem(_cant_open_sheet())  # a deleted or mistyped sheet (404), a wrong tab (400)
    except requests.RequestException:
        raise FileProblem(_cant_open_sheet())

    sheet_id = re.sub(r"[^A-Za-z0-9_-]", "", urlparse(url).path.split("/d/", 1)[-1].split("/", 1)[0])
    return frappe.get_doc(
        {
            "doctype": "File",
            "file_name": f"google-sheet-{sheet_id[:8] or 'copy'}.csv",
            "content": content,
            "is_private": 1,
        }
    ).insert()


def _download_sheet(url: str) -> bytes:
    """The CSV export of the tab ``url`` links to, the way Frappe's
    get_csv_content_from_google_sheets builds its link, but streamed: refused once it passes
    MAX_FILE_SIZE or DOWNLOAD_SECONDS, so a huge or trickling sheet can't hold a worker."""
    # The tab's number only: a "link to this range" adds "&range=…" after it
    gid = re.search(r"gid=(\d+)", url)
    export = url.split("/edit", 1)[0] + f"/export?format=csv&gid={gid.group(1) if gid else 0}"
    deadline = time.monotonic() + DOWNLOAD_SECONDS
    # Frappe's File refuses what passes System Settings' Max File Size (25 MB by default)
    limit = min(MAX_FILE_SIZE, get_max_file_size())
    too_large = _("The sheet is larger than {0} MB, the largest file this site takes.").format(
        round(limit / (1024 * 1024), 1)
    )

    with requests.get(
        export, headers={"Accept": "text/csv"}, stream=True, timeout=DOWNLOAD_TIMEOUT
    ) as response:
        response.raise_for_status()
        if cint(response.headers.get("Content-Length")) > limit:
            raise FileProblem(too_large)
        content = bytearray()
        # As the pieces arrive: a large piece trickling in would put off the time limit
        for chunk in response.iter_content(chunk_size=None):
            content += chunk
            if len(content) > limit:
                raise FileProblem(too_large)
            if time.monotonic() > deadline:
                raise FileProblem(_cant_reach_google())

    # A sheet that isn't shared answers 200 with Google's sign-in page.
    if bytes(content).strip().endswith(b"</html>"):
        raise FileProblem(_cant_open_sheet())
    return bytes(content)


def discard_sheet_copy(file_doc) -> None:
    """Delete a Google Sheet's copy no session uses. The disk file stays while another File
    shares its content."""
    if file_doc:
        # made by this same call, for this user
        frappe.delete_doc("File", file_doc.name, ignore_permissions=True)


def _cant_open_sheet() -> str:
    return _(
        "I can't open this Google Sheet. Share it so anyone with the link can view it: in Google "
        "Sheets click Share, under General access choose Anyone with the link (Viewer), then paste "
        "the link again. If it is shared, open the tab to import and copy the link from the "
        "browser's address bar."
    )


def _cant_reach_google() -> str:
    return _("Google Sheets didn't answer. Please try again in a moment.")


def readable_session_file(session):
    """The session's file, when the current user may read it. A session's fields can be written
    over the REST API, read-only or not, so its source_file is checked each time it is read."""
    if session.source_file and frappe.db.exists("File", session.source_file):
        file_doc = frappe.get_doc("File", session.source_file)
        if frappe.has_permission("File", "read", doc=file_doc):
            return file_doc
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


def read_sheet_rows(file_doc, sheet: Optional[str] = None) -> List[List[str]]:
    """Every row of one sheet as text, header first, for the engine (never the model).

    Blank rows stay in place as empty rows, so a row's position is its line in the
    spreadsheet, the number Data Import reports and the user sees.
    """
    extension = (file_doc.get_extension()[1] or "").lstrip(".").lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise FileProblem(_("Only .csv, .xlsx or .xls files can be imported."))
    try:
        content = file_doc.get_content()
        if isinstance(content, str):
            content = content.encode("utf-8")
        names = []
        for name, rows in _iter_sheets(content, extension):
            names.append(name)
            if extension == "csv" or not sheet or name == sheet:
                return _all_text_rows(rows)
    except FileProblem:
        raise
    except Exception as e:
        raise FileProblem(_("Could not read the file: {0}").format(_describe_read_error(e)))
    raise FileProblem(
        _("Sheet '{0}' not found. Sheets in this file: {1}.").format(
            sheet, ", ".join(str(name) for name in names)
        )
    )


def _all_text_rows(rows: Iterable) -> List[List[str]]:
    width = None
    out = []
    for raw in rows:
        values = [_as_text(v) for v in raw]
        if width is None:
            width = max((i + 1 for i, v in enumerate(values) if v), default=0)
        row = values[:width]
        out.append(row + [""] * (width - len(row)))
    return out


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


# -- how many rows one import may have ----------------------------------------------

DEFAULT_MAX_IMPORT_ROWS = 50000


def holding_session(file_name: str) -> Optional[str]:
    """The import session that holds File ``file_name``, or None (#34003). The import tools read
    that file, showing the model its headers, samples and summaries only, never all its rows.
    Frappe keeps identical bytes once, so a copy uploaded again (a new File with the same
    file_url) is held too."""
    file_url = frappe.db.get_value("File", file_name, "file_url")
    copies = frappe.get_all("File", filters={"file_url": file_url}, pluck="name") if file_url else []
    return frappe.db.get_value(
        "FAC Import Session",
        {"source_file": ["in", list({file_name, *copies})], "status": ["!=", "Cancelled"]},
        "name",
    )


def max_import_rows() -> int:
    """Assistant Core Settings' Max Rows per Import; 0 or blank is the default, never unlimited."""
    try:
        value = frappe.db.get_single_value("Assistant Core Settings", "max_import_rows")
    except frappe.ValidationError:
        # the code is newer than the site: `bench migrate` hasn't added the field yet
        frappe.clear_messages()
        value = None
    return cint(value) or DEFAULT_MAX_IMPORT_ROWS


def row_limit_problem(row_count: int) -> Optional[str]:
    """Why a file of ``row_count`` rows can't be imported in one go, or None (#34003)."""
    limit = max_import_rows()
    if cint(row_count) <= limit:
        return None
    return _(
        "The file has {0} rows, more than the {1} one import may have. Split it into files of at "
        "most {1} rows and import them one after another. (The limit is Max Rows per Import in "
        "Assistant Core Settings.)"
    ).format(row_count, limit)


# -- target DocType ----------------------------------------------------------------


def import_target_problem(doctype: str, import_type: str = "Insert New Records") -> Optional[str]:
    """Why the current user cannot import into ``doctype``, or None when they can.

    Data Import itself requires the DocType to allow import and the user to hold
    Import permission on it; inserting each row also needs Create, updating needs Write.
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
    if import_type == "Update Existing Records":
        if not frappe.has_permission(doctype, "write"):
            return _("You don't have permission to change {0} records, so you can't update them.").format(
                doctype
            )
    elif not frappe.has_permission(doctype, "create"):
        return _("You don't have permission to create {0} records, so you can't import them.").format(doctype)
    if not frappe.has_permission(doctype, "import"):
        return _(
            "You can create {0} records but don't have Import permission on {0}. "
            "A System Manager can grant it in Role Permissions Manager."
        ).format(doctype)
    return None
