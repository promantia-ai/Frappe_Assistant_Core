# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Undo an import (#34002): what a session created, and whether it can be undone now.

The undo set is the session's Created Records plus, for an Insert New Records session, the
records its Data Imports logged as created. The plan says, for each of them, whether it can be
removed: what blocks it (spec §4), and what a blocked record keeps with it. This module only
reads; the delete order and the job are added on top of it.
"""

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Set, Tuple

import frappe
from frappe import _
from frappe.utils import cint, get_datetime, get_time, getdate

INSERT = "Insert New Records"
UPDATE = "Update Existing Records"
UNDO_DAYS = 30  # default of the site config key fac_import_undo_days
UNDO_GRACE_MINUTES = 10  # ERPNext may submit large opening documents in the background, later
CHUNK = 1000  # names per IN (...) of one query
# A submitted document's own ledger and repost entries: ERPNext's on_cancel lists them in
# ignore_linked_doctypes, so they never block the cancel. A later document that uses the record
# (a Payment Entry against an invoice) still blocks through its own fields.
OWN_ENTRIES = {
    "GL Entry",
    "Stock Ledger Entry",
    "Payment Ledger Entry",
    "Advance Payment Ledger Entry",
    "Repost Item Valuation",
    "Repost Payment Ledger",
    "Repost Payment Ledger Items",
    "Repost Accounting Ledger",
    "Repost Accounting Ledger Items",
    "Unreconcile Payment",
    "Unreconcile Payment Entries",
    "Serial and Batch Bundle",
}

Key = Tuple[str, str]


@dataclass
class Record:
    doctype: str
    name: str
    parent: Optional[str] = None


class UndoProblem(Exception):
    """Something the user is told about, as it is worded."""


def finished_at(session) -> datetime:
    """When the session's last import step finished: its last successful Import step. A session
    with no Import step (created masters only) uses its latest step of any kind, and one with
    no steps its creation time. Step timestamps are written once, so this does not move when
    the session is saved."""
    imports = [
        get_datetime(s.timestamp)
        for s in session.steps
        if s.step == "Import" and s.outcome == "Success" and s.timestamp
    ]
    if imports:
        return max(imports)
    stamps = [get_datetime(s.timestamp) for s in session.steps if s.timestamp]
    return max(stamps) if stamps else get_datetime(session.creation)


def _days() -> int:
    return int(frappe.conf.get("fac_import_undo_days", UNDO_DAYS))


def window_ends(session) -> datetime:
    return finished_at(session) + timedelta(days=_days())


def refusal(session) -> str:
    """Why the session can't be undone now, or "" when it can."""
    if session.status == "Importing":
        return _("This session is still importing. Wait for the import to finish, then try again.")
    if session.status == "Undoing":
        return _("This import is already being undone.")
    if session.status == "Undone":
        return _("This import was already undone.")
    if not session.created_records and not session.data_imports:
        return _("This session has nothing to undo: it has not imported or created anything.")
    if frappe.utils.now_datetime() > window_ends(session):
        return _(
            "This import finished on {0}, more than {1} days ago, so it can no longer be undone here."
        ).format(finished_at(session).date(), _days())
    return ""


def _exists(doctype: str, name: str) -> bool:
    return bool(frappe.db.exists(doctype, name))


def undo_set(session) -> Tuple[List[Record], List[Record], Optional[str]]:
    """(present, already removed, note): the records the session created. A record that no
    longer exists is listed as already removed, never as an error."""
    found: List[Record] = []
    seen: Set[Tuple[str, str]] = set()

    def add(doctype: str, name: str, parent: Optional[str] = None) -> None:
        if doctype and name and (doctype, name) not in seen:
            seen.add((doctype, name))
            found.append(Record(doctype, name, parent or None))

    for row in session.created_records:
        add(row.record_doctype, row.record_name, row.parent_record)

    note = None
    if session.import_type == INSERT:
        done: Set[str] = set()
        for link in session.data_imports:
            if not link.data_import or link.data_import in done:
                continue
            done.add(link.data_import)
            doctype = frappe.db.get_value("Data Import", link.data_import, "reference_doctype")
            for docname in frappe.get_all(
                "Data Import Log",
                fields=["docname"],
                filters={"data_import": link.data_import, "success": 1},
                pluck="docname",
                order_by="creation asc",
                limit_page_length=0,
            ):
                add(doctype, docname)
    elif session.import_type == UPDATE and session.data_imports:
        note = _(
            "This session imported with Update Existing Records: those records existed before the "
            "import and are never deleted, so there is nothing to undo for them."
        )

    present: List[Record] = []
    removed: List[Record] = []
    for record in found:
        (present if _exists(record.doctype, record.name) else removed).append(record)
    return present, removed, note


