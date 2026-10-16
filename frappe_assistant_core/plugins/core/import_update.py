# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Updating existing records from a file (#34005).

Data Import's Update Existing Records finds each record by its ID, so a file keyed on another
column (item code, customer name, tax ID, or several together) is matched here first: each row
gets its record's name in an added ID column, and the key columns themselves are not imported,
so a record's key is never rewritten with the file's spelling. The dry run reports rows with no
match or more than one, skips rows that change nothing, and shows old and new values.
"""

from collections import defaultdict
from typing import Dict, List, Tuple

import frappe
from frappe import _
from frappe.core.doctype.data_import.importer import get_id_field
from frappe.utils import cstr, flt, get_datetime, getdate

UPDATE = "Update Existing Records"
ID_HEADER = "ID"
DONT_IMPORT = "Don't Import"
MATCH_CHUNK = 500
NUMBER_TYPES = ("Currency", "Float", "Percent", "Int", "Check")
SKIPPED = ("name", "doctype")


def is_update(session) -> bool:
    return (session.import_type or "") == UPDATE


def id_fieldname(doctype: str) -> str:
    """The field Data Import finds a record by: its autoname field, else name."""
    return get_id_field(doctype).fieldname


def match_fields(session) -> List[str]:
    """The fields a row is matched on, in order: ``match_key`` holds them comma-separated."""
    return [f.strip() for f in (session.match_key or "").split(",") if f.strip()]


def mapping_problems(
    doctype: str, column_to_field_map: Dict[str, str], headers: List[str], match_on: List[str]
) -> Tuple[List[str], List[str]]:
    """The match fields for an update, and what's wrong with the mapping for one."""
    problems = []
    mapped = {field: int(pos) for pos, field in column_to_field_map.items() if field != DONT_IMPORT}
    for pos, field in column_to_field_map.items():
        if "." in field and field != DONT_IMPORT:
            problems.append(
                _(
                    "Column '{0}' goes to a child table. Updating child table rows from a file isn't "
                    "supported: map it to Don't Import."
                ).format(headers[int(pos)] if int(pos) < len(headers) else pos)
            )
    id_field = id_fieldname(doctype)
    id_columns = [f for f in dict.fromkeys(("name", id_field)) if f in mapped]
    match_on = [str(field).strip() for field in match_on if str(field or "").strip()]
    if id_columns and not match_on:
        match_on = [id_columns[0]]
    if not match_on:
        problems.append(
            _(
                "Say which column finds each row's record: pass match_on, e.g. ['tax_id'], "
                "or map a column to the ID ('name')."
            )
        )
    for field in match_on:
        if field not in mapped:
            problems.append(
                _("match_on '{0}' isn't mapped from any column. Map the key column to it.").format(field)
            )
    if id_columns and match_on and match_on != [id_columns[0]]:
        problems.append(
            _(
                "A column is mapped to the ID, so the records are found by it: match_on must be ['{0}']."
            ).format(id_columns[0])
        )
    return match_on, problems


# -- matching rows to records ---------------------------------------------------------------


def _norm(value) -> str:
    # MariaDB compares text case- and trailing-space-insensitively: match the way it does.
    return cstr(value).strip().casefold()


def _positions(session) -> Dict[str, int]:
    """Field → the column it's mapped from."""
    options = frappe.parse_json(session.template_options or "{}") or {}
    mapped = options.get("column_to_field_map") or {}
    return {field: int(pos) for pos, field in mapped.items() if field != DONT_IMPORT}


def _id_match(session) -> bool:
    return match_fields(session) in (["name"], [id_fieldname(session.target_doctype)])


