# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Import presets (#34000): recognise a source system's export by its headers, and clean it.

A preset is data: the headers that identify an export, how each column is read, and which
ERPNext record or opening import the rows become. FAC ships presets in data/import_presets/;
other apps add theirs through the assistant_import_presets hook. This module reads, matches and
cleans. It writes nothing: apply_import_preset writes the cleaned files and opens the sessions.
"""

import datetime
import glob
import json
import os
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

import frappe

PARSERS = (
    "amount",
    "debit",
    "credit",
    "quantity",
    "unit",
    "rate",
    "date",
    "percent",
    "last_segment",
    "parent_segment",
    "strip_number",
    "map",
)
NUMERIC_PARSERS = ("amount", "debit", "credit", "quantity", "rate")
JOIN = " › "  # an upper header cell and the one below it: "Closing Balance › Debit"

# -- parsing ---------------------------------------------------------------------------------

_SEPARATORS = re.compile(r"[\s\-–—.'’]+")


def normalise(text: Any) -> str:
    """A header as presets compare it: lower case, dashes, dots, apostrophes and spaces as one space."""
    return _SEPARATORS.sub(" ", str(text or "").lower()).strip()


_SIDE = re.compile(r"(?<![A-Za-z])(dr|cr)\.?\s*$", re.I)
_CURRENCY = re.compile(r"^[A-Z]{3}\s+|[₹$€£¥]")
# A quantity is a number followed by nothing, or by a unit that starts with a letter or "/".
# Anything else ("1.2.3", "2024-04-01", "10-20") is not a quantity and is kept as written.
_QUANTITY = re.compile(r"^(\(-\)|-)?\s*([\d,]*\.?\d+)\s*(?=$|[A-Za-z/])(.*)$")
_DATE_TOKENS = re.compile(r"yyyy|yy|MMM|MM|M|dd|d")
_STRPTIME = {"yyyy": "%Y", "yy": "%y", "MMM": "%b", "MM": "%m", "M": "%m", "dd": "%d", "d": "%d"}
_NUMBERED = re.compile(r"^\s*(\S+)\s+·\s+(.+)$")


def _number(text: str) -> Optional[Decimal]:
    try:
        return Decimal(str(text).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None


def _plain(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return "0" if text in ("-0", "") else text


def _signed(text: str) -> Optional[Decimal]:
    """`₹ 1,23,456.00`, `-$175,804.25`, `(1,250)`, `(-)1,250`, `8,45,000 Dr`: the number, Dr positive."""
    s = text.strip()
    sign = 1
    side = _SIDE.search(s)
    if side:
        sign = -1 if side.group(1).lower() == "cr" else 1
        s = s[: side.start()].strip()
    s = _CURRENCY.sub("", s).strip()
    if s.startswith("(-)"):
        sign, s = -sign, s[3:]
    elif s.startswith("(") and s.endswith(")"):
        sign, s = -sign, s[1:-1]
    s = s.replace(",", "").replace(" ", "")
    if s.startswith("-"):
        sign, s = -sign, s[1:]
    if not s or not re.fullmatch(r"\d*\.?\d+", s):
        return None
    return sign * Decimal(s)


def _date(text: str, formats) -> str:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    for fmt in formats or ():
        pattern = _DATE_TOKENS.sub(lambda m: _STRPTIME[m.group(0)], fmt)
        try:
            return datetime.datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            continue
    return text


def _parse_one(text: str, kind: str, spec: Dict[str, Any]) -> str:
    if kind in ("amount", "debit", "credit"):
        number = _signed(text)
        if number is None:
            return text
        if spec.get("sign") == "credit_positive":
            number = -number
        if kind == "debit":
            return _plain(number) if number > 0 else ""
        if kind == "credit":
            return _plain(-number) if number < 0 else ""
        return _plain(number)
    if kind in ("quantity", "unit"):
        found = _QUANTITY.match(text)
        if not found:
            return text
        if kind == "unit":
            return found.group(3).strip().lstrip("/").strip()
        number = Decimal(found.group(2).replace(",", ""))
        return _plain(-number if found.group(1) else number)
    if kind == "rate":
        number = _signed(text.split("/", 1)[0])
        return text if number is None else _plain(number)
    if kind == "date":
        return _date(text, spec.get("formats"))
    if kind == "percent":
        found = re.search(r"\d+(?:\.\d+)?", text)
        return _plain(Decimal(found.group(0))) if found else text
    sep = spec.get("sep", ":")
    if kind == "last_segment":
        return text.split(sep)[-1].strip()
    if kind == "parent_segment":
        parts = text.split(sep)
        return parts[-2].strip() if len(parts) > 1 else ""
    if kind == "strip_number":
        found = _NUMBERED.match(text)
        if spec.get("keep") == "number":
            return found.group(1) if found else ""
        return found.group(2).strip() if found else text
    if kind == "map":
        values = {normalise(k): v for k, v in (spec.get("values") or {}).items()}
        return values.get(normalise(text), spec.get("default", text))
    return text


def parse(value: str, spec: Dict[str, Any]) -> str:
    """One cell, read the way a preset column says. A value no parser understands is kept as it
    was, so the dry run reports it; it never becomes 0."""
    text = (value or "").strip()
    kinds = spec.get("parse") or []
    for kind in [kinds] if isinstance(kinds, str) else kinds:
        if not text:
            break
        text = _parse_one(text, kind, spec)
    if not text and "default" in spec:
        return str(spec["default"])
    return text


# -- presets ---------------------------------------------------------------------------------

PRESET_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "import_presets"
)
HOOK = "assistant_import_presets"
ENGINES = {
    "opening_stock": "set_opening_stock",
    "opening_invoices": "set_opening_invoices",
    "opening_balances": "set_opening_balances",
}
GROUP_METHODS = ("empty_amounts", "indent", "known_names")
WHEN_KEYS = ("in", "not_in", "nonzero")


def _engine_fields(engine: str):
    from frappe_assistant_core.plugins.core import import_opening_balances as balances
    from frappe_assistant_core.plugins.core import import_opening_invoices as invoices
    from frappe_assistant_core.plugins.core import import_opening_stock as stock

    return {
        "opening_stock": stock.ITEM_FIELDS + stock.EXTRA_FIELDS,
        "opening_invoices": invoices.FIELDS,
        "opening_balances": balances.FIELDS,
    }[engine]


def engine_doctype(target: Dict[str, Any]) -> str:
    """The record an engine target ends up creating, for the permission check."""
    if target["engine"] == "opening_invoices":
        return f"{target.get('invoice_type')} Invoice"
    return {"opening_stock": "Stock Reconciliation", "opening_balances": "Journal Entry"}[target["engine"]]


def target_mapping(target: Dict[str, Any]) -> Dict[str, Any]:
    """Output column -> field (or candidate fields, or an engine's field), constants and inputs included."""
    mapping = dict(target.get("mapping") or target.get("columns") or {})
    for extra in (target.get("constants") or []) + (target.get("inputs") or []):
        mapping[extra["column"]] = extra["field"]
    return mapping


def _alias_groups(value, allow_empty=False) -> bool:
    if not isinstance(value, list) or (not value and not allow_empty):
        return False
    return all(
        isinstance(group, list) and group and all(isinstance(a, str) and a.strip() for a in group)
        for group in value
    )


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(s, str) for s in value)


