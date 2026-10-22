# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Undo an import (#34002): the undo set (what a session created), when a session can be undone at
all, what blocks a record, the delete order and the background job. Sessions are built the way
the import's own tests build them: masters through create_document with import_session,
Customers through a Data Import, opening documents through the opening import tools.
"""

import unittest
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.utils import add_days, add_to_date, get_datetime, getdate, now_datetime, nowdate

from frappe_assistant_core.assistant_core.doctype.fac_import_session.fac_import_session import (
    FACImportSession,
)
from frappe_assistant_core.tests.test_import_opening_balances import OpeningBalancesCase
from frappe_assistant_core.tests.test_import_opening_invoices import OpeningInvoicesCase
from frappe_assistant_core.tests.test_import_opening_stock import OpeningStockCase
from frappe_assistant_core.tests.test_import_run import MAPPING, ImportRunCase
from frappe_assistant_core.utils import import_undo


@unittest.skipUnless("erpnext" in frappe.get_installed_apps(), "needs ERPNext's Customer")
class UndoCase(ImportRunCase):
    """A session that created Customer Groups, and one that imported Customers.

    The run tests need the bench's Redis queue up: import_undo.start enqueues with
    deduplicate=True, and Frappe asks the queue for the job before it honours now=True."""

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
        # The check lists at most 50 missing masters; once those exist, the next check lists the rest.
        checked = self._run("dry_run_import", {"session": session})["result"]
        while True:
            documents = [{"doctype": m["doctype"], "data": m["data"]} for m in checked["masters_plan"]]
            created = self._run("create_document", {"documents": documents, "import_session": session})
            self.assertTrue(created["success"], created)
            if not checked.get("masters_plan_total"):
                return session, names
            checked = self._run("dry_run_import", {"session": session})["result"]

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

    def _as_update(self, session, session_too=True):
        """The session's Data Import ran with Update Existing Records (and the session, unless
        ``session_too`` is false: a session reused afterwards by an opening import is Insert)."""
        doc = self._doc(session)
        frappe.db.set_value(
            "Data Import", doc.data_imports[-1].data_import, "import_type", "Update Existing Records"
        )
        if session_too:
            doc.db_set("import_type", "Update Existing Records")

    def test_an_update_session_has_no_records_and_a_note(self):
        session = self._session_with_customers()
        self._as_update(session)

        present, removed, note = import_undo.undo_set(self._doc(session))

        self.assertEqual((present, removed), ([], []))
        self.assertIn("Update Existing Records", note)
        self.assertIn("never deleted", note)

    def test_an_update_data_import_on_an_insert_session_contributes_no_records(self):
        session = self._session_with_customers()
        self._as_update(session, session_too=False)
        self.assertEqual(self._doc(session).import_type, "Insert New Records")

        present, removed, note = import_undo.undo_set(self._doc(session))

        self.assertEqual((present, removed), ([], []))
        self.assertIn("Update Existing Records", note)

    def test_the_data_imports_own_type_decides_not_the_sessions(self):
        session = self._session_with_customers()
        self._doc(session).db_set("import_type", "Update Existing Records")  # the Data Import inserted

        present, _removed, note = import_undo.undo_set(self._doc(session))

        n = self._names()
        self.assertEqual({r.name for r in present}, {n["alpha"], n["gamma"], n["delta"]})
        self.assertIsNone(note)


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

    def test_an_import_step_that_ended_with_a_warning_is_the_finish(self):
        session = self._session_with_customers()
        doc = self._doc(session)
        imports = [s for s in doc.steps if s.step == "Import"]
        for step in imports:
            step.outcome = "Warning"  # a partial Data Import, or opening rows refused
        finish = max(s.timestamp for s in imports)
        doc.append(
            "steps", {"step": "Map Columns", "outcome": "Success", "timestamp": add_to_date(finish, hours=1)}
        )

        self.assertEqual(import_undo.finished_at(doc), finish)

    def test_a_later_undo_or_dry_run_step_does_not_move_the_finish(self):
        session, _names = self._session_with_groups()
        doc = self._doc(session)
        made = [s.timestamp for s in doc.steps if s.step == "Create Masters"][-1]
        later = add_to_date(made, hours=2)
        doc.append("steps", {"step": "Undo", "outcome": "Failed", "timestamp": later})
        doc.append("steps", {"step": "Dry Run", "outcome": "Success", "timestamp": later})

        self.assertEqual(import_undo.finished_at(doc), made)

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
        """List ``name`` on the session as a record it made. A record made before the session (the
        test's Customer Group, say) is dated from the session's start, as one the import made."""
        doc = self._doc(session)
        doc.append(
            "created_records", {"record_doctype": doctype, "record_name": name, "parent_record": parent}
        )
        doc.save()
        if get_datetime(frappe.db.get_value(doctype, name, "creation")) < get_datetime(doc.creation):
            frappe.db.set_value(doctype, name, "creation", doc.creation, update_modified=False)

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
        with patch.object(import_undo, "_frozen_till", return_value={}), patch.object(
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

    def _item_with_a_price(self, session):
        """An Item the import made with a Standard Selling Rate: ERPNext's after_insert adds its
        Item Price in the same insert, as the same user."""
        item = (
            frappe.get_doc(
                {
                    "doctype": "Item",
                    "item_code": f"Priced Item {self.tag}",
                    "item_group": frappe.db.get_value("Item Group", {"is_group": 0}),
                    "stock_uom": frappe.db.get_value("UOM", {}) or "Nos",
                    "standard_rate": 100,
                }
            )
            .insert()
            .name
        )
        self._add(session, "Item", item)
        self.assertTrue(frappe.db.exists("Item Price", {"item_code": item}), "ERPNext added no price")
        return item

    def test_the_item_price_erpnext_made_with_the_item_does_not_block_it(self):
        # Item.on_trash deletes the item's prices itself, so Frappe's own delete allows it
        session = self._blank_session()
        item = self._item_with_a_price(session)

        self.assertNotIn(("Item", item), self._blocked(self._plan(session)))

    def test_a_price_another_user_added_to_the_item_blocks_it(self):
        session = self._blank_session()
        item = self._item_with_a_price(session)
        price_list = frappe.get_all("Price List", filters={"buying": 1}, pluck="name", limit=1)[0]
        price = frappe.get_doc(
            {"doctype": "Item Price", "item_code": item, "price_list": price_list, "price_list_rate": 80}
        ).insert()
        frappe.db.set_value("Item Price", price.name, "owner", "Guest", update_modified=False)

        blocker = self._blocked(self._plan(session))[("Item", item)]

        self.assertEqual((blocker.reason, blocker.blocked_by), ("linked", f"Item Price {price.name}"))

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

    def test_a_record_created_after_the_grace_is_changed(self):
        # Deleted by hand and made again under the same name, by the session's user: not the
        # record the import made, whatever its modified time says.
        session, names = self._session_with_groups()
        late = import_undo.finished_at(self._doc(session)) + timedelta(
            minutes=import_undo.UNDO_GRACE_MINUTES + 1
        )
        frappe.db.set_value("Customer Group", names[0], "creation", late, update_modified=False)

        blocked = self._blocked(self._plan(session))

        blocker = blocked[("Customer Group", names[0])]
        self.assertEqual((blocker.reason, blocker.blocked_by), ("changed", None))
        self.assertIn("created", blocker.detail)
        self.assertNotIn(("Customer Group", names[1]), blocked)

    def test_a_record_merged_into_an_older_one_leaves_the_older_one_changed_and_kept(self):
        # Frappe's merge rewrites the session's Created Records row (a Dynamic Link) to name the
        # record it merged into, which existed before the import.
        older = (
            frappe.get_doc(
                {
                    "doctype": "Customer Group",
                    "customer_group_name": f"Older {self.tag}",
                    "parent_customer_group": "All Customer Groups",
                }
            )
            .insert()
            .name
        )
        session, names = self._session_with_groups(2)
        frappe.rename_doc("Customer Group", names[0], older, merge=True)
        self.assertIn(older, [r.record_name for r in self._doc(session).created_records])

        blocked = self._blocked(self._plan(session))

        blocker = blocked[("Customer Group", older)]
        self.assertEqual((blocker.reason, blocker.blocked_by), ("changed", None))
        self.assertIn("existed before this import", blocker.detail)
        self.assertNotIn(("Customer Group", names[1]), blocked)

        import_undo.start(self._doc(session), "unblocked")

        self.assertTrue(frappe.db.exists("Customer Group", older))
        self.assertFalse(frappe.db.exists("Customer Group", names[1]))
        self.assertEqual(self._doc(session).status, "Partially Undone")

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

        with patch.object(import_undo, "_frozen_till", return_value={None: getdate("2024-03-31")}):
            on_the_day = self._blocked(self._plan(session))
        with patch.object(import_undo, "_frozen_till", return_value={None: getdate("2024-03-30")}):
            the_day_before = self._blocked(self._plan(session))

        blocker = on_the_day[("Journal Entry", entry)]
        self.assertEqual(blocker.reason, "closed_books")
        self.assertIn("2024-03-31", blocker.detail)
        self.assertNotIn(("Journal Entry", entry), the_day_before)

    def test_accounts_frozen_till_is_read_per_company_where_the_company_holds_it(self):
        # Frappe v16 keeps the date on each Company (accounts_frozen_till_date) and no longer has
        # Accounts Settings' acc_frozen_upto. A Company date field stands in for it on v15.
        frappe.db.set_value("Company", self.company, "date_of_incorporation", "2024-03-31")
        with patch.object(import_undo, "COMPANY_FROZEN_FIELD", "date_of_incorporation"), patch.object(
            import_undo, "SETTINGS_FROZEN_FIELD", "no_such_field"
        ):
            frozen = import_undo._frozen_till([self.company])

        self.assertEqual(frozen, {self.company: getdate("2024-03-31")})

    def test_accounts_frozen_till_falls_back_to_accounts_settings_for_every_company(self):
        # v15: one date in Accounts Settings, for all companies.
        frappe.db.set_single_value("Accounts Settings", "acc_frozen_upto", "2024-03-31")
        with patch.object(import_undo, "COMPANY_FROZEN_FIELD", "no_such_field"):
            frozen = import_undo._frozen_till([self.company])

        self.assertEqual(frozen[None], getdate("2024-03-31"))

    def test_no_frozen_date_field_is_no_frozen_date(self):
        with patch.object(import_undo, "COMPANY_FROZEN_FIELD", "no_such_field"), patch.object(
            import_undo, "SETTINGS_FROZEN_FIELD", "no_such_field"
        ):
            self.assertEqual(import_undo._frozen_till([self.company]), {})

    def test_the_closed_period_is_found_without_asking_get_all_for_a_sql_function(self):
        from frappe_assistant_core.tests.test_import_opening_stock import refuse_sql_functions

        # Submitted Period Closing Vouchers, written straight to the table (see the class note).
        for end in ("2023-03-31", "2024-03-31"):
            self._insert_raw(
                {
                    "doctype": "Period Closing Voucher",
                    "name": f"UNDO-PCV-{end}-{self.tag}",
                    "company": self.company,
                    "period_start_date": end[:4] + "-01-01",
                    "period_end_date": end,
                    "docstatus": 1,
                }
            )

        with patch("frappe.get_all", refuse_sql_functions(frappe.get_all)):
            closed = import_undo._closed_till([self.company])

        self.assertEqual(closed[self.company], getdate("2024-03-31"))

    def test_a_posting_date_in_a_period_closing_voucher_is_closed_books(self):
        session, _names = self._session_with_groups()
        entry = self._journal_entry(session, "2024-03-31")

        with patch.object(import_undo, "_frozen_till", return_value={}), patch.object(
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

        with patch.object(import_undo, "_frozen_till", return_value={None: getdate(nowdate())}), patch.object(
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

        with patch.object(import_undo, "_frozen_till", return_value={}), patch.object(
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
        self._gl_entry("outside", alpha, "Journal Entry", f"UNDO-JE-OUT-{self.tag}")

        blocker = self._blocked(self._plan(session))[("Customer", alpha)]

        # the voucher behind the ledger row, which the user can cancel; not the row itself
        self.assertEqual(
            (blocker.reason, blocker.blocked_by), ("linked", f"Journal Entry UNDO-JE-OUT-{self.tag}")
        )
        self.assertIn(f"Journal Entry UNDO-JE-OUT-{self.tag}", blocker.detail)

    def test_a_ledger_row_with_an_empty_voucher_is_named_itself(self):
        session = self._session_with_customers()
        alpha = self._names()["alpha"]
        entry = self._gl_entry("novoucher", alpha, "", "")

        blocker = self._blocked(self._plan(session))[("Customer", alpha)]

        self.assertEqual((blocker.reason, blocker.blocked_by), ("linked", f"GL Entry {entry}"))

    def test_ledger_rows_without_a_voucher_are_counted(self):
        session = self._session_with_customers()
        alpha = self._names()["alpha"]
        self._gl_entry("outside", alpha, "Journal Entry", f"UNDO-JE-OUT-{self.tag}")
        self._gl_entry("null", alpha, None, None)

        blocker = self._blocked(self._plan(session))[("Customer", alpha)]

        self.assertEqual(blocker.reason, "linked")
        self.assertIn("(1 more)", blocker.detail)

    def test_a_contact_of_records_in_the_set_only_does_not_block(self):
        session = self._session_with_customers()
        n = self._names()
        self._contact(n["alpha"], n["gamma"])

        self.assertEqual(self._plan(session).blocked, [])

    def _contact_made(self, session, party, owner=None, minutes_after=0):
        """A Contact of ``party`` alone, made by ``owner`` (the session user when None)
        ``minutes_after`` the import finished."""
        contact = self._contact(party)
        when = import_undo.finished_at(self._doc(session)) + timedelta(minutes=minutes_after)
        values = {"creation": when, "owner": owner or self._doc(session).user or "Administrator"}
        frappe.db.set_value("Contact", contact, values, update_modified=False)
        return contact

    def test_a_contact_added_later_by_another_user_blocks(self):
        session = self._session_with_customers()
        alpha = self._names()["alpha"]
        late = import_undo.UNDO_GRACE_MINUTES + 60
        contact = self._contact_made(session, alpha, owner="Guest", minutes_after=late)

        blocker = self._blocked(self._plan(session))[("Customer", alpha)]

        self.assertEqual((blocker.reason, blocker.blocked_by), ("linked", f"Contact {contact}"))

    def test_a_contact_by_another_user_blocks_even_inside_the_grace(self):
        session = self._session_with_customers()
        alpha = self._names()["alpha"]
        contact = self._contact_made(session, alpha, owner="Guest", minutes_after=1)

        blocker = self._blocked(self._plan(session))[("Customer", alpha)]

        self.assertEqual((blocker.reason, blocker.blocked_by), ("linked", f"Contact {contact}"))

    def test_a_contact_by_the_session_user_after_the_grace_blocks(self):
        session = self._session_with_customers()
        alpha = self._names()["alpha"]
        contact = self._contact_made(session, alpha, minutes_after=import_undo.UNDO_GRACE_MINUTES + 1)

        blocker = self._blocked(self._plan(session))[("Customer", alpha)]

        self.assertEqual((blocker.reason, blocker.blocked_by), ("linked", f"Contact {contact}"))

    def test_a_contact_by_the_session_user_inside_the_grace_does_not_block(self):
        session = self._session_with_customers()
        alpha = self._names()["alpha"]
        self._contact_made(session, alpha, minutes_after=import_undo.UNDO_GRACE_MINUTES - 5)

        self.assertEqual(self._plan(session).blocked, [])

    def test_a_contact_edited_by_another_user_blocks(self):
        session = self._session_with_customers()
        alpha = self._names()["alpha"]
        contact = self._contact_made(session, alpha, minutes_after=1)
        frappe.db.set_value("Contact", contact, "modified_by", "Guest", update_modified=False)

        blocker = self._blocked(self._plan(session))[("Customer", alpha)]

        self.assertEqual((blocker.reason, blocker.blocked_by), ("linked", f"Contact {contact}"))

    def test_a_contact_changed_after_the_grace_blocks(self):
        session = self._session_with_customers()
        alpha = self._names()["alpha"]
        contact = self._contact_made(session, alpha, minutes_after=1)
        late = import_undo.finished_at(self._doc(session)) + timedelta(
            minutes=import_undo.UNDO_GRACE_MINUTES + 1
        )
        frappe.db.set_value("Contact", contact, "modified", late, update_modified=False)

        blocker = self._blocked(self._plan(session))[("Customer", alpha)]

        self.assertEqual((blocker.reason, blocker.blocked_by), ("linked", f"Contact {contact}"))

    def test_a_contact_older_than_the_session_blocks(self):
        # A pre-existing Customer merged into an imported one: Frappe rewrites its Contact's
        # Dynamic Link by SQL, so the Contact, made before the import by the same user and not
        # modified since, links only to the imported Customer.
        session = self._session_with_customers()
        alpha = self._names()["alpha"]
        contact = self._contact(alpha)
        before = get_datetime(self._doc(session).creation) - timedelta(minutes=1)
        frappe.db.set_value("Contact", contact, "creation", before, update_modified=False)

        blocker = self._blocked(self._plan(session))[("Customer", alpha)]

        self.assertEqual((blocker.reason, blocker.blocked_by), ("linked", f"Contact {contact}"))

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

    def test_a_session_with_an_insert_and_an_update_import_says_the_update_is_not_restored(self):
        session = self._session_with_customers()
        update = self._insert_raw(
            {
                "doctype": "Data Import",
                "name": f"UNDO-DI-{self.tag}",
                "reference_doctype": "Customer",
                "import_type": "Update Existing Records",
                "status": "Success",
            }
        ).name
        doc = self._doc(session)
        doc.append("data_imports", {"data_import": update, "purpose": "Continue", "status": "Success"})
        doc.save()

        plan = self._plan(session)

        n = self._names()
        self.assertEqual({r.name for r in plan.records}, {n["alpha"], n["gamma"], n["delta"]})
        self.assertIn("Update Existing Records", plan.note)
        self.assertIn("not restored", plan.note)
        self.assertEqual(import_undo.plan_refusal(plan), "")

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


class TestOrder(UndoCase):
    def _order(self, records):
        return [(r.doctype, r.name) for r in import_undo.delete_order(records)]

    def test_documents_are_removed_before_the_masters_they_link_to(self):
        R = import_undo.Record
        created = [
            R("Customer Group", "g", "All Customer Groups"),
            R("Item", "i"),
            R("Customer", "c"),
            R("Batch", "b"),
            R("Stock Reconciliation", "sr"),
            R("Sales Invoice", "si"),
        ]

        for records in (created, list(reversed(created))):
            order = [doctype for doctype, _name in self._order(records)]
            self.assertEqual(len(order), len(created))
            before = order.index
            self.assertLess(before("Sales Invoice"), before("Customer"))
            self.assertLess(before("Sales Invoice"), before("Item"))
            self.assertLess(before("Customer"), before("Customer Group"))
            self.assertLess(before("Stock Reconciliation"), before("Batch"))
            self.assertLess(before("Batch"), before("Item"))

    def test_tree_children_are_removed_before_their_parents(self):
        R = import_undo.Record
        records = [
            R("Customer Group", "child", "top"),  # listed before its parent
            R("Customer Group", "top", "All Customer Groups"),
            R("Customer Group", "leaf", "child"),
            R("Customer Group", "other", "All Customer Groups"),
        ]

        order = [name for _doctype, name in self._order(records)]

        self.assertLess(order.index("leaf"), order.index("child"))
        self.assertLess(order.index("child"), order.index("top"))
        self.assertEqual(order, ["leaf", "child", "other", "top"])  # then reverse creation


class TestRun(BlockerCase):
    """The undo job, run inline (frappe.flags.in_test) by start()."""

    def _start(self, session, mode="all"):
        return import_undo.start(self._doc(session), mode)

    def _undo_options(self, session):
        return (frappe.parse_json(self._doc(session).template_options or "{}") or {}).get("undo") or {}

    def _last_step(self, session):
        return self._doc(session).steps[-1]

    def _exists(self, doctype, name):
        return bool(frappe.db.exists(doctype, name))

    def test_the_job_removes_the_created_records_and_ends_undone(self):
        session, names = self._session_with_groups(2)

        self.assertEqual(self._start(session), "started")

        doc = self._doc(session)
        self.assertEqual([n for n in names if self._exists("Customer Group", n)], [])
        self.assertEqual(doc.status, "Undone")
        self.assertEqual(doc.created_records, [])
        step = self._last_step(session)
        self.assertEqual((step.step, step.outcome), ("Undo", "Success"))
        result = self._undo_options(session)["result"]
        self.assertEqual(
            {k: result[k] for k in ("deleted", "already_removed", "kept", "blocked")},
            {"deleted": 2, "already_removed": 0, "kept": 0, "blocked": []},
        )
        # what was removed is still on the session, though its Created Records rows are gone
        self.assertEqual(sorted(result["removed"]), sorted(f"Customer Group {n}" for n in names))

    def test_the_created_records_row_goes_before_the_delete(self):
        session, names = self._session_with_groups(1)
        frappe.db.savepoint("fac_test_link")
        with self.assertRaises(frappe.LinkExistsError):  # the session's Dynamic Link holds it
            frappe.delete_doc("Customer Group", names[0])
        frappe.db.rollback(save_point="fac_test_link")
        frappe.clear_messages()

        self._start(session)

        self.assertFalse(self._exists("Customer Group", names[0]))
        self.assertEqual(self._doc(session).status, "Undone")

    def test_a_record_that_fails_to_delete_is_kept_with_its_dependencies_and_the_session_is_partially_undone(
        self,
    ):
        session = self._session_with_customers()
        self._add(session, "Customer Group", self.group, "All Customer Groups")
        n = self._names()
        real = frappe.delete_doc

        def refuse(doctype=None, name=None, *args, **kwargs):
            if name == n["alpha"]:
                frappe.throw(frappe._("Alpha is refused"))
            if name == n["gamma"]:  # deleted, then refused: the savepoint brings it back
                real(doctype, name, *args, **kwargs)
                raise frappe.ValidationError("Gamma is refused after its delete")
            return real(doctype, name, *args, **kwargs)

        with patch.object(import_undo.frappe, "delete_doc", side_effect=refuse):
            self._start(session)

        doc = self._doc(session)
        self.assertEqual(doc.status, "Partially Undone")
        self.assertTrue(self._exists("Customer", n["alpha"]))
        self.assertTrue(self._exists("Customer", n["gamma"]))
        self.assertFalse(self._exists("Customer", n["delta"]))
        self.assertTrue(self._exists("Customer Group", self.group))  # alpha and gamma need it
        self.assertIn(self.group, [r.record_name for r in doc.created_records])
        result = self._undo_options(session)["result"]
        self.assertEqual((result["deleted"], result["kept"]), (1, 1))
        blocked = {b["record"]: b for b in result["blocked"]}
        self.assertEqual(set(blocked), {f"Customer {n['alpha']}", f"Customer {n['gamma']}"})
        self.assertEqual({b["reason"] for b in blocked.values()}, {"delete_failed"})
        self.assertIn("Alpha is refused", blocked[f"Customer {n['alpha']}"]["detail"])
        self.assertEqual(self._last_step(session).outcome, "Warning")

    def test_a_record_deleted_by_hand_before_the_job_reaches_it_is_already_removed(self):
        session, names = self._session_with_groups(2)
        real = import_undo.delete_order

        def then_deleted_by_hand(records):
            order = real(records)
            frappe.db.delete("Customer Group", {"name": names[0]})  # after the plan, before its turn
            return order

        with patch.object(import_undo, "delete_order", side_effect=then_deleted_by_hand):
            self._start(session)

        doc = self._doc(session)
        self.assertEqual(doc.status, "Undone", doc.steps[-1].message)
        self.assertEqual(doc.created_records, [])
        result = self._undo_options(session)["result"]
        self.assertEqual(
            (result["deleted"], result["already_removed"], result["kept"], result["blocked"]), (1, 1, 0, [])
        )
        self.assertEqual(result["removed"], [f"Customer Group {names[1]}"])
        self.assertEqual(result["already_removed_records"], [f"Customer Group {names[0]}"])
        self.assertFalse(self._exists("Customer Group", names[1]))

    def test_a_record_deleted_by_hand_before_the_undo_is_already_removed(self):
        session, names = self._session_with_groups(2)
        frappe.db.delete("Customer Group", {"name": names[0]})

        self._start(session)

        doc = self._doc(session)
        self.assertEqual(doc.status, "Undone", doc.steps[-1].message)
        self.assertEqual(doc.created_records, [])
        result = self._undo_options(session)["result"]
        self.assertEqual((result["deleted"], result["already_removed"], result["blocked"]), (1, 1, []))
        self.assertEqual(result["already_removed_records"], [f"Customer Group {names[0]}"])

    def test_a_not_found_error_for_a_record_that_exists_is_delete_failed(self):
        session, names = self._session_with_groups(2)
        real = frappe.get_doc

        def get_doc(*args, **kwargs):
            if args[1:2] == (names[0],):
                raise frappe.DoesNotExistError("A linked record was not found")
            return real(*args, **kwargs)

        with patch.object(import_undo.frappe, "get_doc", side_effect=get_doc):
            self._start(session, "unblocked")

        self.assertTrue(self._exists("Customer Group", names[0]))
        result = self._undo_options(session)["result"]
        self.assertEqual(
            [(b["record"], b["reason"]) for b in result["blocked"]],
            [(f"Customer Group {names[0]}", "delete_failed")],
        )
        self.assertIn("not found", result["blocked"][0]["detail"])
        self.assertEqual((result["already_removed"], result["already_removed_records"]), (0, []))
        self.assertIn(names[0], [r.record_name for r in self._doc(session).created_records])
        self.assertEqual(self._doc(session).status, "Partially Undone")

    def test_a_second_pass_keeps_what_the_first_removed(self):
        session, names = self._session_with_groups(3)
        frappe.db.delete("Customer Group", {"name": names[2]})  # by hand, before the undo
        self._edited_by_someone_else("Customer Group", names[0])
        self._start(session, "unblocked")
        first = self._undo_options(session)["result"]
        self.assertEqual(self._doc(session).status, "Partially Undone")
        self.assertEqual(first["removed"], [f"Customer Group {names[1]}"])

        # the blocker is cleared, and the rest is undone
        frappe.db.set_value("Customer Group", names[0], "modified_by", "Administrator", update_modified=False)
        self._start(session, "unblocked")

        doc = self._doc(session)
        self.assertEqual(doc.status, "Undone", doc.steps[-1].message)
        result = self._undo_options(session)["result"]
        self.assertEqual(result["deleted"], 1)  # this pass
        self.assertEqual(result["removed"], [f"Customer Group {names[1]}", f"Customer Group {names[0]}"])
        self.assertEqual(
            (result["already_removed"], result["already_removed_records"]),
            (1, [f"Customer Group {names[2]}"]),
        )

    def test_unblocked_mode_keeps_blocked_records_and_removes_the_rest(self):
        session, names = self._session_with_groups(2)
        outside = self._outside_customer(names[0])

        self._start(session, "unblocked")

        self.assertTrue(self._exists("Customer Group", names[0]))
        self.assertFalse(self._exists("Customer Group", names[1]))
        self.assertEqual(self._doc(session).status, "Partially Undone")
        result = self._undo_options(session)["result"]
        self.assertEqual((result["deleted"], result["kept"]), (1, 0))
        self.assertEqual(
            [(b["record"], b["reason"]) for b in result["blocked"]],
            [(f"Customer Group {names[0]}", "linked")],
        )
        self.assertIn(outside, result["blocked"][0]["detail"])

    def test_all_mode_with_blocked_records_removes_nothing_and_restores_the_status(self):
        session, names = self._session_with_groups(2)
        self._outside_customer(names[0])
        before = self._doc(session).status

        self._start(session, "all")

        self.assertTrue(all(self._exists("Customer Group", name) for name in names))
        self.assertEqual(self._doc(session).status, before)
        step = self._last_step(session)
        self.assertEqual((step.step, step.outcome), ("Undo", "Failed"))
        self.assertIn("unblocked", step.message)
        self.assertEqual(import_undo.report(self._doc(session))["state"], "stopped")

    def test_progress_is_written_and_the_result_is_kept(self):
        session, names = self._session_with_groups(3)
        written = []
        real = import_undo._progress

        def spy(state, done=0, total=0, deleted=0):
            written.append((state, done, total))
            return real(state, done, total, deleted)

        with patch.object(import_undo, "BATCH", 2), patch.object(
            import_undo, "_progress", side_effect=spy
        ), patch.object(frappe.db, "commit") as commit:
            self._start(session)

        self.assertIn(("running", 2, 3), written)
        self.assertEqual(written[-1], ("done", 3, 3))
        commit.assert_not_called()  # a test's transaction is never committed
        progress = frappe.parse_json(self._doc(session).progress)
        self.assertEqual(
            (progress["step"], progress["state"], progress["done"], progress["total"]), ("Undo", "done", 3, 3)
        )
        result = self._undo_options(session)["result"]
        removed = [f"Customer Group {n}" for n in reversed(names)]  # in delete order
        self.assertEqual(
            result,
            {
                "deleted": 3,
                "already_removed": 0,
                "kept": 0,
                "blocked": [],
                "removed": removed,
                "already_removed_records": [],
            },
        )
        report = import_undo.report(self._doc(session))
        self.assertEqual(
            report,
            {
                "state": "done",
                "deleted": 3,
                "already_removed": 0,
                "total": 3,
                "kept": 0,
                "blocked": [],
                "removed": removed,
                "already_removed_records": [],
                "reason": None,
            },
        )

    def _versions(self, session):
        return frappe.db.count("Version", {"ref_doctype": "FAC Import Session", "docname": session})

    def test_the_job_does_not_save_the_session_once_per_record(self):
        session, names = self._session_with_groups(8)
        before = self._versions(session)
        real_save = FACImportSession.save

        def tracked(doc, *args, **kwargs):
            # Frappe skips Versions under tests (ignore_version = in_test); on a site each save of
            # the session, which has track_changes, writes one.
            kwargs.setdefault("ignore_version", False)
            return real_save(doc, *args, **kwargs)

        with patch.object(FACImportSession, "save", tracked):
            self._start(session)

        doc = self._doc(session)
        self.assertEqual(doc.status, "Undone")
        self.assertEqual(doc.created_records, [])
        self.assertFalse(any(self._exists("Customer Group", name) for name in names))
        # start, the running progress and the end: not one Version per record removed
        self.assertLessEqual(self._versions(session) - before, 4)

    def test_rows_removed_before_a_batch_boundary_stay_removed_and_a_failed_ones_comes_back(self):
        session, names = self._session_with_groups(5)

        with patch.object(import_undo, "BATCH", 2), patch.object(
            import_undo.frappe, "delete_doc", side_effect=refusing(names[2])
        ):
            self._start(session, "unblocked")

        doc = self._doc(session)
        self.assertEqual(doc.status, "Partially Undone")
        self.assertEqual([r.record_name for r in doc.created_records], [names[2]])
        self.assertEqual([n for n in names if self._exists("Customer Group", n)], [names[2]])
        self.assertEqual(self._undo_options(session)["result"]["deleted"], 4)

    def test_a_second_start_while_running_does_not_enqueue_again(self):
        session, names = self._session_with_groups(1)
        with patch.object(import_undo.frappe, "enqueue") as enqueue:
            self.assertEqual(self._start(session), "started")
        self.assertEqual(enqueue.call_count, 1)
        kwargs = enqueue.call_args.kwargs
        self.assertEqual(enqueue.call_args.args, (f"{import_undo.__name__}.undo_job",))
        self.assertEqual(
            {
                k: kwargs[k]
                for k in ("queue", "timeout", "job_id", "deduplicate", "enqueue_after_commit", "now")
            },
            {
                "queue": "long",
                "timeout": 4 * 60 * 60,
                "job_id": f"fac_import_undo::{session}",
                "deduplicate": True,
                "enqueue_after_commit": True,
                "now": True,
            },
        )
        self.assertEqual(
            (kwargs["session_name"], kwargs["user"], kwargs["mode"]), (session, "Administrator", "all")
        )
        self.assertEqual(self._doc(session).status, "Undoing")

        with patch.object(import_undo, "_job_running", return_value=True), patch.object(
            import_undo.frappe, "enqueue"
        ) as again:
            self.assertEqual(self._start(session), "running")
            self.assertEqual(import_undo.report(self._doc(session))["state"], "running")

        again.assert_not_called()
        self.assertEqual(self._doc(session).status, "Undoing")
        self.assertTrue(self._exists("Customer Group", names[0]))

    def _silent_for(self, session, seconds):
        """The session's last progress write is ``seconds`` old."""
        doc = self._doc(session)
        progress = frappe.parse_json(doc.progress)
        progress["updated_on"] = str(add_to_date(now_datetime(), seconds=-seconds))
        doc.db_set("progress", frappe.as_json(progress), update_modified=False)

    def test_a_job_queued_moments_ago_is_not_called_stopped_before_it_is_in_the_queue(self):
        session, names = self._session_with_groups(1)
        before = self._doc(session).status
        with patch.object(import_undo.frappe, "enqueue"):  # enqueued after the commit: not queued yet
            self._start(session)
        steps = len(self._doc(session).steps)

        with patch.object(import_undo, "_job_running", return_value=False):
            report = import_undo.report(self._doc(session))
            again = self._start(session)

        self.assertEqual((report["state"], again), ("running", "running"))
        doc = self._doc(session)
        self.assertEqual(doc.status, "Undoing")
        self.assertNotEqual(doc.status, before)
        self.assertEqual(len(doc.steps), steps)
        self.assertTrue(self._exists("Customer Group", names[0]))

    def test_a_dead_job_is_reported_stopped_and_the_status_restored(self):
        session, names = self._session_with_groups(1)
        before = self._doc(session).status
        with patch.object(import_undo.frappe, "enqueue"):
            self._start(session)
        self.assertEqual(self._undo_options(session)["previous_status"], before)
        self._silent_for(session, import_undo.STALL_SECONDS + 60)

        with patch.object(import_undo, "_job_running", return_value=False):
            report = import_undo.report(self._doc(session))

        self.assertEqual(report["state"], "stopped")
        doc = self._doc(session)
        self.assertEqual(doc.status, before)
        step = self._last_step(session)
        self.assertEqual((step.step, step.outcome), ("Undo", "Failed"))
        self.assertTrue(self._exists("Customer Group", names[0]))
        self.assertEqual(import_undo.refusal(doc), "")  # it can be undone again

    def test_a_dead_rerun_of_a_partially_undone_session_is_reported_stopped_not_done(self):
        session, names = self._session_with_groups(2)
        self._edited_by_someone_else("Customer Group", names[0])
        self._start(session, "unblocked")
        self.assertEqual(self._doc(session).status, "Partially Undone")
        with patch.object(import_undo.frappe, "enqueue"):
            self._start(session, "unblocked")  # a new run: the result is cleared, the job never runs
        self.assertEqual(self._doc(session).status, "Undoing")
        self._silent_for(session, import_undo.STALL_SECONDS + 60)

        with patch.object(import_undo, "_job_running", return_value=False):
            report = import_undo.report(self._doc(session))

        self.assertEqual(report["state"], "stopped")
        self.assertIn("stopped", report["reason"])
        self.assertEqual(self._doc(session).status, "Partially Undone")
        self.assertEqual(import_undo.report(self._doc(session))["state"], "stopped")  # and stays so

    def test_an_unexpected_error_rolls_back_and_restores_the_status(self):
        session, names = self._session_with_groups(2)
        before = self._doc(session).status
        real = import_undo._remove

        def remove_then_fail(session_doc, record):
            real(session_doc, record)
            raise RuntimeError("the database went away")

        with patch.object(import_undo, "_remove", side_effect=remove_then_fail):
            self._start(session)

        self.assertTrue(all(self._exists("Customer Group", name) for name in names))
        doc = self._doc(session)
        self.assertEqual(doc.status, before)
        self.assertEqual(len(doc.created_records), 2)
        self.assertEqual((doc.steps[-1].step, doc.steps[-1].outcome), ("Undo", "Failed"))

    def test_data_import_records_are_deleted(self):
        session = self._session_with_customers()
        doc = self._doc(session)
        data_import = doc.data_imports[-1].data_import
        logs = frappe.db.count("Data Import Log", {"data_import": data_import})

        self._start(session)

        self.assertEqual(self._imported(), set())
        self.assertTrue(self._exists("Data Import", data_import))
        self.assertEqual(frappe.db.count("Data Import Log", {"data_import": data_import}), logs)
        self.assertEqual(self._doc(session).status, "Undone")
        self.assertEqual(self._undo_options(session)["result"]["deleted"], 3)

    def test_the_job_refuses_a_plan_other_than_the_approved_one(self):
        session, names = self._session_with_groups(2)
        before = self._doc(session).status
        approved = self._plan(session).fingerprint

        import_undo.start(self._doc(session), "all", fingerprint="not " + approved)

        self.assertTrue(all(self._exists("Customer Group", name) for name in names))
        doc = self._doc(session)
        self.assertEqual(doc.status, before)
        self.assertEqual(len(doc.created_records), 2)
        step = self._last_step(session)
        self.assertEqual((step.step, step.outcome), ("Undo", "Failed"))
        self.assertIn("changed since the approval", step.message)

        import_undo.start(self._doc(session), "all", fingerprint=approved)  # the approved plan runs

        self.assertEqual(self._doc(session).status, "Undone")
        self.assertFalse(any(self._exists("Customer Group", name) for name in names))

    def test_an_unknown_mode_is_refused(self):
        session, _names = self._session_with_groups(1)

        with self.assertRaises(import_undo.UndoProblem):
            self._start(session, "everything")

    def test_a_user_who_cannot_delete_keeps_the_records_and_nothing_escalates(self):
        session, names = self._session_with_groups(2)
        user = self.make_throwaway_user("undo", roles=("Sales User",))  # reads Customer Groups only
        self._doc(session).db_set("user", user)  # the user's own session, and own records
        for name in names:
            frappe.db.set_value("Customer Group", name, "modified_by", user, update_modified=False)
        frappe.set_user(user)
        self.assertFalse(frappe.has_permission("Customer Group", "delete"))

        self._start(session, "unblocked")  # the plan finds it: no_permission

        self.assertTrue(all(self._exists("Customer Group", name) for name in names))
        result = self._undo_options(session)["result"]
        self.assertEqual({b["reason"] for b in result["blocked"]}, {"no_permission"})
        self.assertEqual(self._doc(session).status, "Partially Undone")

        with patch.object(import_undo, "_permission_blockers", return_value=[]):
            self._start(session, "all")  # the plan misses it: Frappe's delete, as the user, refuses

        self.assertEqual(frappe.session.user, user)
        self.assertTrue(all(self._exists("Customer Group", name) for name in names))
        doc = self._doc(session)
        result = self._undo_options(session)["result"]
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(
            {(b["record"], b["reason"]) for b in result["blocked"]},
            {(f"Customer Group {name}", "delete_failed") for name in names},
        )
        self.assertTrue(all("not allowed" in b["detail"] for b in result["blocked"]), result)
        self.assertEqual(sorted(r.record_name for r in doc.created_records), sorted(names))
        self.assertEqual(doc.status, "Partially Undone")


class TestRunSubmittedEntry(OpeningBalancesCase):
    """An opening Journal Entry made by import_opening_balances: cancelled, then deleted."""

    def _imported(self):
        session = self._session()
        self.assertTrue(self._setup(session)["success"])
        result = self._run("dry_run_import", {"session": session})["result"]
        card = {
            "session": session,
            "company": self.company,
            "cut_off": result["opening_balances"]["cut_off"],
            "rows": result["ready"],
            "total_debit": result["opening_balances"]["total_debit"],
            "total_credit": result["opening_balances"]["total_credit"],
        }
        reply = self._run("import_opening_balances", card)
        self.assertTrue(reply["success"], reply)
        entry = reply["result"]["journal_entry"]
        self.made_entries.append(entry)
        # The class shares one transaction, and tearDown deletes a kept entry, so the next test's
        # entry gets the same name: this session's row naming it must not outlive the test.
        self.addCleanup(frappe.db.delete, "FAC Import Session Record", {"parent": session})
        return session, entry

    def _books_open(self):
        return patch.multiple(
            import_undo, _frozen_till=lambda companies: {}, _closed_till=lambda companies: {}
        )

    def test_a_voucher_cancelled_before_the_undo_keeps_its_ledger_and_is_kept(self):
        session, entry = self._imported()
        frappe.get_doc("Journal Entry", entry)._cancel()
        own = frappe.db.count("GL Entry", {"voucher_type": "Journal Entry", "voucher_no": entry})

        with self._books_open():
            import_undo.start(self._doc(session), "all")

        self.assertEqual(frappe.db.get_value("Journal Entry", entry, "docstatus"), 2)
        self.assertEqual(
            frappe.db.count("GL Entry", {"voucher_type": "Journal Entry", "voucher_no": entry}), own
        )
        result = (frappe.parse_json(self._doc(session).template_options) or {})["undo"]["result"]
        self.assertEqual(
            [(b["record"], b["reason"]) for b in result["blocked"]],
            [(f"Journal Entry {entry}", "delete_failed")],
        )
        self.assertIn("GL Entry", result["blocked"][0]["detail"])
        self.assertEqual(self._doc(session).status, "Partially Undone")

    def test_a_submitted_document_is_cancelled_then_deleted(self):
        session, entry = self._imported()
        self.assertEqual(frappe.db.get_value("Journal Entry", entry, "docstatus"), 1)
        others = {
            ledger: frappe.db.count(ledger, {"voucher_no": ["!=", entry]})
            for ledger in import_undo.VOUCHER_LEDGERS
        }
        cancelled = []
        real = import_undo._cancel

        def spy(doc):
            real(doc)
            cancelled.append((doc.name, frappe.db.get_value("Journal Entry", doc.name, "docstatus")))

        with patch.object(import_undo, "_cancel", side_effect=spy), patch.object(
            import_undo, "_frozen_till", return_value={}
        ), patch.object(import_undo, "_closed_till", return_value={}):
            import_undo.start(self._doc(session), "all")

        self.assertEqual(cancelled, [(entry, 2)])
        self.assertFalse(frappe.db.exists("Journal Entry", entry))
        for ledger in ("GL Entry", "Payment Ledger Entry"):
            self.assertEqual(
                frappe.db.count(ledger, {"voucher_type": "Journal Entry", "voucher_no": entry}), 0
            )
        for ledger, count in others.items():  # no other voucher's rows
            self.assertEqual(frappe.db.count(ledger, {"voucher_no": ["!=", entry]}), count, ledger)
        doc = self._doc(session)
        self.assertEqual(doc.status, "Undone", doc.steps[-1].message)
        self.assertEqual(frappe.db.get_single_value("Accounts Settings", "delete_linked_ledger_entries"), 0)

    def test_a_failed_delete_restores_the_submitted_entry_and_its_ledger(self):
        session, entry = self._imported()
        before = ledger_counts("Journal Entry", entry)
        self.assertTrue(before["GL Entry"][0])

        with self._books_open(), patch.object(import_undo.frappe, "delete_doc", side_effect=refusing(entry)):
            import_undo.start(self._doc(session), "all")

        self.assertEqual(frappe.db.get_value("Journal Entry", entry, "docstatus"), 1)
        self.assertEqual(ledger_counts("Journal Entry", entry), before)  # rows back, none cancelled
        doc = self._doc(session)
        result = (frappe.parse_json(doc.template_options) or {})["undo"]["result"]
        self.assertEqual(
            [(b["record"], b["reason"]) for b in result["blocked"]],
            [(f"Journal Entry {entry}", "delete_failed")],
        )
        self.assertIn("Refused for the test", result["blocked"][0]["detail"])
        self.assertEqual(
            [(r.record_doctype, r.record_name) for r in doc.created_records], [("Journal Entry", entry)]
        )
        self.assertEqual(doc.status, "Partially Undone")


def ledger_counts(voucher_type, voucher_no):
    """Per ledger, (rows, rows cancelled or delinked) of one voucher."""
    counts = {}
    for ledger, flag in (("GL Entry", "is_cancelled"), ("Payment Ledger Entry", "delinked")):
        filters = {"voucher_type": voucher_type, "voucher_no": voucher_no}
        counts[ledger] = (frappe.db.count(ledger, filters), frappe.db.count(ledger, {**filters, flag: 1}))
    return counts


def refusing(*names):
    """A frappe.delete_doc that refuses ``names`` and deletes everything else."""
    real = frappe.delete_doc

    def delete_doc(doctype=None, name=None, *args, **kwargs):
        if name in names:
            frappe.throw(frappe._("Refused for the test"))
        return real(doctype, name, *args, **kwargs)

    return delete_doc


class TestRunSubmittedInvoice(OpeningInvoicesCase):
    """Opening Sales Invoices made by import_opening_invoices: cancelled, then deleted."""

    def test_submitted_opening_invoices_are_cancelled_then_deleted(self):
        numbers = [f"OI-{self.tag}-{i}" for i in (1, 2)]
        session, result = self._ready([f"{self.customer},{n},{self.date},,10" for n in numbers])
        reply = self._run("import_opening_invoices", self._card(session, result))
        self.assertTrue(reply["success"], reply)
        invoices = [
            r.record_name for r in self._doc(session).created_records if r.record_doctype == "Sales Invoice"
        ]
        self.assertEqual(len(invoices), 2)

        with patch.object(import_undo, "_frozen_till", return_value={}), patch.object(
            import_undo, "_closed_till", return_value={}
        ):
            import_undo.start(self._doc(session), "all")

        doc = self._doc(session)
        self.assertEqual(doc.status, "Undone", doc.steps[-1].message)
        for name in invoices:
            self.assertFalse(frappe.db.exists("Sales Invoice", name))
            for ledger in ("GL Entry", "Payment Ledger Entry"):
                self.assertEqual(
                    frappe.db.count(ledger, {"voucher_type": "Sales Invoice", "voucher_no": name}), 0
                )
        self.assertTrue(frappe.db.exists("Customer", self.customer))  # not made by the session

    def test_a_failed_delete_restores_the_submitted_invoice_and_its_ledger(self):
        numbers = [f"OI-{self.tag}-{i}" for i in (1, 2)]
        session, result = self._ready([f"{self.customer},{n},{self.date},,10" for n in numbers])
        self.assertTrue(self._run("import_opening_invoices", self._card(session, result))["success"])
        refused, other = [
            r.record_name for r in self._doc(session).created_records if r.record_doctype == "Sales Invoice"
        ]
        before = ledger_counts("Sales Invoice", refused)
        outstanding = frappe.db.get_value("Sales Invoice", refused, "outstanding_amount")

        with patch.object(import_undo, "_frozen_till", return_value={}), patch.object(
            import_undo, "_closed_till", return_value={}
        ), patch.object(import_undo.frappe, "delete_doc", side_effect=refusing(refused)):
            import_undo.start(self._doc(session), "all")

        self.assertEqual(
            frappe.db.get_value("Sales Invoice", refused, ["docstatus", "outstanding_amount"]),
            (1, outstanding),
        )
        self.assertEqual(ledger_counts("Sales Invoice", refused), before)
        self.assertFalse(frappe.db.exists("Sales Invoice", other))
        doc = self._doc(session)
        result = (frappe.parse_json(doc.template_options) or {})["undo"]["result"]
        self.assertEqual(
            [(b["record"], b["reason"]) for b in result["blocked"]],
            [(f"Sales Invoice {refused}", "delete_failed")],
        )
        self.assertIn(refused, [r.record_name for r in doc.created_records])
        self.assertEqual(doc.status, "Partially Undone")

    def test_a_payment_against_an_opening_invoice_blocks_it_and_names_the_payment(self):
        from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

        session, result = self._ready([f"{self.customer},OI-{self.tag}-1,{self.date},,10"])
        self.assertTrue(self._run("import_opening_invoices", self._card(session, result))["success"])
        doc = self._doc(session)
        invoice = next(r.record_name for r in doc.created_records if r.record_doctype == "Sales Invoice")
        doc.append("created_records", {"record_doctype": "Customer", "record_name": self.customer})
        doc.save()
        payment = get_payment_entry("Sales Invoice", invoice)
        payment.reference_no, payment.reference_date = f"UNDO-{self.tag}", self.date
        payment.insert()
        payment.submit()

        with patch.object(import_undo, "_frozen_till", return_value={}), patch.object(
            import_undo, "_closed_till", return_value={}
        ):
            plan = import_undo.build_plan(self._doc(session))

        blocked = {(b.record.doctype, b.record.name): b for b in plan.blocked}
        blocker = blocked[("Sales Invoice", invoice)]
        # the Payment Entry the user can cancel, not one of its ledger rows
        self.assertEqual((blocker.reason, blocker.blocked_by), ("linked", f"Payment Entry {payment.name}"))
        self.assertNotIn(("Customer", self.customer), {(r.doctype, r.name) for r in plan.to_delete})


class TestRunSubmittedStock(OpeningStockCase):
    """An opening Stock Reconciliation made by import_opening_stock: cancelled, then deleted."""

    def test_a_submitted_stock_reconciliation_is_cancelled_then_deleted(self):
        session, result = self._ready([f"{self.item},{self.stores},5,10"])
        reply = self._run("import_opening_stock", self._card(session, result))
        self.assertTrue(reply["success"], reply)
        recon = reply["result"]["documents"][0]
        self.assertEqual(self._qty(self.item, self.stores), 5)

        with patch.object(import_undo, "_frozen_till", return_value={}), patch.object(
            import_undo, "_closed_till", return_value={}
        ):
            import_undo.start(self._doc(session), "all")

        doc = self._doc(session)
        self.assertEqual(doc.status, "Undone", doc.steps[-1].message)
        self.assertFalse(frappe.db.exists("Stock Reconciliation", recon))
        for ledger in ("Stock Ledger Entry", "GL Entry"):
            self.assertEqual(
                frappe.db.count(ledger, {"voucher_type": "Stock Reconciliation", "voucher_no": recon}), 0
            )
        self.assertEqual(self._qty(self.item, self.stores), 0)

    def _batched(self):
        """An opening Stock Reconciliation of a batch item, whose batch the import made."""
        old = frappe.db.get_single_value("Stock Settings", "use_serial_batch_fields")
        frappe.db.set_single_value("Stock Settings", "use_serial_batch_fields", 1)
        self.addCleanup(frappe.db.set_single_value, "Stock Settings", "use_serial_batch_fields", old)
        item = self._item(f"OS-UB-{self.tag}", has_batch_no=1, create_new_batch=0)
        batch = f"UB-{self.tag}"
        session = self._session([f"{item},{self.stores},3,2,{batch}"], header="Item,Warehouse,Qty,Rate,Batch")
        columns = {
            "Item": "item_code",
            "Warehouse": "warehouse",
            "Qty": "qty",
            "Rate": "valuation_rate",
            "Batch": "batch_no",
        }
        self._setup(session, columns=columns)
        result = self._run("dry_run_import", {"session": session})["result"]
        reply = self._run("import_opening_stock", self._card(session, result))
        self.assertTrue(reply["success"], reply)
        recon = reply["result"]["documents"][0]
        made = {(r.record_doctype, r.record_name) for r in self._doc(session).created_records}
        self.assertEqual(made, {("Stock Reconciliation", recon), ("Batch", batch)})
        self.assertTrue(self._bundles(recon))
        return session, recon, batch, item

    def _bundles(self, recon):
        """The reconciliation's Serial and Batch Bundles: {name: (docstatus, is_cancelled, entries)}."""
        bundles = frappe.get_all(
            "Serial and Batch Bundle",
            filters={"voucher_type": "Stock Reconciliation", "voucher_no": recon},
            fields=["name", "docstatus", "is_cancelled"],
        )
        return {
            b.name: (
                b.docstatus,
                b.is_cancelled,
                frappe.db.count("Serial and Batch Entry", {"parent": b.name}),
            )
            for b in bundles
        }

    def test_a_batched_opening_stock_is_undone_with_its_bundles_and_batch(self):
        session, recon, batch, item = self._batched()
        bundles = list(self._bundles(recon))

        with patch.object(import_undo, "_frozen_till", return_value={}), patch.object(
            import_undo, "_closed_till", return_value={}
        ):
            self.assertEqual(import_undo.build_plan(self._doc(session)).blocked, [])
            import_undo.start(self._doc(session), "all")

        doc = self._doc(session)
        self.assertEqual(doc.status, "Undone", doc.steps[-1].message)
        self.assertFalse(frappe.db.exists("Stock Reconciliation", recon))
        self.assertEqual(self._bundles(recon), {})
        self.assertEqual(frappe.db.count("Serial and Batch Entry", {"parent": ["in", bundles]}), 0)
        self.assertFalse(frappe.db.exists("Batch", batch))
        self.assertEqual(self._qty(item, self.stores), 0)

    def test_a_failed_delete_restores_the_batched_reconciliation_its_bundles_and_batch(self):
        from frappe_assistant_core.tests.test_import_undo import refusing

        session, recon, batch, item = self._batched()
        before = self._bundles(recon)

        with patch.object(import_undo, "_frozen_till", return_value={}), patch.object(
            import_undo, "_closed_till", return_value={}
        ), patch.object(import_undo.frappe, "delete_doc", side_effect=refusing(recon)):
            import_undo.start(self._doc(session), "all")

        self.assertEqual(frappe.db.get_value("Stock Reconciliation", recon, "docstatus"), 1)
        self.assertEqual(self._bundles(recon), before)
        self.assertTrue(frappe.db.exists("Batch", batch))
        self.assertEqual(self._qty(item, self.stores), 3)
        doc = self._doc(session)
        result = (frappe.parse_json(doc.template_options) or {})["undo"]["result"]
        self.assertEqual(
            [(b["record"], b["reason"]) for b in result["blocked"]],
            [(f"Stock Reconciliation {recon}", "delete_failed")],
        )
        self.assertEqual(result["kept"], 1)  # the batch, which the reconciliation needs
        self.assertEqual(doc.status, "Partially Undone")


class TestReviewFindings(BlockerCase):
    """The code review's findings on undo, each pinned by a test that failed first."""

    def test_a_record_that_fails_to_delete_leaves_no_commit_work_behind(self):
        # A cancel or a delete registers work for the commit (ERPNext's queued GL and repost
        # jobs, realtime events); when the record is then refused and rolled back, that work
        # must go with it, or it runs at the batch's commit for a record still there.
        session = self._session_with_customers()
        n = self._names()
        queued = []

        def refuse(doctype=None, name=None, *args, **kwargs):
            for manager in (frappe.db.before_commit, frappe.db.after_commit):
                callback = lambda: None  # noqa: E731
                queued.append(callback)
                manager.add(callback)
            raise frappe.ValidationError("Refused after queuing commit work")

        with patch.object(import_undo.frappe, "delete_doc", side_effect=refuse):
            error = import_undo._remove(self._doc(session), import_undo.Record("Customer", n["alpha"]))

        self.assertIn("Refused after queuing commit work", error)
        left = list(frappe.db.before_commit._functions) + list(frappe.db.after_commit._functions)
        self.assertEqual(len(queued), 2)
        self.assertFalse([c for c in queued if c in left])
        self.assertTrue(frappe.db.exists("Customer", n["alpha"]))

    def test_a_linking_document_the_user_cant_read_is_not_named(self):
        session, names = self._session_with_groups(1)
        hidden = self._outside_customer(names[0])
        allowed = (
            frappe.get_doc(
                {"doctype": "Customer", "customer_name": f"Allowed {self.tag}", "customer_group": self.group}
            )
            .insert()
            .name
        )
        user = self.make_throwaway_user("undo", roles=("Sales User",))
        # A User Permission is how production keeps a Sales User to some Customers only.
        frappe.get_doc(
            {"doctype": "User Permission", "user": user, "allow": "Customer", "for_value": allowed}
        ).insert(ignore_permissions=True)
        frappe.set_user(user)
        self.assertFalse(frappe.has_permission("Customer", "read", doc=hidden))

        blocker = self._blocked(self._plan(session))[("Customer Group", names[0])]

        self.assertEqual(blocker.reason, "linked")
        self.assertNotIn(hidden, blocker.detail)
        self.assertIsNone(blocker.blocked_by)

    def test_a_readable_linking_document_is_still_named(self):
        session, names = self._session_with_groups(1)
        outside = self._outside_customer(names[0])

        blocker = self._blocked(self._plan(session))[("Customer Group", names[0])]

        self.assertEqual(blocker.blocked_by, f"Customer {outside}")

    def test_previewing_an_undo_leaves_the_last_check_standing(self):
        # preview_import_undo keeps the plan's fingerprint in template_options; the file, its
        # mapping and its fixes are unchanged, so the session is still checked as it is.
        session, _checked = self._ready_session()
        doc = self._doc(session)
        from frappe_assistant_core.utils import import_fixes

        self.assertTrue(import_fixes.checked_as_is(doc))

        import_undo.remember_fingerprint(doc, "a plan the user was shown")

        self.assertTrue(import_fixes.checked_as_is(self._doc(session)))
