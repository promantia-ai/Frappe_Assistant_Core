# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
File content in a tool's result, fenced before the model reads it (#34003).

An import tool hands the model what it read from the user's file: headers, sample rows, the
values a check found wrong. A cell can say anything, "ignore previous instructions" included,
so those fields reach the model inside the untrusted-content fence chat attachments use
(wrap_untrusted): data, never instructions. A tool names them in ``untrusted_fields``; the MCP
server fences them as it serialises the result, so tools and their tests keep plain values.
"""

import json
from typing import Any, Dict, Iterable, List

from frappe_assistant_core.chat.api._untrusted import wrap_untrusted

FENCE = "import_file_content"

# Records that keep what a user's file said, and those fields: fenced when a tool returns the
# record (get_document), as the import tools' replies are. A file checked in the background
# leaves its result on the session, and the model reads it there.
DOCUMENT_FIELDS = {
    "FAC Import Session": (
        "file_name",
        "sheet",
        "sheets",
        "transformation_rules",
        "excluded_rows",
        "validation_summary",
        "steps[].file",
        "steps[].message",
        "created_records[].record_name",
        "created_records[].parent_record",
    ),
}


def fence_fields(result: Dict[str, Any], fields: Iterable[str], kind: str = FENCE) -> Dict[str, Any]:
    """A copy of ``result`` with each of ``fields`` it holds replaced by its value, as JSON,
    inside the fence. ``"groups[].examples"`` fences ``examples`` in each item of ``groups``.
    Fields it doesn't hold, or holds empty, stay as they are."""
    out = dict(result)
    for path in fields:
        _fence(out, path.split("."), kind)
    return out


def fence_document(doctype: str, doc: Dict[str, Any]) -> Dict[str, Any]:
    """``doc`` (a record as a dict) with the fields of DOCUMENT_FIELDS fenced, for its DocType."""
    fields = DOCUMENT_FIELDS.get(doctype)
    return fence_fields(doc, fields) if fields else doc


def _fence(node: Dict[str, Any], parts: List[str], kind: str) -> None:
    head, rest = parts[0], parts[1:]
    if head.endswith("[]"):
        key = head[:-2]
        items = node.get(key)
        if isinstance(items, list) and rest:
            node[key] = [_fenced_item(item, rest, kind) for item in items]
        return
    if node.get(head) in (None, "", [], {}):
        return
    if rest:
        if isinstance(node[head], dict):
            node[head] = dict(node[head])
            _fence(node[head], rest, kind)
        return
    value = node[head]
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    node[head] = wrap_untrusted(text, kind=kind)


def _fenced_item(item: Any, rest: List[str], kind: str) -> Any:
    if not isinstance(item, dict):
        return item
    item = dict(item)
    _fence(item, rest, kind)
    return item