# -- the plan ------------------------------------------------------------------------------


@dataclass
class Blocker:
    record: Record
    reason: str  # linked, changed, closed_books, later_stock, no_permission (delete_failed: the job)
    detail: str
    blocked_by: Optional[str] = None  # the blocking document, "Doctype name"


@dataclass
class Plan:
    records: List[Record]  # everything in the set that is still present
    blocked: List[Blocker] = field(default_factory=list)
    kept: List[Tuple[Record, str]] = field(default_factory=list)  # (record, kept_because)
    already_removed: List[Record] = field(default_factory=list)
    note: Optional[str] = None
    fingerprint: str = ""

    @property
    def to_delete(self) -> List[Record]:
        held = {_key(b.record) for b in self.blocked} | {_key(r) for r, _because in self.kept}
        return [r for r in self.records if _key(r) not in held]


def _key(record: Record) -> Key:
    return (record.doctype, record.name)


def _by_doctype(records: Iterable[Record]) -> Dict[str, List[str]]:
    grouped: Dict[str, List[str]] = defaultdict(list)
    for record in records:
        grouped[record.doctype].append(record.name)
    return grouped


def _chunks(names: List[str]) -> Iterable[List[str]]:
    for start in range(0, len(names), CHUNK):
        yield names[start : start + CHUNK]


def _label(doctype: str, name: str) -> str:
    return f"{doctype} {name}"


# -- linked --------------------------------------------------------------------------------


def link_blockers(records: List[Record], ignore: Set[Key]) -> List[Blocker]:
    """Documents outside ``records`` that link to them, by the rules of Frappe's own delete
    check (frappe.model.delete_doc.get_linked_docs and get_dynamic_linked_docs): Link fields,
    Dynamic Link fields and child-table rows (counted as their parent document); cancelled
    documents, the doctypes in the ``ignore_links_on_delete`` hook and a submitted record's own
    ledger entries (OWN_ENTRIES, which ERPNext ignores when cancelling) never block. One query per
    linking field (and per CHUNK names), never one per record. ``ignore`` holds documents whose
    links never block (the session itself). One blocker per record, naming the first linker."""
    from frappe.model.dynamic_links import get_dynamic_link_map
    from frappe.model.rename_doc import get_link_fields

    in_set = {_key(r) for r in records}
    on_delete = set(frappe.get_hooks("ignore_links_on_delete") or [])
    found: Dict[Key, List[Key]] = defaultdict(list)

    def note(target: Key, linker: Key, skip: Set[str]) -> None:
        if target not in in_set or linker in in_set or linker in ignore or linker[0] in skip:
            return
        if linker not in found[target]:
            found[target].append(linker)

    def scan(link_dt, fieldname, doctype, names, skip, doctype_field=None) -> None:
        try:
            meta = frappe.get_meta(link_dt)
        except frappe.DoesNotExistError:
            frappe.clear_last_message()  # an app's leftover customization, as Frappe skips it
            return
        if meta.issingle:
            values = frappe.db.get_singles_dict(link_dt)
            if (
                values.get(fieldname) in names
                and cint(values.get("docstatus")) != 2
                and (not doctype_field or values.get(doctype_field) == doctype)
            ):
                note((doctype, values.get(fieldname)), (link_dt, link_dt), skip)
            return
        table = frappe.qb.DocType(link_dt)
        columns = [table.name, table.docstatus, table[fieldname].as_("target")]
        if meta.istable:
            columns += [table.parent, table.parenttype]
        for chunk in _chunks(names):
            query = frappe.qb.from_(table).select(*columns).where(table[fieldname].isin(chunk))
            if doctype_field:
                query = query.where(table[doctype_field] == doctype)
            if link_dt == "Submission Queue":
                query = query.where(table.status == "Queued")
            for row in query.run(as_dict=True):
                if cint(row.docstatus) == 2:
                    continue
                if not meta.istable:
                    note((doctype, row.target), (link_dt, row.name), skip)
                elif row.parent and row.parenttype:
                    note((doctype, row.target), (row.parenttype, row.parent), skip)

    dynamic_map = get_dynamic_link_map()
    for doctype, names in _by_doctype(records).items():
        skip = on_delete | (OWN_ENTRIES if frappe.get_meta(doctype).is_submittable else set())
        seen: Set[Tuple[str, str]] = set()
        for lf in get_link_fields(doctype):
            if lf["parent"] not in skip and (lf["parent"], lf["fieldname"]) not in seen:
                seen.add((lf["parent"], lf["fieldname"]))
                scan(lf["parent"], lf["fieldname"], doctype, names, skip)
        for df in dynamic_map.get(doctype, []):
            if df.parent not in skip and (df.parent, df.fieldname) not in seen:
                seen.add((df.parent, df.fieldname))
                scan(df.parent, df.fieldname, doctype, names, skip, doctype_field=df.options)

    blockers = []
    for record in records:
        linkers = found.get(_key(record))
        if not linkers:
            continue
        first = _label(*linkers[0])
        if len(linkers) > 1:
            detail = _("{0} links to it ({1} more).").format(first, len(linkers) - 1)
        else:
            detail = _("{0} links to it.").format(first)
        blockers.append(Blocker(record, "linked", detail, first))
    return blockers


