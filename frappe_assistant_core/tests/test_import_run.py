# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Import through Frappe's Data Import with one approval (#33993): a session whose file passed the
dry run is imported as a Data Import, from a cleaned copy of the file, with one approval card;
it ends with created, failed and skipped counts, shows in Desk like any import, and an
interrupted import continues without duplicates.

Customer again, as in the dry run's tests. The file has a value mapped ("Retial" to the
existing group) and a row left out, so the cleaned file is not the user's file.
"""

import io
import json
import os
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe import _
from frappe.core.doctype.data_import.importer import Importer
from rq.timeouts import JobTimeoutException

from frappe_assistant_core.core.tool_registry import get_tool_registry
from frappe_assistant_core.plugins.core import import_run
from frappe_assistant_core.tests.base_test import BaseAssistantTest

MAPPING = {
    "Customer Name": "customer_name",
    "Customer Group": "customer_group",
    "Customer Type": "customer_type",
}
INSERT = "Insert New Records"


class WorkerStopped(BaseException):
    """A worker killed mid-import: not an Exception, so the importer can't catch it as a row's."""


@unittest.skipUnless("erpnext" in frappe.get_installed_apps(), "needs ERPNext's Customer")
class TestImportRun(BaseAssistantTest):
    def setUp(self):
        super().setUp()
        frappe.set_user("Administrator")
        self.registry = get_tool_registry()
        self.files_before = set(frappe.get_all("File", pluck="name"))
        self.tag = frappe.generate_hash(length=6)
        self.group = f"Imported {self.tag}"
        frappe.get_doc(
            {
                "doctype": "Customer Group",
                "customer_group_name": self.group,
                "parent_customer_group": "All Customer Groups",
            }
        ).insert()

    def tearDown(self):
        frappe.set_user("Administrator")
        # The transaction rollback removes File rows but not the files on disk.
        for name in set(frappe.get_all("File", pluck="name")) - self.files_before:
            frappe.delete_doc("File", name, force=True, ignore_permissions=True)
        super().tearDown()

    # -- helpers -----------------------------------------------------------------------

    def _run(self, tool_name, arguments):
        return self.registry.get_tool(tool_name)._safe_execute(arguments)

    def _names(self):
        t = self.tag
        return {"alpha": f"Alpha {t}", "beta": f"Beta {t}", "gamma": f"Gamma {t}", "delta": f"Delta {t}"}

    def _ready_session(self, file_doc=None, sheet=None):
        """A session whose file passed the dry run: a value mapped and a row left out."""
        n = self._names()
        if not file_doc:
            lines = [",".join(MAPPING)]
            lines += [
                f"{n['alpha']},{self.group},Company",  # row 2
                f"{n['beta']},Nowhere {self.tag},Company",  # row 3: left out
                f"{n['gamma']},Retial {self.tag},Company",  # row 4: mapped to the group
                f"{n['delta']},{self.group},Company",  # row 5
            ]
            file_doc = self._file("customers.csv", ("\n".join(lines) + "\n").encode())
        arguments = {"file_url": file_doc.file_url}
        if sheet:
            arguments["sheet"] = sheet
        session = self._run("start_import_session", arguments)["result"]["session_id"]
        mapped = self._run(
            "set_column_mapping", {"session": session, "mapping": MAPPING, "doctype": "Customer"}
        )
        self.assertTrue(mapped["success"], mapped)
        checked = self._run("dry_run_import", {"session": session})["result"]
        groups = {g["value"]: g["id"] for g in checked["groups"] if g.get("value")}
        fixes = []
        if f"Retial {self.tag}" in groups:
            fixes.append({"group": groups[f"Retial {self.tag}"], "action": "map", "to": self.group})
        if f"Nowhere {self.tag}" in groups:
            fixes.append(
                {"group": groups[f"Nowhere {self.tag}"], "action": "leave_out", "reason": "Closed shop"}
            )
        if fixes:
            checked = self._run("dry_run_import", {"session": session, "fixes": fixes})["result"]
        self.assertEqual(checked["status"], "Ready to Import", checked)
        return session, checked

    def _file(self, name, content):
        return frappe.get_doc(
            {"doctype": "File", "file_name": name, "content": content, "is_private": 1}
        ).insert()

    def _card(self, session, checked, **changes):
        """What FACO passes, and the approval card shows."""
        return {
            "session": session,
            "doctype": "Customer",
            "rows": checked["ready"],
            "import_type": INSERT,
            **changes,
        }

    def _import(self, session, checked, **changes):
        return self._run("start_import", self._card(session, checked, **changes))

    def _doc(self, session):
        return frappe.get_doc("FAC Import Session", session)

    def _imported(self):
        return set(
            frappe.get_all(
                "Customer", filters={"customer_name": ["like", f"% {self.tag}"]}, pluck="customer_name"
            )
        )

    @contextmanager
    def _each_row_commits(self):
        """In production the importer commits each record and rolls back only a failed one. A
        test can't commit, so a savepoint taken at each commit stands in for it."""
        real_rollback = frappe.db.rollback

        def commit(*args, **kwargs):
            frappe.db.savepoint("fac_test_row")

        def rollback(*, save_point=None):
            real_rollback(save_point=save_point or "fac_test_row")

        frappe.db.savepoint("fac_test_row")
        with patch.object(frappe.db, "commit", side_effect=commit), patch.object(
            frappe.db, "rollback", side_effect=rollback
        ):
            yield

    # -- registration ------------------------------------------------------------------

    def test_importing_is_a_write_tool_so_it_asks_for_approval(self):
        from frappe_assistant_core.utils.tool_category_detector import detect_tool_category

        tool = self.registry.get_tool("start_import")

        self.assertEqual(detect_tool_category(tool), "write")
        self.assertEqual(tool.inputSchema["required"], ["session", "doctype", "rows", "import_type"])

    # -- criterion 1: one approval card with the doctype, the rows, new or updates ----------

    def test_the_card_must_say_what_was_checked(self):
        session, checked = self._ready_session()

        for wrong in (
            {"rows": checked["ready"] + 1},
            {"doctype": "Supplier"},
            {"import_type": "Update Existing Records"},
        ):
            response = self._import(session, checked, **wrong)
            self.assertFalse(response["success"], (wrong, response))
            self.assertIn("last check", response["result"]["error"])

        self.assertFalse(self._doc(session).data_imports)
        self.assertEqual(self._imported(), set())

    def test_a_file_that_has_not_passed_the_dry_run_is_not_imported(self):
        session, checked = self._ready_session()
        self._run("set_column_mapping", {"session": session, "mapping": MAPPING, "doctype": "Customer"})

        response = self._import(session, checked)

        self.assertFalse(response["success"], response)
        self.assertIn("dry_run_import", response["result"]["error"])
        self.assertEqual(self._imported(), set())

    def test_a_session_changed_since_its_check_is_not_imported(self):
        session, checked = self._ready_session()
        # Edited after the check (update_document can), but not checked again.
        self._doc(session).db_set("excluded_rows", "[2, 3]")

        response = self._import(session, checked)

        self.assertFalse(response["success"], response)
        self.assertEqual(self._imported(), set())

    def test_rows_erpnext_would_refuse_now_stop_the_import_before_it_starts(self):
        session, checked = self._ready_session()
        # The group the rows use was deleted after the check.
        frappe.delete_doc("Customer Group", self.group, force=True)

        response = self._import(session, checked)

        self.assertFalse(response["success"], response)
        self.assertIn("dry_run_import", response["result"]["error"])
        self.assertFalse(self._doc(session).data_imports)

    # -- criteria 2 and 3: it runs, ends with the counts, and shows in Desk ------------------

    def test_an_approved_import_runs_as_a_data_import_and_ends_with_the_counts(self):
        session, checked = self._ready_session()
        n = self._names()

        response = self._import(session, checked)

        self.assertTrue(response["success"], response)
        result = response["result"]
        self.assertEqual(self._imported(), {n["alpha"], n["gamma"], n["delta"]})
        self.assertEqual(
            frappe.db.get_value("Customer", {"customer_name": n["gamma"]}, "customer_group"), self.group
        )
        self.assertEqual((result["created"], result["failed"], result["skipped"]), (3, 0, 1))
        self.assertIn("Imported 3 Customer records", result["message"])
        self.assertIn("1 row left out (Closed shop: row 3)", result["message"])
        doc = self._doc(session)
        self.assertEqual(doc.status, "Completed")
        link = doc.data_imports[-1]
        self.assertEqual(
            (link.data_import, link.purpose, link.success_count, link.failed_count),
            (result["data_import"], "Import", 3, 0),
        )
        self.assertEqual(doc.steps[-1].step, "Import")

    def test_the_data_import_is_like_any_other_in_desk(self):
        session, checked = self._ready_session()

        data_import = self._import(session, checked)["result"]["data_import"]

        di = frappe.get_doc("Data Import", data_import)
        self.assertEqual(
            (di.reference_doctype, di.import_type, di.status, di.mute_emails),
            ("Customer", INSERT, "Success", 1),
        )
        self.assertEqual(
            json.loads(di.template_options)["column_to_field_map"],
            {"0": "customer_name", "1": "customer_group", "2": "customer_type"},
        )
        self.assertEqual(frappe.db.count("File", {"file_url": di.import_file}), 1)  # one file, the import's
        file_doc = frappe.get_doc("File", {"file_url": di.import_file})
        self.assertTrue(file_doc.is_private)
        self.assertEqual(
            (file_doc.attached_to_doctype, file_doc.attached_to_name), ("Data Import", data_import)
        )
        self.assertEqual(frappe.db.count("Data Import Log", {"data_import": data_import, "success": 1}), 3)

    def test_a_data_import_that_cannot_be_saved_leaves_no_file_behind(self):
        from frappe.core.doctype.data_import.data_import import DataImport

        session, checked = self._ready_session()
        files_before = set(frappe.get_all("File", pluck="name"))

        with patch.object(
            DataImport, "validate", autospec=True, side_effect=frappe.ValidationError("refused")
        ):
            response = self._import(session, checked)

        self.assertFalse(response["success"], response)
        self.assertEqual(set(frappe.get_all("File", pluck="name")), files_before)
        self.assertEqual(self._doc(session).status, "Ready to Import")

    def test_the_cleaned_file_keeps_the_users_row_numbers(self):
        session, checked = self._ready_session()

        data_import = self._import(session, checked)["result"]["data_import"]

        content = frappe.get_doc(
            "File", {"file_url": frappe.db.get_value("Data Import", data_import, "import_file")}
        ).get_content()
        lines = (content.decode() if isinstance(content, bytes) else content).splitlines()
        self.assertEqual(lines[2], ",,")  # row 3, left out, is blank
        self.assertIn(self.group, lines[3])  # row 4, "Retial" mapped
        logged = {
            json.loads(log.row_indexes)[0]
            for log in frappe.get_all(
                "Data Import Log", filters={"data_import": data_import}, fields=["row_indexes"]
            )
        }
        self.assertEqual(logged, {2, 4, 5})

    def test_the_sheet_the_user_chose_is_imported_not_the_first(self):
        openpyxl = __import__("openpyxl")
        n = self._names()
        book = openpyxl.Workbook()
        book.active.title = "Notes"
        book.active.append(["Anything"])
        book.active.append(["not customers"])
        sheet = book.create_sheet("Customers")
        sheet.append(list(MAPPING))
        sheet.append([n["alpha"], self.group, "Company"])
        buffer = io.BytesIO()
        book.save(buffer)
        file_doc = self._file("customers.xlsx", buffer.getvalue())
        session, checked = self._ready_session(file_doc, sheet="Customers")

        response = self._import(session, checked)

        self.assertTrue(response["success"], response)
        self.assertEqual(self._imported(), {n["alpha"]})

    def test_records_erpnext_refuses_while_importing_are_a_partial_success_with_their_rows(self):
        session, checked = self._ready_session()
        n = self._names()
        real_process = Importer.process_doc

        def on_hold(importer, doc):
            # The first record too: its rollback must not undo the Data Import and the session.
            if doc.get("customer_name") in (n["alpha"], n["gamma"]):
                frappe.throw(_("Customer is on hold"))
            return real_process(importer, doc)

        with self._each_row_commits(), patch.object(
            Importer, "process_doc", autospec=True, side_effect=on_hold
        ):
            response = self._import(session, checked)

        self.assertTrue(response["success"], response)
        result = response["result"]
        self.assertEqual((result["created"], result["failed"], result["skipped"]), (1, 2, 1))
        self.assertIn("rows 2, 4: Customer is on hold", result["message"])  # the user's row numbers
        self.assertEqual(self._doc(session).status, "Partially Completed")
        self.assertEqual(self._imported(), {n["delta"]})

    # -- criterion 4: an interrupted import continues without duplicates ------------------------

    def test_an_interrupted_import_continues_without_duplicates(self):
        session, checked = self._ready_session()
        n = self._names()
        real_process = Importer.process_doc
        calls = []

        def worker_stops_on_the_second(importer, doc):
            calls.append(doc.get("customer_name"))
            if len(calls) == 2:
                raise WorkerStopped  # a restart: nothing after this line runs, as for a killed worker
            return real_process(importer, doc)

        with self._each_row_commits(), patch.object(
            Importer, "process_doc", autospec=True, side_effect=worker_stops_on_the_second
        ):
            with self.assertRaises(WorkerStopped):
                self._import(session, checked)
        self.assertEqual(self._imported(), {n["alpha"]})
        self.assertEqual(self._doc(session).status, "Importing")

        with self._each_row_commits():
            second = self._import(session, checked)

        self.assertTrue(second["success"], second)
        self.assertEqual(second["result"]["state"], "finished")
        self.assertEqual(self._imported(), {n["alpha"], n["gamma"], n["delta"]})
        self.assertEqual(frappe.db.count("Customer", {"customer_name": n["alpha"]}), 1)
        doc = self._doc(session)
        self.assertEqual(doc.status, "Completed")
        self.assertEqual([d.purpose for d in doc.data_imports], ["Import", "Retry"])
        self.assertEqual(len({d.data_import for d in doc.data_imports}), 1)  # the same Data Import

    def test_a_timed_out_import_says_to_continue(self):
        session, checked = self._ready_session()
        with patch.object(import_run, "enqueue", autospec=True):  # queued, not run yet
            data_import = self._import(session, checked)["result"]["data_import"]
        frappe.db.set_value("Data Import", data_import, "status", "Timed Out")  # what Frappe's job sets

        result = import_run.record_result(session, data_import)

        self.assertIn("continue", result["message"])
        self.assertEqual(self._doc(session).status, "Importing")

    def test_continuing_while_it_still_runs_starts_nothing(self):
        session, checked = self._ready_session()
        with patch.object(import_run, "enqueue", autospec=True):  # queued, not run yet
            self._import(session, checked)

        with patch.object(import_run, "is_job_enqueued", autospec=True, return_value=True), patch.object(
            import_run, "enqueue", autospec=True
        ) as enqueue:
            response = self._import(session, checked)

        self.assertTrue(response["success"], response)
        self.assertEqual(response["result"]["state"], "running")
        enqueue.assert_not_called()

    def test_a_finished_import_is_not_imported_again(self):
        session, checked = self._ready_session()
        self._import(session, checked)

        response = self._import(session, checked)

        self.assertFalse(response["success"], response)
        self.assertIn("already imported", response["result"]["error"])
        self.assertEqual(len(self._doc(session).data_imports), 1)

    def test_a_failed_import_can_be_tried_again(self):
        session, checked = self._ready_session()
        n = self._names()
        real_process = Importer.process_doc

        def all_on_hold(importer, doc):
            frappe.throw(_("Customer is on hold"))

        with self._each_row_commits(), patch.object(
            Importer, "process_doc", autospec=True, side_effect=all_on_hold
        ):
            first = self._import(session, checked)["result"]
        self.assertEqual((first["created"], first["failed"]), (0, 3))
        self.assertEqual(self._doc(session).status, "Failed")

        with self._each_row_commits(), patch.object(
            Importer, "process_doc", autospec=True, side_effect=real_process
        ):
            again = self._import(session, checked)

        self.assertTrue(again["success"], again)
        self.assertEqual(self._imported(), {n["alpha"], n["gamma"], n["delta"]})
        self.assertEqual(self._doc(session).status, "Completed")

    def test_a_retry_queued_in_the_background_is_not_reported_as_the_old_result(self):
        session, checked = self._ready_session()
        n = self._names()
        real_process = Importer.process_doc

        def gamma_on_hold(importer, doc):
            if doc.get("customer_name") == n["gamma"]:
                frappe.throw(_("Customer is on hold"))
            return real_process(importer, doc)

        with self._each_row_commits(), patch.object(
            Importer, "process_doc", autospec=True, side_effect=gamma_on_hold
        ):
            self._import(session, checked)
        self.assertEqual(self._doc(session).status, "Partially Completed")

        with patch.object(import_run, "_runs_now", autospec=True, return_value=False), patch.object(
            import_run, "is_scheduler_inactive", autospec=True, return_value=False
        ), patch.object(import_run, "enqueue", autospec=True):
            response = self._import(session, checked)

        self.assertTrue(response["success"], response)
        self.assertEqual(response["result"]["state"], "continued")
        self.assertIn("in the background", response["result"]["message"])

    def test_a_continue_that_erpnext_refuses_says_why_instead_of_asking_again(self):
        session, checked = self._ready_session()
        real_process = Importer.process_doc
        calls = []

        def worker_stops_on_the_second(importer, doc):
            calls.append(doc)
            if len(calls) == 2:
                raise WorkerStopped
            return real_process(importer, doc)

        with self._each_row_commits(), patch.object(
            Importer, "process_doc", autospec=True, side_effect=worker_stops_on_the_second
        ):
            with self.assertRaises(WorkerStopped):
                self._import(session, checked)
        # The group the remaining rows use is deleted before the user asks to continue.
        frappe.delete_doc("Customer Group", self.group, force=True)

        with self._each_row_commits():
            response = self._import(session, checked)

        result = response["result"]
        self.assertEqual(result["state"], "refused")
        self.assertIn("can't continue", result["message"])
        self.assertEqual(self._doc(session).status, "Importing")

    def test_a_record_spread_over_rows_is_left_out_whole(self):
        # A Customer with two Sales Team rows: row 3 continues the record on row 2.
        n = self._names()
        people = []
        for i in (1, 2):
            people.append(
                frappe.get_doc(
                    {
                        "doctype": "Sales Person",
                        "sales_person_name": f"Seller {i} {self.tag}",
                        "parent_sales_person": "Sales Team",
                    }
                )
                .insert()
                .name
            )
        mapping = {
            **MAPPING,
            "Sales Person": "sales_team.sales_person",
            "Contribution": "sales_team.allocated_percentage",
        }
        lines = [",".join(mapping)]
        lines += [
            f"{n['alpha']},{self.group},Company,{people[0]},50",
            f",,,{people[1]},50",
            f"{n['delta']},{self.group},Company,,",
        ]
        file_doc = self._file("customers-team.csv", ("\n".join(lines) + "\n").encode())
        session = self._run("start_import_session", {"file_url": file_doc.file_url})["result"]["session_id"]
        self.assertTrue(
            self._run("set_column_mapping", {"session": session, "mapping": mapping, "doctype": "Customer"})[
                "success"
            ]
        )
        # A remap after a leave-out can leave only some of a record's rows in excluded_rows.
        self._doc(session).db_set("excluded_rows", "[2]")
        checked = self._run("dry_run_import", {"session": session})["result"]
        self.assertEqual((checked["status"], checked["ready"]), ("Ready to Import", 1), checked)

        response = self._import(session, checked)

        self.assertTrue(response["success"], response)
        self.assertEqual((response["result"]["created"], response["result"]["failed"]), (1, 0))
        self.assertEqual(self._imported(), {n["delta"]})
        self.assertFalse(frappe.get_doc("Customer", {"customer_name": n["delta"]}).sales_team)

    def test_failed_counts_are_records_like_created(self):
        logs = [
            frappe._dict(row_indexes="[2, 3]", messages='[{"message": "On hold"}]'),
            frappe._dict(row_indexes="[4, 5]", messages='[{"message": "On hold"}]'),
        ]

        groups = import_run._failed_by_reason(logs)

        self.assertEqual(groups, [{"reason": "On hold", "count": 2, "rows": [2, 3, 4, 5]}])

    def test_an_imported_session_made_ready_again_is_not_imported_twice(self):
        session, checked = self._ready_session()
        self._import(session, checked)
        # A remap and a new check can bring an imported session back to Ready to Import.
        self._doc(session).db_set("status", "Ready to Import")

        response = self._import(session, checked)

        self.assertFalse(response["success"], response)
        self.assertIn("already imported", response["result"]["error"])
        self.assertEqual(len(self._doc(session).data_imports), 1)
        self.assertEqual(frappe.db.count("Customer", {"customer_name": self._names()["alpha"]}), 1)

    def test_a_start_that_fails_half_way_leaves_no_data_import_behind(self):
        session, checked = self._ready_session()
        imports_before = frappe.db.count("Data Import")
        files_before = set(frappe.get_all("File", pluck="name"))

        with patch.object(
            import_run, "_record_start", autospec=True, side_effect=frappe.TimestampMismatchError
        ):
            response = self._import(session, checked)

        self.assertFalse(response["success"], response)
        self.assertEqual(frappe.db.count("Data Import"), imports_before)
        self.assertEqual(set(frappe.get_all("File", pluck="name")), files_before)
        # The rollback takes the File row; its content on disk goes too.
        self.assertFalse(os.path.exists(frappe.get_site_path("private", "files", f"{session}-import.csv")))

    def test_an_import_run_to_its_end_elsewhere_is_recorded_not_run_again(self):
        from frappe.core.doctype.data_import.data_import import start_import

        session, checked = self._ready_session()
        with patch.object(import_run, "enqueue", autospec=True):  # queued, not run by us
            data_import = self._import(session, checked)["result"]["data_import"]
        start_import(data_import)  # run from Desk's Start Import instead

        response = self._import(session, checked)

        self.assertTrue(response["success"], response)
        self.assertEqual((response["result"]["state"], response["result"]["created"]), ("finished", 3))
        doc = self._doc(session)
        self.assertEqual(doc.status, "Completed")
        self.assertEqual([d.purpose for d in doc.data_imports], ["Import"])

    def test_a_partial_import_run_to_its_end_elsewhere_is_recorded_not_tried_again(self):
        from frappe.core.doctype.data_import.data_import import start_import

        session, checked = self._ready_session()
        n = self._names()
        real_process = Importer.process_doc

        def gamma_on_hold(importer, doc):
            if doc.get("customer_name") == n["gamma"]:
                frappe.throw(_("Customer is on hold"))
            return real_process(importer, doc)

        with patch.object(import_run, "enqueue", autospec=True):  # queued, not run by us
            data_import = self._import(session, checked)["result"]["data_import"]
        with self._each_row_commits(), patch.object(
            Importer, "process_doc", autospec=True, side_effect=gamma_on_hold
        ):
            start_import(data_import)  # run to its end from Desk; the session still says Importing

        response = self._import(session, checked)

        self.assertEqual((response["result"]["state"], response["result"]["failed"]), ("finished", 1))
        doc = self._doc(session)
        self.assertEqual(doc.status, "Partially Completed")
        self.assertEqual([d.purpose for d in doc.data_imports], ["Import"])  # recorded, not run again

    # -- who can import, and when it can't start ----------------------------------------------

    def test_a_user_who_cannot_use_data_import_is_told_before_anything_is_written(self):
        # May create and import Customers, but Data Import is for System Managers.
        frappe.set_user(self.make_throwaway_user("seller", roles=("Sales Master Manager",)))

        self.assertIn("Data Import", import_run.cannot_import("Customer"))

    def test_without_the_scheduler_the_import_is_refused(self):
        with patch.object(import_run, "_runs_now", autospec=True, return_value=False), patch.object(
            import_run, "is_scheduler_inactive", autospec=True, return_value=True
        ):
            self.assertIn("scheduler", import_run.cannot_import("Customer"))

    def test_the_job_waits_for_the_data_import_to_be_saved(self):
        session, checked = self._ready_session()

        with patch.object(import_run, "_runs_now", autospec=True, return_value=False), patch.object(
            import_run, "is_scheduler_inactive", autospec=True, return_value=False
        ), patch.object(import_run, "enqueue", autospec=True) as enqueue:
            response = self._import(session, checked)

        self.assertTrue(response["success"], response)
        self.assertEqual(response["result"]["state"], "started")
        kwargs = enqueue.call_args.kwargs
        self.assertTrue(kwargs["enqueue_after_commit"])
        self.assertEqual(kwargs["job_id"], f"data_import::{response['result']['data_import']}")

    def test_another_users_session_is_refused(self):
        session, checked = self._ready_session()
        frappe.set_user(self.make_throwaway_user("other", roles=("Sales Manager",)))

        response = self._import(session, checked)

        self.assertFalse(response["success"])
        frappe.set_user("Administrator")
        self.assertEqual(self._imported(), set())