def _field_spec(value: Any) -> bool:
    """A field name, or a non-empty list of candidate field names."""
    if isinstance(value, str):
        return True
    return isinstance(value, list) and bool(value) and _string_list(value)


def validate_preset(preset: Any) -> Optional[str]:
    """Why ``preset`` can't be used, or None."""
    if not isinstance(preset, dict):
        return "a preset must be a JSON object"
    for key in ("id", "system", "export"):
        if not isinstance(preset.get(key), str) or not preset[key].strip():
            return f"'{key}' is required"
    signature = preset.get("signature")
    if not isinstance(signature, dict) or not _alias_groups(signature.get("required")):
        return "signature.required must be a non-empty list of alias lists"
    if signature.get("optional") is not None and not _alias_groups(signature["optional"], allow_empty=True):
        return "signature.optional must be a list of alias lists"
    if signature.get("title") is not None and not _alias_groups([signature["title"]]):
        return "signature.title must be a list of patterns"
    header = preset.get("header") or {}
    if not isinstance(header, dict):
        return "header must be an object"
    if header.get("rows", 1) not in (1, 2):
        return "header.rows must be 1 or 2"
    if header.get("search_rows") is not None and not (
        _is_int(header["search_rows"]) and header["search_rows"] > 0
    ):
        return "header.search_rows must be a positive integer"

    columns = preset.get("columns")
    if not isinstance(columns, dict) or not columns:
        return "columns must be a non-empty object"
    for name, spec in columns.items():
        if not isinstance(spec, dict):
            return f"column '{name}' must be an object"
        kinds = spec.get("parse") or []
        for kind in [kinds] if isinstance(kinds, str) else kinds:
            if kind not in PARSERS:
                return f"column '{name}': unknown parse '{kind}'"
        if "map" in ([kinds] if isinstance(kinds, str) else kinds) and not isinstance(
            spec.get("values"), dict
        ):
            return f"column '{name}': parse 'map' needs 'values'"
        if spec.get("fallback") and spec["fallback"] not in columns:
            return f"column '{name}': fallback '{spec['fallback']}' is not a column"
        if "from" in spec and not (
            isinstance(spec["from"], list) and all(isinstance(a, str) and a.strip() for a in spec["from"])
        ):
            return f"column '{name}': from must be a list of non-empty column names"
        index = spec.get("from_index")
        if index is not None and not (
            _is_int(index) or (isinstance(index, list) and all(_is_int(i) for i in index))
        ):
            return f"column '{name}': from_index must be an int or a list of ints"

    groups = preset.get("groups")
    if groups:
        if not isinstance(groups, dict):
            return "groups must be an object"
        methods = groups.get("method")
        methods = methods if isinstance(methods, list) else [methods]
        if not methods or any(m not in GROUP_METHODS for m in methods):
            return f"groups.method must be among {', '.join(GROUP_METHODS)}"
        if groups.get("column") not in columns:
            return "groups.column must be a column"
        if groups.get("carry_to") and groups["carry_to"] not in columns:
            return "groups.carry_to must be a column"
        if groups.get("names") is not None and not _string_list(groups["names"]):
            return "groups.names must be a list of strings"
    skip = preset.get("skip") or {}
    if not isinstance(skip, dict):
        return "skip must be an object"
    for key in ("totals", "footer"):
        if skip.get(key) is not None and not _string_list(skip[key]):
            return f"skip.{key} must be a list of strings"
    distinct = preset.get("distinct_by")
    if distinct is not None and not isinstance(distinct, list):
        return "distinct_by must be a list"
    for key in distinct or []:
        if key not in columns:
            return f"distinct_by '{key}' is not a column"

    targets = preset.get("targets")
    if not isinstance(targets, list) or not targets:
        return "targets must be a non-empty list"
    names = set()
    for target in targets:
        if not isinstance(target, dict) or not target.get("name"):
            return "every target needs a name"
        name = target["name"]
        if name in names:
            return f"target '{name}' appears twice"
        names.add(name)
        if bool(target.get("doctype")) == bool(target.get("engine")):
            return f"target '{name}' needs exactly one of doctype or engine"
        when = target.get("when")
        if when is not None and not isinstance(when, dict):
            return f"target '{name}': when must be an object"
        if when and (when.get("column") not in columns or not any(k in when for k in WHEN_KEYS)):
            return f"target '{name}': when needs a column and one of {', '.join(WHEN_KEYS)}"
        for key in ("in", "not_in"):
            if when and key in when and not _string_list(when[key]):
                return f"target '{name}': when '{key}' must be a list of strings"
        for constant in target.get("constants") or []:
            if not all(isinstance(constant.get(k), str) for k in ("column", "field", "value")):
                return f"target '{name}': a constant needs column, field and value"
        for item in target.get("inputs") or []:
            if not all(isinstance(item.get(k), str) and item[k] for k in ("name", "column", "field", "ask")):
                return f"target '{name}': an input needs name, column, field and ask"
        base = target.get("mapping") or target.get("columns") or {}
        if not isinstance(base, dict):
            return f"target '{name}': mapping must be an object"
        for column, value in base.items():
            if not _field_spec(value):
                return f"target '{name}': '{column}' must map to a field or a list of fields"
        known = set(columns) | {
            e["column"] for e in (target.get("constants") or []) + (target.get("inputs") or [])
        }
        for column in target_mapping(target):
            if column not in known:
                return f"target '{name}' maps unknown column '{column}'"
        engine = target.get("engine")
        if engine:
            if engine not in ENGINES:
                return f"target '{name}': unknown engine '{engine}'"
            if engine == "opening_invoices" and target.get("invoice_type") not in ("Sales", "Purchase"):
                return f"target '{name}': invoice_type must be Sales or Purchase"
            allowed = _engine_fields(engine)
            for value in target_mapping(target).values():
                for field in [value] if isinstance(value, str) else value:
                    if field not in allowed:
                        return f"target '{name}': '{field}' is not a field of {engine}"
    return None


