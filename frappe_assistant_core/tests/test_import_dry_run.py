# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
dry_run_import (#33990): every row checked exactly as Data Import would import it,
nothing written, failures grouped by cause.

Customer is the target because it carries every kind of failure the story names: a Link
(Customer Group), a Select (Customer Type), a required, naming field (Customer Name) and a
controller rule the import preview never runs ("Cannot select a Group type Customer
Group", raised when the group is left empty and the default group is a group).
"""

import json
import unittest
from unittest.mock import patch

import frappe

from frappe_assistant_core.core.tool_registry import get_tool_registry
from frappe_assistant_core.plugins.core import import_dry_run
from frappe_assistant_core.tests.base_test import BaseAssistantTest

GROUP = "FAC Dry Run Group"
MAPPING = {
    "Customer Name": "customer_name",
    "Customer Group": "customer_group",
    "Customer Type": "customer_type",
    "Notes": "Don't Import",
}


@unittest.skipUnless("erpnext" in frappe.get_installed_apps(), "needs ERPNext's Customer")
class TestDryRunImport(BaseAssistantTest):
    def setUp(self):
        super().setUp()
        frappe.set_user("Administrator")
        self.registry = get_tool_registry()
        self.files_before = set(frappe.get_all("File", pluck="name"))
        self.tag = frappe.generate_hash(length=6)
        # A misspelt group of this test's own: one test creates it to show a fix.
        self.typo = f"Retial {self.tag}"
        if not frappe.db.exists("Customer Group", GROUP):
            frappe.get_doc(
                {
                    "doctype": "Customer Group",
                    "customer_group_name": GROUP,
                    "parent_customer_group": "All Customer Groups",
                    "is_group": 0,
                }
            ).insert()
        self.existing = f"Existing {self.tag}"
        frappe.get_doc(
            {"doctype": "Customer", "customer_name": self.existing, "customer_group": GROUP}
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

    def _csv(self, rows):
        lines = ["Customer Name,Customer Group,Customer Type,Notes"]
        lines += [",".join(row) for row in rows]
        return ("\n".join(lines) + "\n").encode()

    def _session(self, rows, mapping=MAPPING):
        """Upload a CSV, open its session and map it, as the earlier steps do."""
        file_doc = frappe.get_doc(
            {"doctype": "File", "file_name": "customers.csv", "content": self._csv(rows), "is_private": 1}
        ).insert()
        started = self._run("start_import_session", {"file_url": file_doc.file_url})
        self.assertTrue(started["success"], started)
        session = started["result"]["session_id"]
        mapped = self._run(
            "set_column_mapping", {"session": session, "mapping": mapping, "doctype": "Customer"}
        )
        self.assertTrue(mapped["success"], mapped)
        return session

    def _check(self, session, **arguments):
        response = self._run("dry_run_import", {"session": session, **arguments})
        self.assertTrue(response["success"], response)
        return response["result"]

    def _check_fails(self, session, expected_text, **arguments):
        response = self._run("dry_run_import", {"session": session, **arguments})
        self.assertFalse(response["success"], response)
        self.assertIn(expected_text, response["error"])
        return response

    def _mixed_rows(self):
        t = self.tag
        return [
            [f"Alpha {t}", GROUP, "Company", "fine"],  # row 2: ready
            [f"Beta {t}", self.typo, "Company", ""],  # row 3: missing link
            [f"Gamma {t}", self.typo, "Company", ""],  # row 4: missing link, same group
            [f"Delta {t}", "", "Company", ""],  # row 5: controller rule
            [f"Alpha {t}", GROUP, "Company", ""],  # row 6: duplicate of row 2
            [self.existing, GROUP, "Company", ""],  # row 7: already in ERPNext
            [f"Eps {t}", GROUP, "Robot", ""],  # row 8: not an allowed option
        ]

    @staticmethod
    def _group(result, cause):
        return next(g for g in result["groups"] if g["cause"] == cause)

    # -- registration ------------------------------------------------------------------

    def test_tool_is_registered_and_raises_no_approval_card(self):
        from frappe_assistant_core.plugins.core.plugin import CorePlugin
        from frappe_assistant_core.utils.tool_category_detector import detect_tool_category

        self.assertIn("dry_run_import", CorePlugin().get_tools())
        self.assertEqual(detect_tool_category(self.registry.get_tool("dry_run_import")), "read_only")

    # -- criterion 1 and 2: every row checked, grouped summary, nothing written ------------

    def test_every_row_is_checked_and_failures_are_grouped_by_cause(self):
        session = self._session(self._mixed_rows())

        result = self._check(session)

        self.assertEqual((result["total_rows"], result["ready"], result["failed"]), (7, 1, 6))
        self.assertEqual(result["message"], "1 rows ready, 6 would fail. Nothing was written.")
        link = self._group(result, "Missing link")
        self.assertEqual((link["field"], link["value"], link["count"]), ("customer_group", self.typo, 2))
        self.assertEqual([e["row"] for e in link["examples"]], [3, 4])
        self.assertEqual(self._group(result, "Not an allowed option")["value"], "Robot")

    def test_controller_rules_the_preview_misses_are_caught(self):
        session = self._session(self._mixed_rows())

        rejected = self._group(self._check(session), "Rejected by ERPNext")

        self.assertEqual(rejected["error"], "ValidationError")
        self.assertIn("Group type Customer Group", rejected["message"])
        self.assertEqual([e["row"] for e in rejected["examples"]], [5])

    def test_nothing_is_written(self):
        session = self._session(self._mixed_rows())
        before = {dt: frappe.db.count(dt) for dt in ("Customer", "Contact", "Email Queue", "Version")}

        self._check(session)

        self.assertEqual({dt: frappe.db.count(dt) for dt in before}, before)
        self.assertFalse(frappe.db.exists("Customer", {"customer_name": f"Alpha {self.tag}"}))

    def test_a_group_shows_at_most_three_example_rows(self):
        rows = [[f"Row {i} {self.tag}", self.typo, "Company", ""] for i in range(5)]
        session = self._session(rows)

        link = self._group(self._check(session), "Missing link")

        self.assertEqual(link["count"], 5)
        self.assertEqual(len(link["examples"]), 3)
        self.assertEqual(link["examples"][0]["values"]["Customer Group"], self.typo)
        self.assertNotIn("Notes", link["examples"][0]["values"])  # not imported, not shown

    # -- criterion 3: duplicates are their own groups ---------------------------------------

    def test_duplicates_in_the_file_and_in_erpnext_are_their_own_groups(self):
        session = self._session(self._mixed_rows())

        result = self._check(session)

        in_file = self._group(result, "Duplicate in the file")
        self.assertEqual(([e["row"] for e in in_file["examples"]], in_file["field"]), ([6], "customer_name"))
        existing = self._group(result, "Already in ERPNext")
        self.assertEqual([e["row"] for e in existing["examples"]], [7])

    # -- the session holds the result ----------------------------------------------------------

    def test_the_session_holds_the_summary_the_full_list_and_a_step(self):
        session = self._session(self._mixed_rows())

        self._check(session)

        doc = frappe.get_doc("FAC Import Session", session)
        self.assertEqual(doc.status, "Dry Run Failed")
        summary = json.loads(doc.validation_summary)
        self.assertEqual((summary["ready"], summary["failed"]), (1, 6))
        results = frappe.get_doc("File", {"file_url": doc.validation_results})
        self.assertTrue(results.is_private)
        self.assertEqual(
            (results.attached_to_doctype, results.attached_to_name), ("FAC Import Session", session)
        )
        per_row = {r["row"]: r["status"] for r in json.loads(results.get_content())["rows"]}
        self.assertEqual(
            per_row,
            {2: "ready", 3: "failed", 4: "failed", 5: "failed", 6: "failed", 7: "failed", 8: "failed"},
        )
        step = doc.steps[-1]
        self.assertEqual(
            (step.step, step.outcome, step.rows_ok, step.rows_failed), ("Dry Run", "Warning", 1, 6)
        )
        self.assertTrue(doc.validated_fingerprint)

    def test_a_clean_file_is_ready_to_import(self):
        session = self._session([[f"Clean {self.tag}", GROUP, "Company", ""]])

        result = self._check(session)

        self.assertEqual((result["ready"], result["failed"], result["status"]), (1, 0, "Ready to Import"))
        self.assertEqual(frappe.get_doc("FAC Import Session", session).steps[-1].outcome, "Success")

    def test_the_session_keeps_no_row_of_the_file(self):
        session = self._session(self._mixed_rows())

        self._check(session)

        stored = frappe.as_json(frappe.get_doc("FAC Import Session", session).as_dict())
        self.assertNotIn(f"Beta {self.tag}", stored)

    # -- criterion 5: checking again reports only what still fails ------------------------------

    def test_checking_again_after_a_fix_reports_only_remaining_failures(self):
        session = self._session(self._mixed_rows())
        self._check(session)
        frappe.get_doc(
            {
                "doctype": "Customer Group",
                "customer_group_name": self.typo,
                "parent_customer_group": "All Customer Groups",
            }
        ).insert()

        result = self._check(session)

        self.assertEqual((result["ready"], result["failed"]), (3, 4))
        self.assertNotIn("Missing link", [g["cause"] for g in result["groups"]])
        self.assertEqual(result["previous"], {"failed": 6, "fixed": 2})
        attached = frappe.get_all(
            "File", filters={"attached_to_doctype": "FAC Import Session", "attached_to_name": session}
        )
        self.assertEqual(len(attached), 1)  # the old result file is replaced, not kept

    def test_rows_left_out_are_not_checked(self):
        session = self._session(self._mixed_rows())
        frappe.db.set_value("FAC Import Session", session, "excluded_rows", json.dumps([3, 4]))

        result = self._check(session)

        self.assertEqual((result["ready"], result["failed"], result["excluded"]), (1, 4, 2))

    # -- criterion 4: large files run in the background -------------------------------------------

    def test_a_large_file_is_checked_in_the_background(self):
        session = self._session(self._mixed_rows())

        with patch.object(import_dry_run, "BACKGROUND_ROWS", 5), patch("frappe.enqueue") as enqueue:
            started = self._check(session)
            again = self._check(session)

        self.assertEqual((started["state"], started["total_rows"]), ("running", 7))
        self.assertEqual(again["state"], "running")
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.kwargs["session_name"], session)
        self.assertEqual(
            enqueue.call_args.args[0], "frappe_assistant_core.plugins.core.import_dry_run.run_job"
        )
        progress = json.loads(frappe.db.get_value("FAC Import Session", session, "progress"))
        self.assertEqual((progress["state"], progress["total"]), ("queued", 7))

        import_dry_run.run_job(session, "Administrator")

        doc = frappe.get_doc("FAC Import Session", session)
        self.assertEqual(doc.status, "Dry Run Failed")
        self.assertEqual(json.loads(doc.progress)["state"], "done")
        self.assertEqual(json.loads(doc.validation_summary)["failed"], 6)

    # -- side effects are held back ------------------------------------------------------------------

    def test_held_back_swallows_commits_jobs_and_events_then_restores_them(self):
        frappe.db.after_commit.add(print)
        with (
            patch.object(frappe.db.__class__, "commit") as real_commit,
            patch("frappe.enqueue") as real_enqueue,
            patch("frappe.utils.background_jobs.enqueue") as real_bg_enqueue,
            patch("frappe.publish_realtime") as real_publish,
        ):
            with import_dry_run.held_back():
                self.assertTrue(frappe.flags.mute_emails and frappe.flags.in_import)
                frappe.enqueue("frappe.ping")
                frappe.utils.background_jobs.enqueue("frappe.ping")
                frappe.publish_realtime("x")
                frappe.db.commit()
                frappe.db.after_commit.add(len)  # a row's callback must not outlive the row

            for real in (real_commit, real_enqueue, real_bg_enqueue, real_publish):
                real.assert_not_called()
            # Restored afterwards.
            frappe.db.commit()
            frappe.enqueue("frappe.ping")
            real_commit.assert_called_once()
            real_enqueue.assert_called_once()

        self.assertEqual(list(frappe.db.after_commit._functions)[-1], print)
        frappe.db.after_commit._functions.pop()
        self.assertFalse(frappe.flags.in_import)

    # -- when it can't run ---------------------------------------------------------------------------

    def test_an_unmapped_session_is_refused(self):
        file_doc = frappe.get_doc(
            {
                "doctype": "File",
                "file_name": "c.csv",
                "content": self._csv([["A", GROUP, "Company", ""]]),
                "is_private": 1,
            }
        ).insert()
        session = self._run("start_import_session", {"file_url": file_doc.file_url})["result"]["session_id"]

        self._check_fails(session, "Map the columns first")

    def test_a_file_only_in_the_chat_asks_for_the_file_then_continues_with_it(self):
        started = self._run(
            "start_import_session",
            {"file_name": "customers.csv", "columns": list(MAPPING), "row_count": 1},
        )
        session = started["result"]["session_id"]
        self._run("set_column_mapping", {"session": session, "mapping": MAPPING, "doctype": "Customer"})

        self._check_fails(session, "Attach it in FAC Chat")

        file_doc = frappe.get_doc(
            {
                "doctype": "File",
                "file_name": "customers.csv",
                "content": self._csv([[f"Chat {self.tag}", GROUP, "Company", ""]]),
                "is_private": 1,
            }
        ).insert()
        result = self._check(session, file_url=file_doc.file_url)
        self.assertEqual(result["ready"], 1)
        doc = frappe.get_doc("FAC Import Session", session)
        self.assertEqual((doc.file_source, doc.source_file), ("Site File", file_doc.name))

    def test_a_file_with_other_columns_is_not_linked(self):
        started = self._run(
            "start_import_session", {"file_name": "customers.csv", "columns": list(MAPPING), "row_count": 1}
        )
        session = started["result"]["session_id"]
        self._run("set_column_mapping", {"session": session, "mapping": MAPPING, "doctype": "Customer"})
        other = frappe.get_doc(
            {"doctype": "File", "file_name": "other.csv", "content": b"Name\nX\n", "is_private": 1}
        ).insert()

        self._check_fails(session, "columns don't match", file_url=other.file_url)

        self.assertFalse(frappe.db.get_value("FAC Import Session", session, "source_file"))

    def test_a_doctype_whose_side_effects_cant_be_held_back_is_refused(self):
        session = self._session(self._mixed_rows())

        with patch.dict(import_dry_run.UNSAFE_DOCTYPES, {"Customer": "test reason"}):
            self._check_fails(session, "can't be done for Customer")

    def test_another_users_session_is_refused(self):
        session = self._session(self._mixed_rows())
        frappe.set_user(self.make_throwaway_user("other", roles=("Blogger",)))

        response = self._run("dry_run_import", {"session": session})

        self.assertFalse(response["success"])
        self.assertFalse(frappe.db.get_value("FAC Import Session", session, "validation_summary"))
