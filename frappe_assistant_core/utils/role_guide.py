# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Role guides (#34425): a short guide per role, written from this site's own setup, kept as a FAC Skill
of type "Role Guide" shared with the role, so it survives chat retention and FACO follows it when a
user of that role asks "how do I make an invoice?".

What a guide is written from is read here, from the DocType meta, the active Workflow and the Print
Formats, never from the model's general knowledge of ERPNext. Saving a guide records that setup;
opening it later compares the setup again and puts a note on top when something changed.
"""

import json
from typing import Any, Dict, List

import frappe
from frappe import _

ROLE_GUIDE = "Role Guide"
#: Field types that hold no value, so a user never fills them in
NO_VALUE = ("Section Break", "Column Break", "Tab Break", "HTML", "Button", "Fold", "Heading", "Image")
#: What a field rule is made of; a change to any of them changes what the user must do
RULE_KEYS = ("options", "depends_on", "mandatory_depends_on")


def doctypes_of(text: str | None) -> List[str]:
    """The DocTypes a guide covers, one per line or comma separated, in order without repeats."""
    names = (part.strip() for line in (text or "").splitlines() for part in line.split(","))
    return list(dict.fromkeys(name for name in names if name))


def _field(df) -> Dict[str, Any]:
    row = {"fieldname": df.fieldname, "label": df.label, "fieldtype": df.fieldtype}
    for key in RULE_KEYS:
        if df.get(key):
            row[key] = df.get(key)
    if df.get("is_custom_field"):
        row["custom"] = 1
    return row


def _mandatory(meta) -> List[Dict[str, Any]]:
    """Fields the user must fill in: required, or required when a condition holds. Hidden and
    read-only fields are left out: the system fills those."""
    return [
        _field(df)
        for df in meta.fields
        if df.fieldtype not in NO_VALUE
        and not df.hidden
        and not df.read_only
        and (df.reqd or df.mandatory_depends_on)
    ]


def _tables(meta) -> Dict[str, List[Dict[str, Any]]]:
    """The mandatory fields of each visible child table, by the table's label."""
    tables = {}
    for df in meta.get_table_fields():
        if df.hidden or df.read_only:
            continue
        rows = _mandatory(frappe.get_meta(df.options))
        if rows:
            tables[df.label or df.fieldname] = rows
    return tables


def _naming(meta) -> Dict[str, Any]:
    df = meta.get_field("naming_series")
    if not df:
        return {"rule": meta.naming_rule or meta.autoname or ""}
    options = [line for line in (df.options or "").splitlines() if line.strip()]
    return {"series": options, "default": df.default or (options[0] if options else "")}


def _workflow(doctype: str) -> Dict[str, Any]:
    from frappe.model.workflow import get_workflow_name

    name = get_workflow_name(doctype)
    if not name:
        return {}
    workflow = frappe.get_cached_doc("Workflow", name)
    return {
        "name": name,
        "states": [
            {"state": s.state, "doc_status": s.doc_status, "edited_by": s.allow_edit} for s in workflow.states
        ],
        "transitions": [
            {"from": t.state, "action": t.action, "to": t.next_state, "allowed": t.allowed}
            for t in workflow.transitions
        ],
    }


def setup_facts(doctype: str) -> Dict[str, Any]:
    """What a user of this site has to know to make one ``doctype``: its mandatory fields (custom ones
    marked), child table rows, naming series, approval workflow and print formats."""
    meta = frappe.get_meta(doctype)
    return {
        "doctype": doctype,
        "is_submittable": bool(meta.is_submittable),
        "mandatory": _mandatory(meta),
        "tables": _tables(meta),
        "naming": _naming(meta),
        "workflow": _workflow(doctype),
        "default_print_format": meta.default_print_format or "Standard",
        "print_formats": sorted(
            frappe.get_all("Print Format", filters={"doc_type": doctype, "disabled": 0}, pluck="name")
        ),
    }


def snapshot(doctypes: List[str]) -> Dict[str, Any]:
    """The setup of every DocType a guide covers; one that no longer exists is None."""
    return {
        doctype: setup_facts(doctype) if frappe.db.exists("DocType", doctype) else None
        for doctype in doctypes
    }


# -- what changed ---------------------------------------------------------------------------


def _by(rows: List[Dict[str, Any]], key: str) -> Dict[str, Dict[str, Any]]:
    return {row[key]: row for row in rows or []}