# -- changed -------------------------------------------------------------------------------


def _changed_blockers(records: List[Record], session) -> List[Blocker]:
    """Edited by another user than the session's, or later than the import plus the grace."""
    user = session.user or session.owner
    finished = finished_at(session)
    latest = finished + timedelta(minutes=UNDO_GRACE_MINUTES)
    rows: Dict[Key, frappe._dict] = {}
    for doctype, names in _by_doctype(records).items():
        for chunk in _chunks(names):
            for row in frappe.get_all(
                doctype,
                filters={"name": ["in", chunk]},
                fields=["name", "modified", "modified_by"],
                limit_page_length=0,
            ):
                rows[(doctype, row.name)] = row
    blockers = []
    for record in records:
        row = rows.get(_key(record))
        if not row:
            continue
        if row.modified_by and row.modified_by != user:
            detail = _("{0} changed it on {1}, after the import.").format(row.modified_by, row.modified)
        elif get_datetime(row.modified) > latest:
            detail = _("It was changed on {0}, after the import finished ({1}).").format(
                row.modified, finished
            )
        else:
            continue
        blockers.append(Blocker(record, "changed", detail, None))
    return blockers


# -- closed books --------------------------------------------------------------------------


def _frozen_till() -> Optional[date]:
    """Accounts Settings' Accounts Frozen Till Date, if set."""
    if not frappe.db.exists("DocType", "Accounts Settings"):
        return None
    value = frappe.db.get_single_value("Accounts Settings", "acc_frozen_upto")
    return getdate(value) if value else None


def _closed_till(companies: Iterable[str]) -> Dict[str, date]:
    """Per company, the end of its last period closed by a submitted Period Closing Voucher:
    ERPNext refuses entries on or before that date."""
    companies = sorted({c for c in companies if c})
    if not companies or not frappe.db.exists("DocType", "Period Closing Voucher"):
        return {}
    rows = frappe.get_all(
        "Period Closing Voucher",
        filters={"docstatus": 1, "company": ["in", companies]},
        fields=["company", "max(period_end_date) as period_end_date"],
        group_by="company",
    )
    return {r.company: getdate(r.period_end_date) for r in rows if r.period_end_date}


def _closed_books_blockers(records: List[Record]) -> List[Blocker]:
    """Submitted documents posted on or before Accounts Frozen Till, or in a closed period."""
    dated: Dict[Key, frappe._dict] = {}
    for doctype, names in _by_doctype(records).items():
        meta = frappe.get_meta(doctype)
        if not meta.is_submittable:
            continue
        date_field = next((f for f in ("posting_date", "transaction_date") if meta.has_field(f)), None)
        if not date_field:
            continue
        fields = ["name", f"{date_field} as posting_date"]
        if meta.has_field("company"):
            fields.append("company")
        for chunk in _chunks(names):
            for row in frappe.get_all(
                doctype,
                filters={"name": ["in", chunk], "docstatus": 1},
                fields=fields,
                limit_page_length=0,
            ):
                if row.posting_date:
                    dated[(doctype, row.name)] = row
    if not dated:
        return []
    frozen = _frozen_till()
    closed = _closed_till(row.get("company") for row in dated.values())
    blockers = []
    for record in records:
        row = dated.get(_key(record))
        if not row:
            continue
        posted, company = getdate(row.posting_date), row.get("company")
        if frozen and posted <= frozen:
            detail = _("Its posting date {0} is on or before Accounts Frozen Till ({1}).").format(
                posted, frozen
            )
        elif company in closed and posted <= closed[company]:
            detail = _(
                "Its posting date {0} is in a period closed by a Period Closing Voucher of {1} (up to {2})."
            ).format(posted, company, closed[company])
        else:
            continue
        blockers.append(Blocker(record, "closed_books", detail, None))
    return blockers


