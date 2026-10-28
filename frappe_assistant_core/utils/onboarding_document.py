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

"""Saving the documents the consultant hands to the client (#34419): setup records, go-live
checklists, training guides and hypercare reports, as FAC Onboarding Documents."""

import hashlib
import json
from typing import Any, Dict, Optional
from urllib.parse import quote

import frappe
from frappe import _

DOCTYPE = "FAC Onboarding Document"
TYPES = ("Setup Record", "Go-Live Checklist", "Training Guide", "Hypercare Report")


def source_hash(facts: Any) -> str:
    """A fingerprint of the facts a document was written from: the same facts give the same hash
    however their keys are ordered, so a changed site shows as a changed hash."""
    return hashlib.sha256(json.dumps(facts, sort_keys=True, default=str).encode()).hexdigest()


def save(
    document_type: str,
    title: str,
    content: str,
    data: Optional[Any] = None,
    company: Optional[str] = None,
    role: Optional[str] = None,
    status: str = "Draft",
) -> Any:
    """Save a document and return it. ``data`` is the facts it was written from (kept as JSON, and
    hashed so a later change to them can be told). The caller is the owner and keeps its own access."""
    if document_type not in TYPES:
        frappe.throw(_("Unknown document type {0}.").format(document_type))
    doc = frappe.get_doc(
        {
            "doctype": DOCTYPE,
            "title": title,
            "document_type": document_type,
            "content": content,
            "company": company,
            "role": role,
            "status": status,
            "data": frappe.as_json(data) if data is not None else None,
            "source_hash": source_hash(data) if data is not None else None,
        }
    )
    doc.insert()
    return doc


def pdf(name: str) -> Dict[str, str]:
    """The document as a PDF: ``{"file_url": ...}`` when ``generate_document`` is available (FAC
    Cloud), else ``{"print_url": ...}``, the page Desk prints it from, so the document is still
    shareable without it."""
    doc = frappe.get_doc(DOCTYPE, name)
    doc.check_permission("read")

    from frappe_assistant_core.core.tool_registry import get_tool_registry

    tool = get_tool_registry().get_tool("generate_document")
    if tool:
        result = tool._safe_execute({"content": doc.content or "", "title": doc.title})
        if result.get("success") and result.get("file_url"):
            return {"file_url": result["file_url"]}
    return {"print_url": f"/printview?doctype={quote(DOCTYPE)}&name={quote(name)}"}
