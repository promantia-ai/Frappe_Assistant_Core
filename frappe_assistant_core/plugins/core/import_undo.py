# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Undo an import (#34002): what a session created, whether it can be undone now, and the
background job that removes it.

The undo set is the session's Created Records plus the records its Insert New Records Data
Imports logged as created (each Data Import by its own import type). The plan says, for each of them, whether it can be
removed: what blocks it (spec §4), and what a blocked record keeps with it. start() hands the
plan's records to a background job, which removes them in delete order (spec §5), one savepoint
per record, and report() says how far it got.
"""

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Set, Tuple

import frappe
from frappe import _
from frappe.utils import cint, get_datetime, get_time, getdate
from pypika import functions as fn
from pypika.terms import Tuple as Row

INSERT = "Insert New Records"
UPDATE = "Update Existing Records"
UNDO_DAYS = 30  # default of the site config key fac_import_undo_days
UNDO_GRACE_MINUTES = 10  # ERPNext may submit large opening documents in the background, later
CHUNK = 1000  # names per IN (...) of one query
MAX_LISTED = 50  # entries of a list in a tool reply; more are summed as "(N more)"
MAX_NAMED = 10  # sample names under one entry of a tool reply
# Ledger and repost entries (ERPNext's ignore_linked_doctypes on cancel): made by a voucher and
# removed with it. A row whose voucher_type/voucher_no is in the undo set never blocks, whatever
# it links to (the voucher, its Customer, its Account); a row made by a voucher outside does.
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
# ERPNext deletes a party's Contacts and Addresses with it (delete_contact_and_address)
CONTACT_DOCTYPES = ["Contact", "Address"]

Key = Tuple[str, str]


@dataclass
class Record:
    doctype: str
    name: str
    parent: Optional[str] = None


class UndoProblem(Exception):
    """Something the user is told about, as it is worded."""


IMPORTED = ("Success", "Warning")  # Import step outcomes that imported records (Warning: some refused)
NOT_IMPORTING = ("Undo", "Dry Run")  # steps that never create a record


def finished_at(session) -> datetime:
    """When the session's last import step finished: its last Import step that imported
    (Success, or Warning when some rows were refused). A session with no such step (created
    masters only) uses its latest step of another kind than Undo and Dry Run, and one with no
    steps its creation time. Step timestamps are written once, so this does not move when the
    session is saved."""
    imports = [
        get_datetime(s.timestamp)
        for s in session.steps
        if s.step == "Import" and s.outcome in IMPORTED and s.timestamp
    ]
    if imports:
        return max(imports)
    stamps = [get_datetime(s.timestamp) for s in session.steps if s.timestamp and s.step not in NOT_IMPORTING]
    return max(stamps) if stamps else get_datetime(session.creation)


def _days() -> int:
    return int(frappe.conf.get("fac_import_undo_days", UNDO_DAYS))


def window_ends(session) -> datetime:
    return finished_at(session) + timedelta(days=_days())


def window(session) -> dict:
    """The undo window as a tool reports it: its length in days and the day it ends."""
    return {"days": _days(), "ends_on": str(window_ends(session).date())}


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

    # Each Data Import by its own import type, not the session's: a session that ran an update
    # can be reused by an opening import, which sets the session to Insert.
    note = None
    done: Set[str] = set()
    updates = inserts = 0
    for link in session.data_imports:
        if not link.data_import or link.data_import in done:
            continue
        done.add(link.data_import)
        di = frappe.db.get_value(
            "Data Import", link.data_import, ["reference_doctype", "import_type"], as_dict=True
        )
        if not di:
            continue
        if (di.import_type or INSERT) != INSERT:
            updates += 1  # its records existed before it: never deleted
            continue
        inserts += 1
        for docname in frappe.get_all(
            "Data Import Log",
            fields=["docname"],
            filters={"data_import": link.data_import, "success": 1},
            pluck="docname",
            order_by="creation asc",
            limit_page_length=0,
        ):
            add(di.reference_doctype, docname)
    if updates and not inserts:
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


def _groups(keys: Iterable[Key]) -> Dict[str, List[str]]:
    grouped: Dict[str, List[str]] = defaultdict(list)
    for doctype, name in sorted(set(keys)):
        grouped[doctype].append(name)
    return grouped


def _not_among(doctype_col, name_col, groups: Dict[str, List[str]]) -> list:
    """SQL conditions: a row's (doctype, name) is none of ``groups``; IN lists at most CHUNK
    long. A NULL doctype or name is no document, so the row stays."""
    conditions = []
    for doctype, names in groups.items():
        for chunk in _chunks(names):
            conditions.append(
                ~((fn.Coalesce(doctype_col, "") == doctype) & fn.Coalesce(name_col, "").isin(chunk))
            )
    return conditions


def _contacts_inside(in_set: Set[Key], session) -> Set[Key]:
    """Contacts and Addresses made with the import, whose every Dynamic Link row points to a
    record in the set: ERPNext deletes them with the party, so they don't block. Made with the
    import: by the session's user, at most UNDO_GRACE_MINUTES after it finished. One added by
    someone else, or later, is not the import's and blocks as linked, or the undo would delete
    it silently. Batched: one query per set doctype for the candidates, one per Contact/Address
    doctype for all their rows and one for their owners."""
    if session is None or not frappe.db.exists("DocType", "Dynamic Link"):
        return set()
    user = session.user or session.owner
    latest = finished_at(session) + timedelta(minutes=UNDO_GRACE_MINUTES)
    link = frappe.qb.DocType("Dynamic Link")
    candidates: Set[Key] = set()
    for doctype, names in _groups(in_set).items():
        for chunk in _chunks(names):
            rows = (
                frappe.qb.from_(link)
                .select(link.parenttype, link.parent)
                .distinct()
                .where(link.parenttype.isin(CONTACT_DOCTYPES))
                .where(link.link_doctype == doctype)
                .where(link.link_name.isin(chunk))
                .run(as_dict=True)
            )
            candidates.update((r.parenttype, r.parent) for r in rows)
    outside: Set[Key] = set()
    for parenttype, parents in _groups(candidates).items():
        for chunk in _chunks(parents):
            rows = (
                frappe.qb.from_(link)
                .select(link.parent, link.link_doctype, link.link_name)
                .where(link.parenttype == parenttype)
                .where(link.parent.isin(chunk))
                .run(as_dict=True)
            )
            outside.update(
                (parenttype, r.parent) for r in rows if (r.link_doctype, r.link_name) not in in_set
            )
            for row in frappe.get_all(
                parenttype,
                filters={"name": ["in", chunk]},
                fields=["name", "owner", "creation"],
                limit_page_length=0,
            ):
                if row.owner != user or get_datetime(row.creation) > latest:
                    outside.add((parenttype, row.name))
    return candidates - outside


def link_blockers(records: List[Record], ignore: Set[Key], session=None) -> List[Blocker]:
    """Documents outside ``records`` that link to them, by the rules of Frappe's own delete
    check (frappe.model.delete_doc.get_linked_docs and get_dynamic_linked_docs): Link fields,
    Dynamic Link fields and child-table rows (counted as their parent document); cancelled
    documents and the doctypes in the ``ignore_links_on_delete`` hook never block. Neither do
    ledger entries (OWN_ENTRIES) made by a voucher in the set, Contacts and Addresses that
    ``session`` made with the import and linked only to records in the set (_contacts_inside;
    without a session, every Contact and Address counts), nor the documents in ``ignore`` (the
    session itself).

    One aggregated query per linking field (and per CHUNK names): it returns at most a row per
    target (and linking parent doctype) with the first linker and a count, however many
    documents link to it. One blocker per record, naming the first linker. The count is summed
    over the linking fields, so a document linking through two fields counts twice."""
    from frappe.model.dynamic_links import get_dynamic_link_map
    from frappe.model.rename_doc import get_link_fields

    in_set = {_key(r) for r in records}
    skip = set(frappe.get_hooks("ignore_links_on_delete") or [])
    never = _groups(in_set | set(ignore) | _contacts_inside(in_set, session))
    vouchers = _groups(in_set)
    first: Dict[Key, Key] = {}
    count: Dict[Key, int] = defaultdict(int)

    def note(target: Key, linker: Key, n: int) -> None:
        first.setdefault(target, linker)
        count[target] += n

    def scan(link_dt, fieldname, doctype, names, doctype_field=None) -> None:
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
                and (link_dt, link_dt) not in ignore
            ):
                note((doctype, values.get(fieldname)), (link_dt, link_dt), 1)
            return
        table = frappe.qb.DocType(link_dt)
        target = table[fieldname]
        if meta.istable:
            linker_dt, linker = table.parenttype, table.parent
            conditions = _not_among(linker_dt, linker, never) + [linker.isnotnull()]
            if skip:
                conditions.append(fn.Coalesce(linker_dt, "").notin(sorted(skip)))
        else:
            linker_dt, linker = None, table.name
            conditions = [linker.notin(chunk) for chunk in _chunks(never.get(link_dt, []))]
        conditions.append(fn.Coalesce(table.docstatus, 0) != 2)
        if doctype_field:
            conditions.append(table[doctype_field] == doctype)
        if link_dt == "Submission Queue":
            conditions.append(table.status == "Queued")
        if link_dt in OWN_ENTRIES and meta.has_field("voucher_type") and meta.has_field("voucher_no"):
            conditions += _not_among(table.voucher_type, table.voucher_no, vouchers)
        columns = [target.as_("target"), fn.Count(linker).distinct().as_("n"), fn.Min(linker).as_("first")]
        group = [target]
        if linker_dt is not None:
            columns.append(linker_dt.as_("linker_doctype"))
            group.append(linker_dt)
        for chunk in _chunks(names):
            query = frappe.qb.from_(table).select(*columns).where(target.isin(chunk))
            for condition in conditions:
                query = query.where(condition)
            for row in query.groupby(*group).run(as_dict=True):
                note((doctype, row.target), (row.get("linker_doctype") or link_dt, row.first), cint(row.n))

    dynamic_map = get_dynamic_link_map()
    for doctype, names in _by_doctype(records).items():
        seen: Set[Tuple[str, str]] = set()
        for lf in get_link_fields(doctype):
            if lf["parent"] not in skip and (lf["parent"], lf["fieldname"]) not in seen:
                seen.add((lf["parent"], lf["fieldname"]))
                scan(lf["parent"], lf["fieldname"], doctype, names)
        for df in dynamic_map.get(doctype, []):
            if df.parent not in skip and (df.parent, df.fieldname) not in seen:
                seen.add((df.parent, df.fieldname))
                scan(df.parent, df.fieldname, doctype, names, doctype_field=df.options)

    blockers = []
    for record in records:
        linker = first.get(_key(record))
        if not linker:
            continue
        by = _label(*linker)
        more = count[_key(record)] - 1
        if more > 0:
            detail = _("{0} links to it ({1} more).").format(by, more)
        else:
            detail = _("{0} links to it.").format(by)
        blockers.append(Blocker(record, "linked", detail, by))
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
    """A submitted Stock Reconciliation whose items and warehouses have stock movements after it
    that the undo set did not make: removing it would change the valuation of those movements.
    A draft posted nothing. Per reconciliation, one query per CHUNK item/warehouse pairs, the
    set's own vouchers left out in SQL, the earliest movement only."""
    names = [r.name for r in records if r.doctype == "Stock Reconciliation"]
    if not names:
        return []
    heads: Dict[str, frappe._dict] = {}
    pairs: Dict[str, Set[Tuple[str, str]]] = defaultdict(set)
    for chunk in _chunks(names):
        for row in frappe.get_all(
            "Stock Reconciliation",
            filters={"name": ["in", chunk], "docstatus": 1},
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
    sle = frappe.qb.DocType("Stock Ledger Entry")
    own = _not_among(sle.voucher_type, sle.voucher_no, _groups(in_set))
    blockers = []
    for record in records:
        head, wanted = heads.get(record.name), pairs.get(record.name)
        if record.doctype != "Stock Reconciliation" or not head or not wanted:
            continue
        after = datetime.combine(getdate(head.posting_date), get_time(head.posting_time or "00:00:00"))
        earliest = None
        for chunk in _chunks(sorted(wanted)):
            query = (
                frappe.qb.from_(sle)
                .select(sle.item_code, sle.warehouse, sle.voucher_type, sle.voucher_no, sle.posting_datetime)
                .where(Row(sle.item_code, sle.warehouse).isin([Row(*pair) for pair in chunk]))
                .where(sle.is_cancelled == 0)
                .where(sle.posting_datetime > after)
            )
            for condition in own:
                query = query.where(condition)
            rows = query.orderby(sle.posting_datetime).orderby(sle.creation).limit(1).run(as_dict=True)
            if rows and (earliest is None or rows[0].posting_datetime < earliest.posting_datetime):
                earliest = rows[0]
        if earliest:
            by = _label(earliest.voucher_type, earliest.voucher_no)
            detail = _("{0} moved {1} in {2} on {3}, after this reconciliation.").format(
                by, earliest.item_code, earliest.warehouse, earliest.posting_datetime
            )
            blockers.append(Blocker(record, "later_stock", detail, by))
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
        add(link_blockers(present, ignore, session))
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


# -- delete order --------------------------------------------------------------------------


def _links_between(doctypes: List[str]) -> Dict[str, Set[str]]:
    """For each doctype of the set, the other set doctypes it links to: through the Link fields
    of the doctype and of its child tables, and through the Dynamic Link fields that Frappe's
    delete check counts as pointing to a set doctype (get_dynamic_link_map)."""
    from frappe.model.dynamic_links import get_dynamic_link_map

    wanted = set(doctypes)
    links: Dict[str, Set[str]] = {doctype: set() for doctype in doctypes}
    holders: Dict[str, Set[str]] = defaultdict(set)  # doctype or child table -> set doctypes
    for doctype in doctypes:
        meta = frappe.get_meta(doctype)
        holders[doctype].add(doctype)
        metas = [meta]
        for table_field in meta.get_table_fields():
            holders[table_field.options].add(doctype)
            metas.append(frappe.get_meta(table_field.options))
        for part in metas:
            links[doctype].update(df.options for df in part.get_link_fields() if df.options in wanted)
    dynamic = get_dynamic_link_map()
    for target in doctypes:
        for df in dynamic.get(target, []):
            for holder in holders.get(df.parent, ()):
                links[holder].add(target)
    for doctype in doctypes:
        links[doctype].discard(doctype)
    return links


def _doctype_order(doctypes: List[str]) -> List[str]:
    """A doctype that links to another goes first. Each pick is the first by name among the
    doctypes nothing left links to; inside a cycle, the first by name among the cycle's
    doctypes (a cycle nothing outside it links to)."""
    links = _links_between(doctypes)
    left = set(doctypes)
    order: List[str] = []

    def reach(start: str) -> Set[str]:
        seen: Set[str] = set()
        stack = [start]
        while stack:
            for nxt in links[stack.pop()] & left:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    while left:
        reached = {doctype: reach(doctype) for doctype in left}
        first = [
            doctype
            for doctype in left
            if all(other in reached[doctype] for other in left if doctype in reached[other])
        ]
        pick = min(first or left)
        order.append(pick)
        left.remove(pick)
    return order


def _tree_order(records: List[Record]) -> List[Record]:
    """Records of one doctype: deepest in the set's own tree first (a child before its parent,
    by the session's parent_record), then reverse creation."""
    by_name = {r.name: r for r in records}
    position = {r.name: i for i, r in enumerate(records)}

    def depth(record: Record) -> int:
        seen, parent, level = {record.name}, record.parent, 0
        while parent in by_name and parent not in seen:
            seen.add(parent)
            level += 1
            parent = by_name[parent].parent
        return level

    return sorted(records, key=lambda r: (-depth(r), -position[r.name]))


def delete_order(records: List[Record]) -> List[Record]:
    """The order to remove ``records`` in (spec §5 steps 1-2): by doctype, a doctype that links
    to another first (invoices, then parties, then masters); inside one doctype, tree children
    before their parents, then reverse creation. ``records`` is in creation order."""
    by_doctype: Dict[str, List[Record]] = defaultdict(list)
    for record in records:
        by_doctype[record.doctype].append(record)
    return [r for doctype in _doctype_order(list(by_doctype)) for r in _tree_order(by_doctype[doctype])]


# -- the job -------------------------------------------------------------------------------

SESSION_DOCTYPE = "FAC Import Session"
JOB = "fac_import_undo::{}"
MODES = ("all", "unblocked")
BATCH = 25  # records between progress writes (and commits, outside tests)
STEP = "Undo"
UNDOING, UNDONE, PARTIALLY_UNDONE = "Undoing", "Undone", "Partially Undone"
RESTORED_DEFAULT = "Completed"  # when no previous status was kept
STALL_SECONDS = (
    600  # a queued job is in the queue only after its commit: silent this long, with no job, it has stopped
)
RECORD_SAVEPOINT = "fac_import_undo_record"
JOB_SAVEPOINT = "fac_import_undo_job"


def _undo_options(session) -> dict:
    return dict((frappe.parse_json(session.template_options or "{}") or {}).get("undo") or {})


def _store_undo(session, undo: dict) -> None:
    options = frappe.parse_json(session.template_options or "{}") or {}
    options["undo"] = undo
    session.template_options = frappe.as_json(options)


def remember_fingerprint(session, fingerprint: str) -> None:
    """Keep the plan the user was shown, so undo_import can tell the records changed since. Saved
    on its own: the status, the steps and the modified time of the session stay as they are."""
    undo = _undo_options(session)
    undo["fingerprint"] = fingerprint
    _store_undo(session, undo)
    session.db_set("template_options", session.template_options, update_modified=False)


def stored_fingerprint(session) -> Optional[str]:
    return _undo_options(session).get("fingerprint")


def has_result(session) -> bool:
    """Whether an undo of this session ran to its end and left its result."""
    return bool(_undo_options(session).get("result"))


def plan_refusal(plan: Plan) -> str:
    """Why this plan has nothing to remove, or "" when it has. A set whose records are all
    removed already has something to do: the session is set as undone."""
    if not plan.records and not plan.already_removed:
        return plan.note or _("This session has no records to remove.")
    if plan.records and not plan.to_delete:
        return _("Nothing can be removed: every record is blocked or kept for one that is.")
    return ""


def _progress(state: str, done: int = 0, total: int = 0, deleted: int = 0) -> str:
    return frappe.as_json(
        {
            "step": STEP,
            "state": state,
            "done": done,
            "total": total,
            "deleted": deleted,
            "updated_on": str(frappe.utils.now_datetime()),
        }
    )


def _commits() -> bool:
    """Whether the job commits its batches (not under tests, which roll back at the end)."""
    return not frappe.flags.in_test


def _job_running(session) -> bool:
    from frappe.utils.background_jobs import is_job_enqueued

    try:
        return bool(is_job_enqueued(JOB.format(session.name)))
    except Exception:
        return True  # the queue can't be asked (Redis down, say): don't call the job stopped


def _stalled(session) -> bool:
    """Whether a session Undoing has no job behind it and has been silent for a while. A job
    queued moments ago is not in the queue until its commit, so the grace applies from the
    last progress write (start() writes one)."""
    if _job_running(session):
        return False
    progress = frappe.parse_json(session.progress or "{}") or {}
    updated = progress.get("updated_on")
    return not updated or (
        frappe.utils.time_diff_in_seconds(frappe.utils.now_datetime(), updated) > STALL_SECONDS
    )


def start(session, mode: str) -> str:
    """Hand the undo to a background job, as the current user: "started", or "running" when its
    job is already queued (nothing is queued again). The caller has checked the plan; the job
    builds it again. A session left Undoing by a job that died is set back first."""
    if mode not in MODES:
        raise UndoProblem(_("The mode is all or unblocked, not {0}.").format(mode))
    if session.status == UNDOING:
        if not _stalled(session):
            return "running"
        _stopped(session.name, _("the background job stopped."))
        session.reload()
    reason = refusal(session)
    if reason:
        raise UndoProblem(reason)
    undo = _undo_options(session)
    undo.pop("result", None)
    undo.pop("stopped", None)
    undo.update(previous_status=session.status, mode=mode)
    _store_undo(session, undo)
    session.status = UNDOING
    session.progress = _progress("queued")
    session.save()
    frappe.enqueue(
        f"{__name__}.undo_job",
        queue="long",
        timeout=4 * 60 * 60,
        job_id=JOB.format(session.name),
        deduplicate=True,
        enqueue_after_commit=True,
        now=bool(frappe.flags.in_test),
        session_name=session.name,
        user=frappe.session.user,
        mode=mode,
    )
    return "started"


def undo_job(session_name: str, user: str, mode: str) -> None:
    """Background job: the undo, as the user who approved it. A record that can't be removed
    is kept (delete_failed) with what it needs; anything else that goes wrong rolls back and
    sets the session back to its status before the undo."""
    from frappe_assistant_core.plugins.core import import_dry_run as dry_run

    frappe.set_user(user)  # nosemgrep: frappe-setuser — the job runs as the user who approved the undo
    session = frappe.get_doc(SESSION_DOCTYPE, session_name)
    if session.status != UNDOING:
        return  # set back meanwhile (report found no job), or already finished
    frappe.db.savepoint(JOB_SAVEPOINT)
    try:
        _run(session, mode)
    except Exception as e:
        message = dry_run._message_of(e)
        if _commits():
            frappe.db.rollback()
        else:
            frappe.db.rollback(save_point=JOB_SAVEPOINT)
        _stopped(session_name, _("{0}.").format(message.rstrip(".")))
        frappe.log_error(title=_("FAC import undo failed"), message=frappe.get_traceback())
    if _commits():
        frappe.db.commit()  # nosemgrep: frappe-manual-commit — a background job commits its own work


def _run(session, mode: str) -> None:
    plan = build_plan(session)
    blocked: List[Blocker] = list(plan.blocked)
    kept: List[Tuple[Record, str]] = list(plan.kept)
    if mode == "all" and blocked:
        _store_result(session, 0, kept, blocked)
        _restore(
            session,
            _(
                "Nothing was removed: {0} records can't be removed. Undo with mode unblocked to remove "
                "the rest and keep those."
            ).format(len(blocked)),
        )
        return

    records = delete_order(plan.to_delete)
    total, deleted = len(records), 0
    held: Set[Key] = set()  # kept because a record that failed needs them
    session.progress = _progress("running", 0, total)
    session.save()
    for done, record in enumerate(records, start=1):
        if _key(record) not in held:
            error = _remove(session, record)
            if error is None:
                deleted += 1
            else:
                failed = Blocker(record, "delete_failed", error)
                blocked.append(failed)
                pending = [r for r in records[done:] if _key(r) not in held]
                for needed, because in _cascade([record, *pending], [failed]):
                    held.add(_key(needed))
                    kept.append((needed, because))
        if done % BATCH == 0 and done < total:
            session.progress = _progress("running", done, total, deleted)
            session.save()
            if _commits():
                frappe.db.commit()  # nosemgrep: frappe-manual-commit — a background job commits its own work

    session.reload()
    _store_result(session, deleted, kept, blocked)
    session.status = PARTIALLY_UNDONE if blocked or kept else UNDONE
    session.progress = _progress("done", total, total, deleted)
    if blocked or kept:
        message = _(
            "Removed {0} records; kept {1}: {2} can't be removed, and {3} are kept with them."
        ).format(deleted, len(blocked) + len(kept), len(blocked), len(kept))
        session.log_step(STEP, "Warning", message, rows_ok=deleted, rows_failed=len(blocked) + len(kept))
    else:
        session.log_step(STEP, "Success", _("Removed {0} records.").format(deleted), rows_ok=deleted)
    session.save()


def _store_result(session, deleted: int, kept: List[Tuple[Record, str]], blocked: List[Blocker]) -> None:
    undo = _undo_options(session)
    undo["result"] = {
        "deleted": deleted,
        "kept": len(kept),
        "blocked": [
            {"record": _label(b.record.doctype, b.record.name), "reason": b.reason, "detail": b.detail}
            for b in blocked
        ],
    }
    _store_undo(session, undo)


def _cancel(doc) -> None:
    """Cancel now, in this job: Document._cancel, not cancel(), which ERPNext overrides to queue
    large documents (Journal Entry, Stock Reconciliation over 100 rows) in another background
    job, while the delete follows at once. Frappe still checks the user's cancel permission."""
    doc._cancel()


# The ledger tables whose rows ERPNext's AccountsController.on_trash deletes by voucher_type and
# voucher_no when Accounts Settings' delete_linked_ledger_entries is on.
VOUCHER_LEDGERS = ("Payment Ledger Entry", "GL Entry", "Stock Ledger Entry", "Advance Payment Ledger Entry")


def _remove_own_ledger_rows(doctype: str, name: str) -> None:
    """Delete the ledger rows of one voucher that this undo has just cancelled, so it can be
    deleted.

    Cancelling marks a voucher's GL, Payment Ledger and Stock Ledger rows cancelled (or posts
    reversing rows) but keeps them, submitted, linked to it through voucher_no; Frappe's delete
    check then refuses to delete the cancelled voucher ("is linked with GL Entry ..."), unless
    Accounts Settings' delete_linked_ledger_entries is on, in which case ERPNext's on_trash
    deletes them first. This does the part of that routine that concerns the voucher's own rows,
    for this voucher only, without changing the setting: rows whose voucher_type and voucher_no
    are this voucher, in the tables ERPNext clears (VOUCHER_LEDGERS). It leaves out what that
    routine also does to other vouchers (cancelling exchange gain/loss Journal Entries, rows of
    other vouchers against this one): a row of another voucher still refuses the delete, and the
    record is kept. Only for a voucher this job cancelled: the caller has just cancelled it, and
    it must be cancelled now. Rolled back with the record's savepoint if the delete fails."""
    if frappe.db.get_value(doctype, name, "docstatus") != 2:
        raise UndoProblem(_("{0} {1} is not cancelled, so its ledger is kept.").format(_(doctype), name))
    for ledger in VOUCHER_LEDGERS:
        if not frappe.db.exists("DocType", ledger):
            continue
        table = frappe.qb.DocType(ledger)
        frappe.qb.from_(table).delete().where(
            (table.voucher_type == doctype) & (table.voucher_no == name)
        ).run()


def _remove(session, record: Record) -> Optional[str]:
    """Remove one record in a savepoint (spec §5 step 3): its Created Records row first (Frappe
    counts that Dynamic Link and would refuse the delete), cancel it when submitted, then
    delete it. Returns None, or what refused it: then all of it is rolled back."""
    since = len(frappe.local.message_log or [])
    frappe.db.savepoint(RECORD_SAVEPOINT)
    try:
        session.reload()
        rows = [
            row
            for row in session.created_records
            if not (row.record_doctype == record.doctype and row.record_name == record.name)
        ]
        if len(rows) != len(session.created_records):
            session.created_records = rows
            session.save()
        doc = frappe.get_doc(record.doctype, record.name)
        if doc.docstatus == 1:
            _cancel(doc)
            _remove_own_ledger_rows(record.doctype, record.name)
        frappe.delete_doc(record.doctype, record.name)
    except Exception as e:
        error = _error_of(e, since)
        frappe.db.rollback(save_point=RECORD_SAVEPOINT)
        frappe.clear_document_cache(record.doctype, record.name)
        session.reload()
        return error
    return None


def _error_of(e: Exception, since: int) -> str:
    """What refused a record: the messages shown since ``since``, which are then dropped, so
    one record's failure isn't shown with the next."""
    from frappe_assistant_core.plugins.core import import_dry_run as dry_run

    log = list(frappe.local.message_log or [])
    frappe.local.message_log = log[since:]
    try:
        return dry_run._message_of(e)
    finally:
        frappe.local.message_log = log[:since]


def _restore(session, message: str) -> None:
    """Set the session back to its status before the undo, with a Failed step saying why."""
    undo = _undo_options(session)
    undo["stopped"] = message
    _store_undo(session, undo)
    session.status = undo.get("previous_status") or RESTORED_DEFAULT
    progress = frappe.parse_json(session.progress or "{}") or {}
    session.progress = _progress(
        "stopped", cint(progress.get("done")), cint(progress.get("total")), cint(progress.get("deleted"))
    )
    session.log_step(STEP, "Failed", message)
    session.save()


def _stopped(session_name: str, reason: str) -> None:
    """An undo that stopped before its end: what it committed stays removed, and undo can be run
    again for the rest."""
    session = frappe.get_doc(SESSION_DOCTYPE, session_name)
    if session.status != UNDOING:
        return
    _restore(
        session,
        _("The undo stopped: {0} What it removed stays removed; undo again for the rest.").format(reason),
    )


def report(session) -> dict:
    """How far the undo got: state running, done or stopped, with the counts. A session Undoing
    with no job in the queue and no progress for STALL_SECONDS is stopped, and set back to its
    status before the undo."""
    if session.status == UNDOING and _stalled(session):
        _stopped(session.name, _("the background job stopped."))
        session.reload()
    undo = _undo_options(session)
    if session.status == UNDOING:
        state = "running"
    elif undo.get("stopped"):
        state = "stopped"  # also for a session set back to Partially Undone by a run that died
    elif session.status in (UNDONE, PARTIALLY_UNDONE):
        state = "done"
    else:
        state = "stopped"
    result = undo.get("result") or {}
    progress = frappe.parse_json(session.progress or "{}") or {}
    if progress.get("step") != STEP:
        progress = {}
    return {
        "state": state,
        "deleted": cint(result.get("deleted", progress.get("deleted"))),
        "total": cint(progress.get("total")),
        "kept": cint(result.get("kept")),
        "blocked": list(result.get("blocked") or []),
        "reason": undo.get("stopped") if state == "stopped" else None,
    }
