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
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional

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
_QUANTITY = re.compile(r"^(\(-\)|-)?\s*([\d,]*\.?\d+)\s*(.*)$")
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
