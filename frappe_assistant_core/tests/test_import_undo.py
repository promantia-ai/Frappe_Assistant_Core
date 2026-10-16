# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Undo an import (#34002): the undo set (what a session created) and when a session can be
undone at all. Sessions are built the way the import's own tests build them: masters through
create_document with import_session, Customers through a Data Import.
"""

import unittest
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.utils import add_days, add_to_date, getdate, now_datetime, nowdate

from frappe_assistant_core.plugins.core import import_undo
from frappe_assistant_core.tests.test_import_run import MAPPING, ImportRunCase


@unittest.skipUnless("erpnext" in frappe.get_installed_apps(), "needs ERPNext's Customer")
class UndoCase(ImportRunCase):
    """A session that created Customer Groups, and one that imported Customers."""

    def _blank_session(self):
        file_doc = self._file("blank.csv", b"Customer Name\nx\n")
        return self._run("start_import_session", {"file_url": file_doc.file_url})["result"]["session_id"]

    def _session_with_groups(self, count=2):
        """A session whose create_document made ``count`` Customer Groups (the missing groups
        of a Customer file, as the dry run plans them), each listed in Created Records."""
        names = [f"Undo {self.tag} {i}" for i in range(count)]
        lines = [",".join(MAPPING)] + [f"Cust {self.tag} {i},{name},Company" for i, name in enumerate(names)]
        file_doc = self._file("customers.csv", ("\n".join(lines) + "\n").encode())
        session = self._run("start_import_session", {"file_url": file_doc.file_url})["result"]["session_id"]
        self._run("set_column_mapping", {"session": session, "mapping": MAPPING, "doctype": "Customer"})
        checked = self._run("dry_run_import", {"session": session})["result"]
        documents = [{"doctype": m["doctype"], "data": m["data"]} for m in checked["masters_plan"]]
        created = self._run("create_document", {"documents": documents, "import_session": session})
        self.assertTrue(created["success"], created)
        return session, names

    def _session_with_customers(self):
        session, checked = self._ready_session()
        self.assertTrue(self._import(session, checked)["success"])
        return session

    def _set_step_time(self, session, when):
        doc = self._doc(session)
        for step in doc.steps:
            step.db_set("timestamp", when, update_modified=False)


class TestUndoSet(UndoCase):
    def test_created_records_are_listed_with_their_parent(self):
        session, names = self._session_with_groups()

        present, removed, note = import_undo.undo_set(self._doc(session))

        self.assertEqual(
            {(r.doctype, r.name, r.parent) for r in present},
            {("Customer Group", name, "All Customer Groups") for name in names},
        )
        self.assertEqual((removed, note), ([], None))

    def test_data_import_records_are_listed_from_the_log(self):
        session = self._session_with_customers()

        present, removed, note = import_undo.undo_set(self._doc(session))

        n = self._names()
        self.assertEqual({r.name for r in present}, {n["alpha"], n["gamma"], n["delta"]})
        self.assertEqual({r.doctype for r in present}, {"Customer"})
        self.assertEqual((removed, note), ([], None))

    def test_a_data_import_run_twice_counts_each_record_once(self):
        session = self._session_with_customers()
        doc = self._doc(session)
        first = doc.data_imports[-1].data_import
        doc.append("data_imports", {"data_import": first, "purpose": "Continue", "status": "Success"})
        doc.save()

        present, _removed, _note = import_undo.undo_set(self._doc(session))

        self.assertEqual(len(present), 3)
        self.assertEqual(len({(r.doctype, r.name) for r in present}), 3)

    def test_a_record_deleted_by_hand_is_already_removed(self):
        session = self._session_with_customers()
        gone = self._names()["alpha"]
        frappe.db.delete("Customer", {"name": gone})
        made, names = self._session_with_groups(2)
        frappe.db.delete("Customer Group", {"name": names[0]})

        present, removed, _note = import_undo.undo_set(self._doc(session))
        made_present, made_removed, _note = import_undo.undo_set(self._doc(made))

        self.assertEqual([r.name for r in removed], [gone])
        self.assertNotIn(gone, {r.name for r in present})
        self.assertEqual(len(present), 2)
        self.assertEqual([r.name for r in made_removed], [names[0]])
        self.assertEqual([r.name for r in made_present], [names[1]])

    def test_an_update_session_has_no_records_and_a_note(self):
        session = self._session_with_customers()
        self._doc(session).db_set("import_type", "Update Existing Records")

        present, removed, note = import_undo.undo_set(self._doc(session))

        self.assertEqual((present, removed), ([], []))
        self.assertIn("Update Existing Records", note)
        self.assertIn("never deleted", note)


class TestRefusal(UndoCase):
    def test_importing_session_is_refused(self):
        session, _names = self._session_with_groups()
        self._doc(session).db_set("status", "Importing")

        self.assertIn("importing", import_undo.refusal(self._doc(session)))

    def test_undoing_and_undone_sessions_are_refused(self):
        session, _names = self._session_with_groups()
        reasons = {}
        for status in ("Undoing", "Undone"):
            self._doc(session).db_set("status", status)
            reasons[status] = import_undo.refusal(self._doc(session))

        self.assertIn("being undone", reasons["Undoing"])
        self.assertIn("already undone", reasons["Undone"])

    def test_session_with_no_import_is_refused(self):
        session = self._blank_session()

        self.assertIn("nothing to undo", import_undo.refusal(self._doc(session)))

    def test_session_past_the_window_is_refused(self):
        session = self._session_with_customers()
        self._set_step_time(session, add_to_date(now_datetime(), hours=-1))

        with patch.dict(frappe.conf, {"fac_import_undo_days": 0}):
            reason = import_undo.refusal(self._doc(session))

        self.assertIn("0 days", reason)

    def test_session_inside_the_window_is_allowed(self):
        session = self._session_with_customers()
        self._set_step_time(session, add_to_date(now_datetime(), days=-3))

        with patch.dict(frappe.conf, {"fac_import_undo_days": 30}):
            doc = self._doc(session)
            self.assertEqual(import_undo.refusal(doc), "")
            last_import = [s for s in doc.steps if s.step == "Import"][-1]
            self.assertEqual(import_undo.finished_at(doc), last_import.timestamp)
            self.assertEqual(import_undo.window_ends(doc), import_undo.finished_at(doc) + timedelta(days=30))

    def test_a_masters_only_session_is_measured_from_its_last_step_and_does_not_move_on_save(self):
        session, _names = self._session_with_groups()
        doc = self._doc(session)
        self.assertFalse([s for s in doc.steps if s.step == "Import"])
        expected = max(s.timestamp for s in doc.steps)
        self.assertEqual(import_undo.finished_at(doc), expected)

        frappe.db.set_value("FAC Import Session", session, "modified", add_to_date(now_datetime(), days=2))
        doc = self._doc(session)
        doc.save()

        self.assertEqual(import_undo.finished_at(self._doc(session)), expected)

    def test_a_session_with_no_steps_is_measured_from_its_creation(self):
        session = self._blank_session()
        doc = self._doc(session)
        doc.steps = []

        self.assertEqual(import_undo.finished_at(doc), doc.creation)


class BlockerCase(UndoCase):
    """Sessions with extra records listed on them, and documents outside the set that use them.
    Rows written straight to the database (a submitted Journal Entry, a Stock Reconciliation and
    its ledger) stand in for documents too costly to build; they are deleted in tearDown."""

    def setUp(self):
        super().setUp()
        self.company = frappe.get_all("Company", pluck="name", order_by="creation", limit=1)[0]
        self.raw = []

    def tearDown(self):
        for doctype, name in reversed(self.raw):
            frappe.db.delete(doctype, {"name": name})
        super().tearDown()

    def _add(self, session, doctype, name, parent=None):
        doc = self._doc(session)
        doc.append(
            "created_records", {"record_doctype": doctype, "record_name": name, "parent_record": parent}
        )
        doc.save()

    def _insert_raw(self, values):
        doc = frappe.get_doc(values)
        doc.db_insert()
        self.raw.append((doc.doctype, doc.name))
        return doc

    def _plan(self, session):
        return import_undo.build_plan(self._doc(session))

    def _blocked(self, plan):
        return {(b.record.doctype, b.record.name): b for b in plan.blocked}

    def _kept(self, plan):
        return {(r.doctype, r.name): because for r, because in plan.kept}

    def _outside_customer(self, group):
        return (
            frappe.get_doc(
                {"doctype": "Customer", "customer_name": f"Outside {self.tag}", "customer_group": group}
            )
            .insert()
            .name
        )

    def _edited_by_someone_else(self, doctype, name):
        frappe.db.set_value(doctype, name, "modified_by", "Guest", update_modified=False)

    def _journal_entry(self, session, posting_date):
        entry = self._insert_raw(
            {
                "doctype": "Journal Entry",
                "name": f"UNDO-JE-{self.tag}",
                "company": self.company,
                "posting_date": posting_date,
                "voucher_type": "Journal Entry",
                "docstatus": 1,
            }
        )
        self._add(session, "Journal Entry", entry.name)
        return entry.name

    @contextmanager
    def _books_open(self):
        with patch.object(import_undo, "_frozen_till", return_value=None), patch.object(
            import_undo, "_closed_till", return_value={}
        ):
            yield

    def _reconciliation(self, session, docstatus=1):
        """A Stock Reconciliation three days ago, of one item in one warehouse, on the session."""
        self.stock_item, self.stock_warehouse = f"UNDO-IT-{self.tag}", f"UNDO-WH-{self.tag}"
        recon = self._insert_raw(
            {
                "doctype": "Stock Reconciliation",
                "name": f"UNDO-SR-{self.tag}",
                "company": self.company,
                "purpose": "Opening Stock",
                "posting_date": add_days(nowdate(), -3),
                "posting_time": "10:00:00",
                "docstatus": docstatus,
            }
        ).name
        self._insert_raw(
            {
                "doctype": "Stock Reconciliation Item",
                "name": f"UNDO-SRI-{self.tag}",
                "parent": recon,
                "parenttype": "Stock Reconciliation",
                "parentfield": "items",
                "idx": 1,
                "item_code": self.stock_item,
                "warehouse": self.stock_warehouse,
                "docstatus": docstatus,
            }
        )
        self._add(session, "Stock Reconciliation", recon)
        return recon

    def _ledger(self, suffix, voucher_type, voucher_no, days_ago):
        posting = add_days(nowdate(), -days_ago)
        self._insert_raw(
            {
                "doctype": "Stock Ledger Entry",
                "name": f"UNDO-SLE-{suffix}-{self.tag}",
                "item_code": self.stock_item,
                "warehouse": self.stock_warehouse,
                "company": self.company,
                "posting_date": posting,
                "posting_time": "12:00:00",
                "posting_datetime": f"{posting} 12:00:00",
                "voucher_type": voucher_type,
                "voucher_no": voucher_no,
                "actual_qty": 1,
                "is_cancelled": 0,
                "docstatus": 1,
            }
        )

    def _gl_entry(self, suffix, party, voucher_type, voucher_no):
        self._insert_raw(
            {
                "doctype": "GL Entry",
                "name": f"UNDO-GLE-{suffix}-{self.tag}",
                "company": self.company,
                "posting_date": nowdate(),
                "party_type": "Customer",
                "party": party,
                "voucher_type": voucher_type,
                "voucher_no": voucher_no,
                "is_cancelled": 0,
                "docstatus": 1,
            }
        )
        # raw rows don't refresh Frappe's cached list of doctypes a Dynamic Link points to
        frappe.cache.delete_value("dynamic_link_map::GL Entry::party_type")
        return f"UNDO-GLE-{suffix}-{self.tag}"

    def _contact(self, *parties):
        return (
            frappe.get_doc(
                {
                    "doctype": "Contact",
                    "first_name": f"Undo {self.tag} {len(parties)}",
                    "links": [{"link_doctype": "Customer", "link_name": p} for p in parties],
                }
            )
            .insert()
            .name
        )


class TestBlockers(BlockerCase):
    def test_a_document_linking_to_a_record_blocks_it_as_linked(self):
        session, names = self._session_with_groups()
        outside = self._outside_customer(names[0])

        blocked = self._blocked(self._plan(session))

        blocker = blocked[("Customer Group", names[0])]
        self.assertEqual(blocker.reason, "linked")
        self.assertEqual(blocker.blocked_by, f"Customer {outside}")
        self.assertIn(outside, blocker.detail)
        self.assertNotIn(("Customer Group", names[1]), blocked)

    def test_the_sessions_own_rows_do_not_block(self):
        session, names = self._session_with_groups(1)
        records = [import_undo.Record("Customer Group", names[0])]

        unignored = import_undo.link_blockers(records, set())
        ignored = import_undo.link_blockers(records, {("FAC Import Session", session)})

        self.assertEqual([b.blocked_by for b in unignored], [f"FAC Import Session {session}"])
        self.assertEqual(ignored, [])

    def test_a_link_from_a_child_table_blocks_it(self):
        session = self._session_with_customers()
        alpha = self._names()["alpha"]
        item = (
            frappe.get_doc(
                {
                    "doctype": "Item",
                    "item_code": f"Undo Item {self.tag}",
                    "item_group": frappe.db.get_value("Item Group", {"is_group": 0}),
                    "stock_uom": frappe.db.get_value("UOM", {}) or "Nos",
                    "customer_items": [{"customer_name": alpha, "ref_code": f"R{self.tag}"}],
                }
            )
            .insert()
            .name
        )

        blocker = self._blocked(self._plan(session))[("Customer", alpha)]

        self.assertEqual((blocker.reason, blocker.blocked_by), ("linked", f"Item {item}"))

    def test_a_dynamic_link_blocks_it(self):
        session = self._session_with_customers()
        gamma = self._names()["gamma"]
        bank = frappe.get_doc({"doctype": "Bank", "bank_name": f"Undo Bank {self.tag}"}).insert().name
        account = (
            frappe.get_doc(
                {
                    "doctype": "Bank Account",
                    "account_name": f"Undo {self.tag}",
                    "bank": bank,
                    "party_type": "Customer",
                    "party": gamma,
                }
            )
            .insert()
            .name
        )

        blocker = self._blocked(self._plan(session))[("Customer", gamma)]

        self.assertEqual((blocker.reason, blocker.blocked_by), ("linked", f"Bank Account {account}"))

    def test_a_cancelled_document_does_not_block(self):
        session, names = self._session_with_groups()
        outside = self._outside_customer(names[0])
        frappe.db.set_value("Customer", outside, "docstatus", 2, update_modified=False)

        self.assertEqual(self._plan(session).blocked, [])

    def test_a_document_inside_the_set_does_not_block(self):
        session = self._session_with_customers()
        self._add(session, "Customer Group", self.group, "All Customer Groups")

        plan = self._plan(session)

        self.assertEqual(plan.blocked, [])
        self.assertIn(("Customer Group", self.group), {(r.doctype, r.name) for r in plan.to_delete})

    def test_a_record_edited_by_another_user_is_changed(self):
        session, names = self._session_with_groups()
        self._edited_by_someone_else("Customer Group", names[0])

        blocked = self._blocked(self._plan(session))

        blocker = blocked[("Customer Group", names[0])]
        self.assertEqual((blocker.reason, blocker.blocked_by), ("changed", None))
        self.assertIn("Guest", blocker.detail)
        self.assertNotIn(("Customer Group", names[1]), blocked)

    def test_a_record_modified_after_the_grace_is_changed(self):
        session, names = self._session_with_groups()
        late = import_undo.finished_at(self._doc(session)) + timedelta(
            minutes=import_undo.UNDO_GRACE_MINUTES + 1
        )
        frappe.db.set_value("Customer Group", names[0], "modified", late, update_modified=False)

        blocker = self._blocked(self._plan(session))[("Customer Group", names[0])]

        self.assertEqual(blocker.reason, "changed")

    def test_a_record_modified_inside_the_grace_by_the_session_user_is_not_changed(self):
        session, names = self._session_with_groups()
        soon = import_undo.finished_at(self._doc(session)) + timedelta(
            minutes=import_undo.UNDO_GRACE_MINUTES - 5
        )
        frappe.db.set_value("Customer Group", names[0], "modified", soon, update_modified=False)

        self.assertEqual(self._plan(session).blocked, [])

    def test_a_posting_date_on_or_before_accounts_frozen_till_is_closed_books(self):
        session, _names = self._session_with_groups()
        entry = self._journal_entry(session, "2024-03-31")

        with patch.object(import_undo, "_frozen_till", return_value=getdate("2024-03-31")):
            on_the_day = self._blocked(self._plan(session))
        with patch.object(import_undo, "_frozen_till", return_value=getdate("2024-03-30")):
            the_day_before = self._blocked(self._plan(session))

        blocker = on_the_day[("Journal Entry", entry)]
        self.assertEqual(blocker.reason, "closed_books")
        self.assertIn("2024-03-31", blocker.detail)
        self.assertNotIn(("Journal Entry", entry), the_day_before)

    def test_a_posting_date_in_a_period_closing_voucher_is_closed_books(self):
        session, _names = self._session_with_groups()
        entry = self._journal_entry(session, "2024-03-31")

        with patch.object(import_undo, "_frozen_till", return_value=None), patch.object(
            import_undo, "_closed_till", return_value={self.company: getdate("2024-12-31")}
        ):
            blocked = self._blocked(self._plan(session))

        blocker = blocked[("Journal Entry", entry)]
        self.assertEqual(blocker.reason, "closed_books")
        self.assertIn("Period Closing Voucher", blocker.detail)

    def test_stock_movement_after_the_reconciliation_is_later_stock(self):
        session, _names = self._session_with_groups()
        recon = self._reconciliation(session)
        self._ledger("own", "Stock Reconciliation", recon, 2)  # made by the undo set: does not count
        self._ledger("cancelled", "Stock Entry", f"UNDO-SE0-{self.tag}", 1)
        frappe.db.set_value(
            "Stock Ledger Entry", f"UNDO-SLE-cancelled-{self.tag}", "is_cancelled", 1, update_modified=False
        )
        with self._books_open():
            before = self._blocked(self._plan(session))
            self._ledger("later", "Stock Entry", f"UNDO-SE-{self.tag}", 1)
            after = self._blocked(self._plan(session))

        self.assertNotIn(("Stock Reconciliation", recon), before)
        blocker = after[("Stock Reconciliation", recon)]
        self.assertEqual(
            (blocker.reason, blocker.blocked_by), ("later_stock", f"Stock Entry UNDO-SE-{self.tag}")
        )
        self.assertIn(self.stock_item, blocker.detail)

    def test_a_draft_reconciliation_is_neither_closed_books_nor_later_stock(self):
        session, _names = self._session_with_groups()
        recon = self._reconciliation(session, docstatus=0)
        self._ledger("later", "Stock Entry", f"UNDO-SE-{self.tag}", 1)

        with patch.object(import_undo, "_frozen_till", return_value=getdate(nowdate())), patch.object(
            import_undo, "_closed_till", return_value={self.company: getdate(nowdate())}
        ):
            blocked = self._blocked(self._plan(session))

        self.assertNotIn(("Stock Reconciliation", recon), blocked)

    def test_a_doctype_without_delete_permission_is_no_permission(self):
        session, names = self._session_with_groups()
        real = frappe.has_permission

        def no_delete(doctype=None, ptype="read", *args, **kwargs):
            if doctype == "Customer Group" and ptype == "delete":
                return False
            return real(doctype, ptype, *args, **kwargs)

        with patch.object(import_undo.frappe, "has_permission", side_effect=no_delete):
            blocked = self._blocked(self._plan(session))

        self.assertEqual(
            {key: b.reason for key, b in blocked.items()},
            {("Customer Group", name): "no_permission" for name in names},
        )

    def test_a_submitted_record_needs_cancel_permission(self):
        session, _names = self._session_with_groups()
        entry = self._journal_entry(session, nowdate())
        real = frappe.has_permission

        def no_cancel(doctype=None, ptype="read", *args, **kwargs):
            if doctype == "Journal Entry" and ptype == "cancel":
                return False
            return real(doctype, ptype, *args, **kwargs)

        with patch.object(import_undo, "_frozen_till", return_value=None), patch.object(
            import_undo, "_closed_till", return_value={}
        ), patch.object(import_undo.frappe, "has_permission", side_effect=no_cancel):
            blocked = self._blocked(self._plan(session))

        blocker = blocked[("Journal Entry", entry)]
        self.assertEqual(blocker.reason, "no_permission")
        self.assertIn("cancel", blocker.detail)

    def test_ledger_rows_of_a_voucher_in_the_set_do_not_block_its_customer(self):
        session = self._session_with_customers()
        alpha = self._names()["alpha"]
        invoice = self._insert_raw(
            {
                "doctype": "Sales Invoice",
                "name": f"UNDO-SI-{self.tag}",
                "customer": alpha,
                "company": self.company,
                "posting_date": nowdate(),
                "docstatus": 1,
            }
        ).name
        self._add(session, "Sales Invoice", invoice)
        self._gl_entry("own", alpha, "Sales Invoice", invoice)

        with self._books_open():
            blocked = self._blocked(self._plan(session))

        self.assertNotIn(("Customer", alpha), blocked)
        self.assertNotIn(("Sales Invoice", invoice), blocked)

    def test_ledger_rows_of_a_voucher_outside_the_set_block_its_customer(self):
        session = self._session_with_customers()
        alpha = self._names()["alpha"]
        entry = self._gl_entry("outside", alpha, "Journal Entry", f"UNDO-JE-OUT-{self.tag}")

        blocker = self._blocked(self._plan(session))[("Customer", alpha)]

        self.assertEqual((blocker.reason, blocker.blocked_by), ("linked", f"GL Entry {entry}"))

    def test_a_contact_of_records_in_the_set_only_does_not_block(self):
        session = self._session_with_customers()
        n = self._names()
        self._contact(n["alpha"], n["gamma"])

        self.assertEqual(self._plan(session).blocked, [])

    def test_a_contact_also_linked_outside_the_set_blocks(self):
        session = self._session_with_customers()
        alpha = self._names()["alpha"]
        contact = self._contact(alpha, self._outside_customer(self.group))

        blocked = self._blocked(self._plan(session))

        self.assertEqual(set(blocked), {("Customer", alpha)})
        self.assertEqual(
            (blocked[("Customer", alpha)].reason, blocked[("Customer", alpha)].blocked_by),
            ("linked", f"Contact {contact}"),
        )

    def test_many_linking_documents_stay_one_query_and_one_row_per_field(self):
        session, names = self._session_with_groups(2)
        records = [import_undo.Record("Customer Group", name) for name in names]
        ignore = {("FAC Import Session", session)}
        real_sql = frappe.db.sql

        def measure():
            sizes = []

            def counting(*args, **kwargs):
                result = real_sql(*args, **kwargs)
                sizes.append(len(result) if isinstance(result, (list, tuple)) else 0)
                return result

            with patch.object(frappe.db, "sql", side_effect=counting):
                found = import_undo.link_blockers(records, ignore)
            return found, sizes

        frappe.get_doc(
            {"doctype": "Customer", "customer_name": f"Many {self.tag} 0", "customer_group": names[0]}
        ).insert()
        import_undo.link_blockers(records, ignore)  # warm Frappe's caches
        _found, one_linker = measure()
        for i in range(1, 30):
            frappe.get_doc(
                {"doctype": "Customer", "customer_name": f"Many {self.tag} {i}", "customer_group": names[0]}
            ).insert()
        found, many_linkers = measure()

        self.assertEqual(many_linkers, one_linker)  # the same queries, returning the same rows
        self.assertEqual([b.record.name for b in found], [names[0]])
        self.assertIn("(29 more)", found[0].detail)


class TestCascade(BlockerCase):
    def _customers_with_their_group(self):
        session = self._session_with_customers()
        self._add(session, "Customer Group", self.group, "All Customer Groups")
        return session

    def test_a_kept_customer_keeps_its_customer_group(self):
        session = self._customers_with_their_group()
        alpha = self._names()["alpha"]
        self._edited_by_someone_else("Customer", alpha)

        plan = self._plan(session)

        self.assertEqual(self._blocked(plan)[("Customer", alpha)].reason, "changed")
        because = self._kept(plan)[("Customer Group", self.group)]
        self.assertIn(alpha, because)
        self.assertNotIn(("Customer Group", self.group), {(r.doctype, r.name) for r in plan.to_delete})

    def test_a_kept_tree_child_keeps_its_parent(self):
        session, _names = self._session_with_groups(1)
        top, child = f"Undo Top {self.tag}", f"Undo Child {self.tag}"
        for name, parent, is_group in ((top, "All Customer Groups", 1), (child, top, 0)):
            frappe.get_doc(
                {
                    "doctype": "Customer Group",
                    "customer_group_name": name,
                    "parent_customer_group": parent,
                    "is_group": is_group,
                }
            ).insert()
            self._add(session, "Customer Group", name, parent)
        self._edited_by_someone_else("Customer Group", child)

        plan = self._plan(session)

        self.assertEqual(set(self._blocked(plan)), {("Customer Group", child)})
        self.assertIn(child, self._kept(plan)[("Customer Group", top)])

    def test_kept_records_are_not_counted_as_blocked(self):
        session = self._customers_with_their_group()
        n = self._names()
        self._edited_by_someone_else("Customer", n["alpha"])
        self._edited_by_someone_else("Customer", n["gamma"])

        plan = self._plan(session)

        self.assertEqual(set(self._blocked(plan)), {("Customer", n["alpha"]), ("Customer", n["gamma"])})
        self.assertEqual(set(self._kept(plan)), {("Customer Group", self.group)})

    def test_a_record_blocked_itself_stays_blocked_when_another_depends_on_it(self):
        session = self._customers_with_their_group()
        self._edited_by_someone_else("Customer", self._names()["alpha"])
        self._edited_by_someone_else("Customer Group", self.group)

        plan = self._plan(session)

        self.assertIn(("Customer Group", self.group), self._blocked(plan))
        self.assertEqual(plan.kept, [])


class TestPlan(BlockerCase):
    def test_the_fingerprint_is_stable_and_changes_when_a_record_is_added(self):
        session, _names = self._session_with_groups()
        first = self._plan(session).fingerprint
        reordered = self._doc(session)
        reordered.created_records.reverse()

        self.assertEqual(self._plan(session).fingerprint, first)
        self.assertEqual(import_undo.build_plan(reordered).fingerprint, first)

        extra = f"Undo Extra {self.tag}"
        frappe.get_doc(
            {
                "doctype": "Customer Group",
                "customer_group_name": extra,
                "parent_customer_group": "All Customer Groups",
            }
        ).insert()
        self._add(session, "Customer Group", extra, "All Customer Groups")
        added = self._plan(session).fingerprint
        self.assertNotEqual(added, first)

        self._edited_by_someone_else("Customer Group", extra)
        self.assertNotEqual(self._plan(session).fingerprint, added)

    def test_to_delete_excludes_blocked_and_kept(self):
        session = self._session_with_customers()
        self._add(session, "Customer Group", self.group, "All Customer Groups")
        n = self._names()
        self._edited_by_someone_else("Customer", n["alpha"])

        plan = self._plan(session)

        self.assertEqual(len(plan.records), 4)
        self.assertEqual(
            [(r.doctype, r.name) for r in plan.to_delete],
            [("Customer", n["gamma"]), ("Customer", n["delta"])],
        )
        self.assertEqual((plan.already_removed, plan.note), ([], None))
