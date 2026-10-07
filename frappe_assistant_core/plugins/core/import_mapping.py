# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""
Helpers for get_import_schema and set_column_mapping (#33989): which fields of a DocType
can be imported, and the column mapping kept on a FAC Import Session.

The mapping uses Data Import's own shape, ``template_options.column_to_field_map``:
``{"<0-based column position>": "<fieldname>" | "<table fieldname>.<fieldname>" | "Don't Import"}``,
so the import itself (#33993) can copy it straight onto a Data Import.

Whether the user may import into a DocType is import_session.import_target_problem, the
check start_import_session uses.
"""

import difflib
from typing import Any, Dict, List, Optional, Tuple

import frappe
from frappe.model import display_fieldtypes, no_value_fields

DONT_IMPORT = "Don't Import"
MAX_SELECT_OPTIONS = 25
SESSION_DOCTYPE = "FAC Import Session"


# --- Schema --------------------------------------------------------------------------


def build_import_schema(doctype: str) -> Dict[str, Any]:
    """The fields Data Import can fill on `doctype`, kept short enough for the model to read.

    Uses the rule of Frappe's own import template (no layout or display fields) and also
    leaves out hidden and read-only fields, which an import does not set.
    """
    meta = frappe.get_meta(doctype)
    schema: Dict[str, Any] = {
        "doctype": doctype,
        "naming": _naming(meta),
        "fields": _importable_fields(meta),
    }

    child_tables = []
    for table_df in meta.get_table_fields():
        if table_df.hidden or table_df.read_only:
            continue
        table = {
            "fieldname": table_df.fieldname,
            "label": table_df.label or table_df.fieldname,
            "doctype": table_df.options,
        }
        if table_df.reqd:
            table["required"] = True
        table["fields"] = _importable_fields(frappe.get_meta(table_df.options))
        child_tables.append(table)
    if child_tables:
        schema["child_tables"] = child_tables

    schema["map_as"] = (
        "Map a column to a field by its fieldname, to a child table field as "
        f"'<table fieldname>.<fieldname>', or to \"{DONT_IMPORT}\"."
    )
    return schema


def _importable_fields(meta) -> List[Dict[str, Any]]:
    fields = []
    for df in meta.fields:
        if df.fieldtype in display_fieldtypes + no_value_fields or df.hidden or df.read_only:
            continue
        field: Dict[str, Any] = {
            "fieldname": df.fieldname,
            "label": df.label or df.fieldname,
            "type": df.fieldtype,
        }
        if df.reqd:
            field["required"] = True
            if df.default:
                field["default"] = df.default
        if df.fieldtype == "Link":
            field["link"] = df.options
        elif df.fieldtype == "Dynamic Link":
            field["link_type_from"] = df.options
        elif df.fieldtype == "Select" and df.options:
            options = [option for option in df.options.split("\n") if option]
            field["options"] = options[:MAX_SELECT_OPTIONS]
            if len(options) > MAX_SELECT_OPTIONS:
                field["options_total"] = len(options)
        fields.append(field)
    return fields


def _naming(meta) -> Dict[str, Any]:
    autoname = (meta.autoname or "").strip()
    if autoname.startswith("field:"):
        rule = f"from the {autoname[len('field:') :]} field"
    elif autoname.lower() == "prompt":
        rule = "typed by the user, so map a column to 'name' (ID) when inserting"
    elif autoname.startswith("naming_series:") or autoname == "naming_series":
        rule = "naming series"
    elif autoname in ("", "hash"):
        rule = "automatic"
    else:
        rule = autoname
    return {"rule": rule, "id_field": "name", "id_note": "Map a column to 'name' to update existing records."}


def _mapping_targets(schema: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Every value a column may map to, keyed the way column_to_field_map stores it."""
    targets: Dict[str, Dict[str, Any]] = {"name": {"label": "ID", "table": None}}
    for field in schema["fields"]:
        targets[field["fieldname"]] = {**field, "table": None}
    for table in schema.get("child_tables", []):
        for field in table["fields"]:
            targets[f"{table['fieldname']}.{field['fieldname']}"] = {**field, "table": table}
    return targets


# --- Column mapping --------------------------------------------------------------------


def resolve_mapping(
    doctype: str, schema: Dict[str, Any], headers: List[str], mapping: Dict[str, Any]
) -> Tuple[Dict[str, str], List[str]]:
    """Turn {column: field} from the model into column_to_field_map entries.

    A column is its header as in the file, or its 0-based position. A field is a fieldname,
    '<table>.<fieldname>', an exact label, or "Don't Import". Returns the entries and a
    message for every one that could not be resolved.
    """
    targets = _mapping_targets(schema)
    by_label = _unique_labels(targets)
    resolved: Dict[str, str] = {}
    problems: List[str] = []

    for column, field in mapping.items():
        position, problem = _column_position(column, headers)
        if problem:
            problems.append(problem)
            continue
        key, problem = _field_key(doctype, field, targets, by_label)
        if problem:
            problems.append(f"Column '{headers[int(position)]}': {problem}")
            continue
        resolved[position] = key

    return resolved, problems


def duplicate_targets(column_to_field_map: Dict[str, str], headers: List[str]) -> List[str]:
    """A message for each field that more than one column maps to."""
    columns_by_field: Dict[str, List[str]] = {}
    for position, field in column_to_field_map.items():
        if field != DONT_IMPORT and position.isdigit() and int(position) < len(headers):
            columns_by_field.setdefault(field, []).append(headers[int(position)])
    return [
        f"Columns {', '.join(repr(c) for c in columns)} all map to '{field}'. Map only one of them."
        for field, columns in columns_by_field.items()
        if len(columns) > 1
    ]


def describe_mapping(
    schema: Dict[str, Any], headers: List[str], column_to_field_map: Dict[str, str]
) -> Dict[str, Any]:
    """The mapping as FACO shows it: parent and child table rows apart, gaps marked."""
    targets = _mapping_targets(schema)
    parent_rows: List[Dict[str, Any]] = []
    child_rows: Dict[str, List[Dict[str, Any]]] = {}
    not_imported, unmapped = [], []
    mapped_fields = set()

    for position, header in enumerate(headers):
        field = column_to_field_map.get(str(position))
        if not field:
            unmapped.append(header)
            continue
        if field == DONT_IMPORT:
            not_imported.append(header)
            continue
        target = targets.get(field)
        if not target:
            unmapped.append(header)
            continue
        mapped_fields.add(field)
        row = {"column": header, "position": position, "field": field, "label": target["label"]}
        if target["table"]:
            child_rows.setdefault(_table_title(target["table"]), []).append(row)
        else:
            parent_rows.append(row)

    mapped_tables = {field.split(".", 1)[0] for field in mapped_fields if "." in field}
    missing_required = []
    for field, target in targets.items():
        table = target["table"]
        if not target.get("required") or target.get("default") or field in mapped_fields:
            continue
        # A child table's required fields matter once any of its columns is mapped.
        if table and table["fieldname"] not in mapped_tables:
            continue
        missing = {"field": field, "label": target["label"]}
        if table:
            missing["table"] = _table_title(table)
        missing_required.append(missing)

    summary = (
        f"{len(parent_rows) + sum(len(rows) for rows in child_rows.values())} columns mapped, "
        f"{len(not_imported)} not imported, {len(unmapped)} unmapped."
    )
    if missing_required:
        summary += (
            f" {len(missing_required)} required field(s) have no column: "
            f"{', '.join(m['label'] for m in missing_required)}."
        )

    return {
        "mapping": parent_rows,
        "child_tables": child_rows,
        "not_imported": not_imported,
        "unmapped_columns": unmapped,
        "missing_required": missing_required,
        "summary": summary,
    }


def _column_position(column: Any, headers: List[str]) -> Tuple[Optional[str], Optional[str]]:
    key = str(column).strip()
    for matches in (
        [i for i, header in enumerate(headers) if header.strip() == key],
        [i for i, header in enumerate(headers) if header.strip().lower() == key.lower()],
    ):
        if len(matches) == 1:
            return str(matches[0]), None
        if len(matches) > 1:
            return None, (
                f"Column '{key}' appears {len(matches)} times in the file. "
                f"Use its 0-based position instead: {', '.join(str(i) for i in matches)}."
            )
    if key.isdigit() and int(key) < len(headers):
        return key, None
    return None, f"'{key}' is not a column in the file. Its columns are: {', '.join(headers)}."


def _field_key(
    doctype: str, field: Any, targets: Dict[str, Dict[str, Any]], by_label: Dict[str, str]
) -> Tuple[Optional[str], Optional[str]]:
    if field is None or str(field).strip() in ("", DONT_IMPORT):
        return DONT_IMPORT, None
    value = str(field).strip()
    if value in targets:
        return value, None
    if value.lower() in by_label:
        return by_label[value.lower()], None

    table_part, _, fieldname = value.rpartition(".")
    df = frappe.get_meta(doctype).get_field(fieldname) if not table_part else None
    if df and (df.hidden or df.read_only):
        return (
            None,
            f"'{value}' is {'hidden' if df.hidden else 'read-only'} on {doctype}, so an import won't set it.",
        )

    candidates = {**{key: key for key in targets}, **by_label}
    close = difflib.get_close_matches(value.lower(), [c.lower() for c in candidates], n=3, cutoff=0.6)
    lookup = {c.lower(): key for c, key in candidates.items()}
    suggestions = list(dict.fromkeys(lookup[c] for c in close))
    problem = f"'{value}' is not an importable field of {doctype}."
    if suggestions:
        problem += " Did you mean " + " or ".join(f"'{s}' ({targets[s]['label']})" for s in suggestions) + "?"
    return None, problem + " Check get_import_schema for the field list."


def _unique_labels(targets: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    """Lowercased label -> target, only for labels that name one field."""
    seen: Dict[str, List[str]] = {}
    for key, target in targets.items():
        # Child fields go by Frappe's import-template label: "Barcode (Barcodes)".
        label = target["label"] if not target["table"] else f"{target['label']} ({target['table']['label']})"
        seen.setdefault(label.lower(), []).append(key)
    return {label: keys[0] for label, keys in seen.items() if len(keys) == 1}


def _table_title(table: Dict[str, Any]) -> str:
    return f"{table['label']} ({table['fieldname']})"


# --- Import session --------------------------------------------------------------------


def load_session(name: str):
    """The session, if this user may change it. Raises DoesNotExistError or PermissionError."""
    doc = frappe.get_doc(SESSION_DOCTYPE, name)
    if not frappe.has_permission(SESSION_DOCTYPE, "write", doc=doc):
        raise frappe.PermissionError(f"You can't change import session {name}.")
    return doc


def session_headers(doc) -> List[str]:
    """The chosen sheet's column headers, as start_import_session stored them."""
    sheets = frappe.parse_json(doc.sheets or "[]") or []
    chosen = [s for s in sheets if s.get("name") == doc.sheet] or sheets[:1]
    return [str(column) for column in (chosen[0].get("columns") if chosen else None) or []]


def session_mapping(doc) -> Dict[str, str]:
    options = frappe.parse_json(doc.template_options or "{}") or {}
    return dict(options.get("column_to_field_map") or {})


def save_mapping(doc, target_doctype: str, column_to_field_map: Dict[str, str], summary: str) -> None:
    """Store the mapping, move the session to Mapped and log the step."""
    options = frappe.parse_json(doc.template_options or "{}") or {}
    options["column_to_field_map"] = column_to_field_map
    doc.target_doctype = target_doctype
    doc.template_options = frappe.as_json(options)
    doc.status = "Mapped"
    doc.log_step("Map Columns", "Success", summary)
    doc.save()
