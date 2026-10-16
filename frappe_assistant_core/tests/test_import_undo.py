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
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, now_datetime

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
            self.assertEqual(import_undo.finished_at(doc), doc.steps[-1].timestamp)
            self.assertEqual(import_undo.window_ends(doc), import_undo.finished_at(doc) + timedelta(days=30))