def _preset_files() -> List[str]:
    """FAC's preset files, then each hooked app's, in hook order."""
    files = sorted(glob.glob(os.path.join(PRESET_DIR, "*.json")))
    for entry in frappe.get_hooks(HOOK) or []:
        try:
            path = frappe.get_app_path(entry["app"], entry["path"])
        except Exception:
            frappe.log_error(title="Import preset hook skipped", message=repr(entry))
            continue
        files += sorted(glob.glob(os.path.join(path, "*.json"))) if os.path.isdir(path) else [path]
    return files


def _read_file(path: str) -> List[Any]:
    try:
        with open(path, encoding="utf-8") as f:
            content = json.load(f)
    except Exception as e:
        frappe.log_error(title="Import preset file skipped", message=f"{path}: {e}")
        return []
    return content if isinstance(content, list) else [content]


def load_presets() -> List[Dict[str, Any]]:
    """Every valid preset, FAC's first; a later preset with the same id replaces the earlier one.
    Read once per request."""
    cached = getattr(frappe.local, "fac_import_presets", None)
    if cached is not None:
        return cached
    by_id: Dict[str, Dict[str, Any]] = {}
    for path in _preset_files():
        for preset in _read_file(path):
            try:
                problem = validate_preset(preset)
            except Exception as e:
                # A shape validate_preset does not expect is this preset's problem only.
                problem = f"{type(e).__name__}: {e}"
            if problem:
                ident = preset.get("id") if isinstance(preset, dict) else None
                frappe.log_error(title="Import preset skipped", message=f"{path}: {ident}: {problem}")
                continue
            by_id[preset["id"]] = preset
    frappe.local.fac_import_presets = list(by_id.values())
    return frappe.local.fac_import_presets


def clear_cache() -> None:
    frappe.local.fac_import_presets = None