# -- later stock ---------------------------------------------------------------------------


def _later_stock_blockers(records: List[Record], in_set: Set[Key]) -> List[Blocker]:
    """A Stock Reconciliation whose items and warehouses have stock movements after it that the
    undo set did not make: removing it would change the valuation of those movements."""
    names = [r.name for r in records if r.doctype == "Stock Reconciliation"]
    if not names:
        return []
    heads: Dict[str, frappe._dict] = {}
    pairs: Dict[str, Set[Tuple[str, str]]] = defaultdict(set)
    for chunk in _chunks(names):
        for row in frappe.get_all(
            "Stock Reconciliation",
            filters={"name": ["in", chunk], "docstatus": ["<", 2]},
            fields=["name", "posting_date", "posting_time"],
            limit_page_length=0,
        ):
            heads[row.name] = row
        for row in frappe.get_all(
            "Stock Reconciliation Item",
            filters={"parent": ["in", chunk], "parenttype": "Stock Reconciliation"},
            fields=["parent", "item_code", "warehouse"],
            limit_page_length=0,
        ):
            pairs[row.parent].add((row.item_code, row.warehouse))
    blockers = []
    for record in records:
        head, wanted = heads.get(record.name), pairs.get(record.name)
        if record.doctype != "Stock Reconciliation" or not head or not wanted:
            continue
        after = datetime.combine(getdate(head.posting_date), get_time(head.posting_time or "00:00:00"))
        for entry in frappe.get_all(
            "Stock Ledger Entry",
            filters={
                "item_code": ["in", sorted({p[0] for p in wanted})],
                "warehouse": ["in", sorted({p[1] for p in wanted})],
                "is_cancelled": 0,
                "posting_datetime": [">", after],
            },
            fields=["item_code", "warehouse", "voucher_type", "voucher_no", "posting_datetime"],
            order_by="posting_datetime asc, creation asc",
            limit_page_length=0,
        ):
            if (entry.item_code, entry.warehouse) not in wanted:
                continue
            if (entry.voucher_type, entry.voucher_no) in in_set:
                continue
            by = _label(entry.voucher_type, entry.voucher_no)
            detail = _("{0} moved {1} in {2} on {3}, after this reconciliation.").format(
                by, entry.item_code, entry.warehouse, entry.posting_datetime
            )
            blockers.append(Blocker(record, "later_stock", detail, by))
            break
    return blockers


# -- no permission -------------------------------------------------------------------------


def _permission_blockers(records: List[Record]) -> List[Blocker]:
    """The user can't delete the doctype, or can't cancel it when the record is submitted."""
    blockers = []
    for doctype, names in _by_doctype(records).items():
        can_delete = frappe.has_permission(doctype, "delete")
        submitted: Set[str] = set()
        if frappe.get_meta(doctype).is_submittable:
            for chunk in _chunks(names):
                submitted.update(
                    frappe.get_all(
                        doctype,
                        filters={"name": ["in", chunk], "docstatus": 1},
                        pluck="name",
                        limit_page_length=0,
                    )
                )
        can_cancel = frappe.has_permission(doctype, "cancel") if submitted else True
        for record in (r for r in records if r.doctype == doctype):
            if not can_delete:
                detail = _("You are not allowed to delete {0} records.").format(_(doctype))
            elif record.name in submitted and not can_cancel:
                detail = _("It is submitted, and you are not allowed to cancel {0} records.").format(
                    _(doctype)
                )
            else:
                continue
            blockers.append(Blocker(record, "no_permission", detail, None))
    return blockers


# -- cascade -------------------------------------------------------------------------------