def match_records(session, rows: List[list]) -> Dict[int, List[str]]:
    """Each row's matching records, by row number (the header is row 1): one name when the
    match is clean; none, or several, when it isn't. Only records the user may read match."""
    fields = match_fields(session)
    positions = _positions(session)
    keys: Dict[int, tuple] = {}
    for number, row in enumerate(rows[1:], start=2):
        keys[number] = tuple(_norm(row[positions[f]] if positions[f] < len(row) else "") for f in fields)
    found = defaultdict(list)
    firsts = sorted({key[0] for key in keys.values() if all(key)})
    for start in range(0, len(firsts), MATCH_CHUNK):
        chunk = firsts[start : start + MATCH_CHUNK]
        for record in frappe.get_list(
            session.target_doctype,
            filters=[[fields[0], "in", chunk]],
            fields=list(dict.fromkeys(["name", *fields])),
            limit_page_length=0,
        ):
            found[tuple(_norm(record.get(f)) for f in fields)].append(record.name)
    return {number: (list(found.get(key, [])) if all(key) else []) for number, key in keys.items()}


def with_ids(session, rows: List[list], records: Dict[int, str]) -> Tuple[List[list], Dict[str, str]]:
    """The rows with an ID column holding each row's record, and the column map that imports
    them: the ID column to the record's ID, the key columns not imported (they only find the
    record). Matching on the ID itself needs no column: the rows come back as they are."""
    options = frappe.parse_json(session.template_options or "{}") or {}
    column_map = dict(options.get("column_to_field_map") or {})
    if _id_match(session) or not rows:
        return rows, column_map
    width = len(rows[0])
    keys = match_fields(session)
    for field, position in _positions(session).items():
        if field in keys:
            column_map[str(position)] = DONT_IMPORT
    column_map[str(width)] = "name"
    out = [list(rows[0]) + [ID_HEADER]]
    for number, row in enumerate(rows[1:], start=2):
        out.append((list(row) + [""] * width)[:width] + [records.get(number, "")])
    return out, column_map


def key_label(session) -> str:
    """The match fields' labels, for messages: 'Tax ID', or 'Item Code + Price List'."""
    meta = frappe.get_meta(session.target_doctype)
    labels = []
    for field in match_fields(session):
        df = meta.get_field(field)
        labels.append(_("ID") if field == "name" else _(df.label if df else field))
    return " + ".join(labels)


# -- what an update changes -----------------------------------------------------------------


def _same(df, old, new) -> bool:
    if df.fieldtype in NUMBER_TYPES:
        return flt(old, 9) == flt(new, 9)
    if df.fieldtype == "Date" and old and new:
        return getdate(old) == getdate(new)
    if df.fieldtype == "Datetime" and old and new:
        return get_datetime(old) == get_datetime(new)
    return cstr(old).strip() == cstr(new).strip()


def changes(doctype: str, record: str, doc: Dict) -> List[Dict]:
    """What updating ``record`` with the parsed row ``doc`` changes: one {field, label, old, new}
    per field whose value differs. A blank cell is no change: Data Import leaves it as it is."""
    meta = frappe.get_meta(doctype)
    skip = {*SKIPPED, id_fieldname(doctype)}
    fields = []
    for field in doc:
        df = meta.get_field(field)
        if field not in skip and df and not df.is_virtual and df.fieldtype not in frappe.model.table_fields:
            fields.append(field)
    current = frappe.db.get_value(doctype, record, fields, as_dict=True) if fields else {}
    out = []
    for field in fields:
        df = meta.get_field(field)
        if not _same(df, current.get(field), doc[field]):
            out.append({"field": field, "label": _(df.label), "old": current.get(field), "new": doc[field]})
    return out


def checked_records(session) -> Dict[int, str]:
    """The record of each row the last check found ready, from its results file."""
    from frappe_assistant_core.plugins.core import import_dry_run as dry_run

    file_doc = dry_run._results_file(session)
    if not file_doc:
        return {}
    content = frappe.parse_json(file_doc.get_content()) or {}
    return {
        int(row["row"]): row["record"]
        for row in content.get("rows") or []
        if row.get("status") == "ready" and row.get("record")
    }