def _field_changes(where: str, old: List[Dict], new: List[Dict]) -> List[str]:
    before, after = _by(old, "fieldname"), _by(new, "fieldname")
    lines = []
    for name, row in after.items():
        label = row.get("label") or name
        if name not in before:
            lines.append(_("{0}: new mandatory field {1}.").format(where, label))
        elif any(before[name].get(key) != row.get(key) for key in RULE_KEYS):
            lines.append(_("{0}: the rule for {1} changed.").format(where, label))
    for name, row in before.items():
        if name not in after:
            lines.append(_("{0}: {1} is no longer mandatory.").format(where, row.get("label") or name))
    return lines


def _workflow_changes(doctype: str, old: Dict, new: Dict) -> List[str]:
    if old.get("name") != new.get("name"):
        if not new:
            return [_("{0}: the approval workflow {1} is no longer active.").format(doctype, old["name"])]
        return [_("{0}: the approval workflow is now {1}.").format(doctype, new["name"])]
    if not new:
        return []
    lines = []
    before, after = _by(old.get("states"), "state"), _by(new.get("states"), "state")
    lines += [_("{0}: new workflow state {1}.").format(doctype, s) for s in after if s not in before]
    lines += [_("{0}: workflow state {1} was removed.").format(doctype, s) for s in before if s not in after]
    if old.get("transitions") != new.get("transitions"):
        lines.append(_("{0}: who approves what in the workflow changed.").format(doctype))
    return lines


def changes(old: Dict[str, Any], new: Dict[str, Any]) -> List[str]:
    """What changed in the setup since the guide was written, as plain sentences."""
    lines = []
    for doctype, then in old.items():
        now = new.get(doctype)
        if then and not now:
            lines.append(_("{0} no longer exists.").format(doctype))
            continue
        if not then or not now:
            continue
        lines += _field_changes(doctype, then.get("mandatory"), now.get("mandatory"))
        for table in sorted(set(then.get("tables") or {}) | set(now.get("tables") or {})):
            lines += _field_changes(
                f"{doctype} ({table})",
                (then.get("tables") or {}).get(table, []),
                (now.get("tables") or {}).get(table, []),
            )
        lines += _workflow_changes(doctype, then.get("workflow") or {}, now.get("workflow") or {})
        if then.get("naming") != now.get("naming"):
            lines.append(_("{0}: the naming series changed.").format(doctype))
        if then.get("default_print_format") != now.get("default_print_format"):
            lines.append(
                _("{0}: the default print format is now {1}.").format(
                    doctype, now.get("default_print_format")
                )
            )
        added = sorted(set(now.get("print_formats") or []) - set(then.get("print_formats") or []))
        if added:
            lines.append(_("{0}: new print format {1}.").format(doctype, ", ".join(added)))
    return lines


def record(skill) -> None:
    """On save of a role guide: check what it covers and record the setup it was written from.
    Recorded again only when the guide itself changes, so a change to its sharing doesn't hide
    that the setup moved on."""
    doctypes = doctypes_of(skill.guide_doctypes)
    if not doctypes:
        frappe.throw(_("List the DocTypes this role guide covers, for example Sales Invoice."))
    missing = [doctype for doctype in doctypes if not frappe.db.exists("DocType", doctype)]
    if missing:
        frappe.throw(_("DocType {0} not found.").format(", ".join(missing)))
    skill.guide_doctypes = "\n".join(doctypes)
    if (
        skill.is_new()
        or not skill.setup_snapshot
        or any(skill.has_value_changed(field) for field in ("content", "guide_doctypes"))
    ):
        skill.setup_snapshot = json.dumps(snapshot(doctypes), sort_keys=True)


def stale_note(skill) -> str:
    """A note to put on top of a role guide whose setup changed since it was written, or ""."""
    if skill.get("skill_type") != ROLE_GUIDE or not skill.get("setup_snapshot"):
        return ""
    old = json.loads(skill.setup_snapshot)
    lines = changes(old, snapshot(list(old)))
    if not lines:
        return ""
    listed = "\n".join(f"> - {line}" for line in lines)
    return (
        "> **"
        + _("This guide is out of date.")
        + "** "
        + _("The setup changed after it was written:")
        + f"\n{listed}\n> "
        + _("Tell the user, and offer to update the guide (get_skill('role-guides')).")
        + "\n\n"
    )