def _depends_on(records: List[Record], in_set: Set[Key]) -> List[Tuple[Record, Key]]:
    """(record, set record it needs): what each record links to inside the set through its
    Link and Dynamic Link fields and its child rows', and its tree parent. One query per
    doctype and per child table."""
    set_doctypes = {k[0] for k in in_set}
    by_key = {_key(r): r for r in records}
    pairs: List[Tuple[Record, Key]] = []

    def fields_of(meta):
        links = [(df.fieldname, df.options) for df in meta.get_link_fields() if df.options in set_doctypes]
        dynamic = [(df.fieldname, df.options) for df in meta.get_dynamic_link_fields()]
        columns = {f for f, _t in links} | {f for pair in dynamic for f in pair}
        return links, dynamic, columns

    def collect(holder: Optional[Record], row, links, dynamic) -> None:
        if not holder:
            return
        for fieldname, target in links:
            if row.get(fieldname) and (target, row.get(fieldname)) in in_set:
                pairs.append((holder, (target, row.get(fieldname))))
        for fieldname, doctype_field in dynamic:
            key = (row.get(doctype_field), row.get(fieldname))
            if key[0] and key[1] and key in in_set:
                pairs.append((holder, key))

    for doctype, names in _by_doctype(records).items():
        meta = frappe.get_meta(doctype)
        links, dynamic, columns = fields_of(meta)
        if columns:
            for chunk in _chunks(names):
                for row in frappe.get_all(
                    doctype,
                    filters={"name": ["in", chunk]},
                    fields=["name", *sorted(columns)],
                    limit_page_length=0,
                ):
                    collect(by_key.get((doctype, row.name)), row, links, dynamic)
        for table_field in meta.get_table_fields():
            child_links, child_dynamic, child_columns = fields_of(frappe.get_meta(table_field.options))
            if not child_columns:
                continue
            for chunk in _chunks(names):
                for row in frappe.get_all(
                    table_field.options,
                    filters={
                        "parent": ["in", chunk],
                        "parenttype": doctype,
                        "parentfield": table_field.fieldname,
                    },
                    fields=["parent", *sorted(child_columns)],
                    limit_page_length=0,
                ):
                    collect(by_key.get((doctype, row.parent)), row, child_links, child_dynamic)

    for record in records:
        if record.parent and (record.doctype, record.parent) in in_set:
            pairs.append((record, (record.doctype, record.parent)))
    return [(holder, key) for holder, key in pairs if key != _key(holder)]


def _cascade(records: List[Record], blocked: List[Blocker]) -> List[Tuple[Record, str]]:
    """What the blocked records keep with them, until nothing more is kept: the set records
    they link to and their tree parents, then in turn what those kept records need."""
    in_set = {_key(r) for r in records}
    by_key = {_key(r): r for r in records}
    held = {_key(b.record) for b in blocked}
    kept: Dict[Key, str] = {}
    kept_for: Dict[Key, str] = {}  # kept record -> the blocked record it is kept for
    frontier = [b.record for b in blocked]
    while frontier:
        nxt = []
        for holder, key in _depends_on(frontier, in_set):
            if key in held:
                continue
            held.add(key)
            holder_label = _label(holder.doctype, holder.name)
            if _key(holder) in kept_for:
                kept_for[key] = kept_for[_key(holder)]
                kept[key] = _("Kept because {0} depends on it and is kept for {1}.").format(
                    holder_label, kept_for[key]
                )
            else:
                kept_for[key] = holder_label
                kept[key] = _("Kept because {0} depends on it and can't be removed.").format(holder_label)
            nxt.append(by_key[key])
        frontier = nxt
    return [(r, kept[_key(r)]) for r in records if _key(r) in kept]


# -- build ---------------------------------------------------------------------------------


def _fingerprint(records: List[Record], blocked: List[Blocker]) -> str:
    """sha1 of the sorted records and the sorted blocked names: the same for the same set,
    whatever order the session lists them in."""
    names = sorted(f"{r.doctype}|{r.name}" for r in records)
    blocked_names = sorted(f"{b.record.doctype}|{b.record.name}" for b in blocked)
    text = "\n".join(names) + "\n--\n" + "\n".join(blocked_names)
    return hashlib.sha1(text.encode()).hexdigest()


def build_plan(session) -> Plan:
    """The undo set; what blocks each record (one reason per record, the first that applies of
    linked, changed, closed_books, later_stock, no_permission); what the blocked records keep
    with them; and the fingerprint."""
    present, removed, note = undo_set(session)
    in_set = {_key(r) for r in present}
    ignore: Set[Key] = {("FAC Import Session", session.name)}
    ignore |= {("Data Import", row.data_import) for row in session.data_imports if row.data_import}

    blocked: List[Blocker] = []
    held: Set[Key] = set()

    def add(found: List[Blocker]) -> None:
        for blocker in found:
            if _key(blocker.record) not in held:
                held.add(_key(blocker.record))
                blocked.append(blocker)

    def still_open() -> List[Record]:
        return [r for r in present if _key(r) not in held]

    if present:
        add(link_blockers(present, ignore))
        add(_changed_blockers(still_open(), session))
        add(_closed_books_blockers(still_open()))
        add(_later_stock_blockers(still_open(), in_set))
        add(_permission_blockers(still_open()))

    order = {_key(r): i for i, r in enumerate(present)}
    blocked.sort(key=lambda b: order[_key(b.record)])
    return Plan(
        records=present,
        blocked=blocked,
        kept=_cascade(present, blocked),
        already_removed=removed,
        note=note,
        fingerprint=_fingerprint(present, blocked),
    )
