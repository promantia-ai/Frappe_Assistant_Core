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

from typing import Dict, List, Tuple

from frappe import _
from frappe.core.doctype.data_import.importer import get_id_field

UPDATE = "Update Existing Records"
ID_HEADER = "ID"
DONT_IMPORT = "Don't Import"


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
