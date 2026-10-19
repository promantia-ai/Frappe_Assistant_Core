# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
create_document and update_document with several records in one call (documents): one tool
call, so one approval card; every record checked first, then all written or none.

Frappe's own ToDo (anyone may create one; its description goes to global search) and Note
(needs a title), so these run on any site. Single-record calls are covered, unchanged, by
test_document_tools.
"""

import os
from unittest.mock import patch

import frappe

from frappe_assistant_core.core.tool_registry import get_tool_registry
from frappe_assistant_core.plugins.core.tools.create_document import DocumentCreate
from frappe_assistant_core.tests.base_test import BaseAssistantTest
from frappe_assistant_core.utils import document_batch


class TestDocumentBatch(BaseAssistantTest):
    def setUp(self):
        super().setUp()
        frappe.set_user("Administrator")
        self.registry = get_tool_registry()
        self.tag = frappe.generate_hash(length=8)

    def _run(self, tool_name, arguments):
        return self.registry.get_tool(tool_name)._safe_execute(arguments)

    def _todo(self, text):
        return {"doctype": "ToDo", "data": {"description": f"{text} {self.tag}"}}

    def _todos(self):
        return set(
            frappe.get_all("ToDo", filters={"description": ["like", f"% {self.tag}"]}, pluck="description")
        )

    def _notes(self):
        return set(frappe.get_all("Note", filters={"title": ["like", f"% {self.tag}"]}, pluck="title"))

    # -- create_document ---------------------------------------------------------------------

    def test_one_call_creates_records_of_several_doctypes(self):
        response = self._run(
            "create_document",
            {
                "documents": [
                    self._todo("Call the bank"),
                    {"doctype": "Note", "data": {"title": f"Bank details {self.tag}"}},
                    self._todo("Send the invoice"),
                ]
            },
        )

        self.assertTrue(response["success"], response)
        result = response["result"]
        self.assertEqual([c["doctype"] for c in result["created"]], ["ToDo", "Note", "ToDo"])
        self.assertEqual(self._todos(), {f"Call the bank {self.tag}", f"Send the invoice {self.tag}"})
        self.assertEqual(self._notes(), {f"Bank details {self.tag}"})
        self.assertIn("Created 3 records", result["message"])

    def test_one_record_failing_creates_none_and_says_which(self):
        logged = {"method": "Document Creation Error"}
        logs_before = frappe.db.count("Error Log", logged)

        response = self._run(
            "create_document",
            {
                "documents": [
                    self._todo("Call the bank"),
                    {"doctype": "Note", "data": {"content": "no title"}},
                ]
            },
        )

        self.assertFalse(response["success"], response)
        self.assertIn("Document 2 (Note) couldn't be created", response["result"]["error"])
        self.assertIn("Nothing was created", response["result"]["error"])
        self.assertEqual(self._todos(), set())  # the first one is undone
        self.assertEqual(frappe.db.count("Error Log", logged), logs_before + 1)

    def test_every_record_is_checked_before_any_is_written(self):
        response = self._run(
            "create_document",
            {"documents": [self._todo("Fine"), {"doctype": "ToDo"}, {"data": {"description": "x"}}, "oops"]},
        )

        self.assertFalse(response["success"], response)
        self.assertEqual([p["document"] for p in response["result"]["problems"]], [2, 3, 4])
        self.assertEqual(self._todos(), set())

    def test_a_record_the_user_may_not_create_stops_the_list_before_anything_is_written(self):
        frappe.set_user(self.make_throwaway_user("writer", roles=("Blogger",)))

        response = self._run(
            "create_document",
            {
                "documents": [
                    self._todo("Mine"),
                    {"doctype": "Role", "data": {"role_name": f"Boss {self.tag}"}},
                    {"doctype": "Role", "data": {"role_name": f"Chief {self.tag}"}},
                ]
            },
        )

        self.assertFalse(response["success"], response)
        # Checked before anything is written, so every refused record is listed, not the first.
        self.assertEqual([p["document"] for p in response["result"]["problems"]], [2, 3])
        frappe.set_user("Administrator")
        self.assertEqual(self._todos(), set())
        self.assertFalse(frappe.db.exists("Role", f"Boss {self.tag}"))

    def test_a_restricted_field_in_a_list_is_refused(self):
        response = self._run(
            "create_document",
            {
                "documents": [
                    {"doctype": "ToDo", "data": {"description": f"x {self.tag}", "api_key": "k"}},
                    {"doctype": "ToDo", "data": {"description": f"y {self.tag}", "api_secret": "s"}},
                ]
            },
        )

        self.assertFalse(response["success"], response)
        problems = response["result"]["problems"]
        self.assertEqual([p["document"] for p in problems], [1, 2])  # both listed, before writing
        self.assertIn("api_key", problems[0]["error"])
        self.assertEqual(self._todos(), set())

    def test_more_than_a_hundred_records_are_refused(self):
        response = self._run("create_document", {"documents": [self._todo(str(i)) for i in range(101)]})

        self.assertFalse(response["success"], response)
        self.assertIn("At most 100", response["result"]["error"])
        self.assertEqual(self._todos(), set())

    def test_a_list_and_a_single_record_together_are_refused(self):
        response = self._run(
            "create_document",
            {"doctype": "ToDo", "data": {"description": "single"}, "documents": [self._todo("listed")]},
        )

        self.assertFalse(response["success"], response)
        self.assertIn("not both", response["result"]["error"])

    def test_empty_optional_inputs_are_not_given(self):
        # Clients fill optional inputs in: a single call arrives with "documents": [], and a list
        # call with "doctype": "" and "data": {}.
        single = self._run(
            "create_document",
            {"doctype": "ToDo", "data": {"description": f"One {self.tag}"}, "documents": []},
        )
        listed = self._run("create_document", {"doctype": "", "data": {}, "documents": [self._todo("Two")]})

        self.assertTrue(single["success"], single)
        self.assertTrue(listed["success"], listed)
        self.assertEqual(self._todos(), {f"One {self.tag}", f"Two {self.tag}"})

    def test_submit_is_not_for_a_list(self):
        response = self._run("create_document", {"documents": [self._todo("x")], "submit": True})

        self.assertFalse(response["success"], response)
        self.assertIn("one document at a time", response["result"]["error"])
        self.assertEqual(self._todos(), set())

    def test_validate_only_with_a_list_creates_nothing(self):
        # validate_only runs each record's own validate(), as it does for one record (which
        # doesn't check mandatory fields); either way nothing is created.
        response = self._run(
            "create_document",
            {
                "documents": [self._todo("Fine"), {"doctype": "Note", "data": {"title": f"T {self.tag}"}}],
                "validate_only": True,
            },
        )

        self.assertTrue(response["success"], response)
        result = response["result"]
        self.assertEqual([d["validation_passed"] for d in result["documents"]], [True, True])
        self.assertIn("Nothing was created", result["message"])
        self.assertEqual(self._todos(), set())
        self.assertEqual(self._notes(), set())

    def test_one_record_without_doctype_says_what_to_pass(self):
        response = self._run("create_document", {"data": {"description": "x"}})

        self.assertFalse(response["success"], response)
        self.assertIn("documents", response["result"]["error"])

    def test_records_reach_global_search_once_all_succeed(self):
        with patch("frappe.utils.global_search.sync_value_in_queue") as search_queue:
            response = self._run("create_document", {"documents": [self._todo("Searchable")]})

        self.assertTrue(response["success"], response)
        queued = [c.args[0]["doctype"] for c in search_queue.call_args_list]
        self.assertIn("ToDo", queued)

    def test_an_undone_batch_sends_nothing_out(self):
        creator = DocumentCreate()
        documents = [self._todo("Undone"), {"doctype": "Note", "data": {"content": "no title"}}]
        realtime_before = list(getattr(frappe.local, "_realtime_log", []))
        callbacks_before = len(frappe.db.after_commit._functions)

        with patch("frappe.utils.global_search.sync_value_in_queue") as search_queue:
            with self.assertRaises(document_batch.BatchProblem):
                document_batch.write_all(documents, lambda e: creator.execute(dict(e)), "created")

        self.assertEqual(search_queue.call_count, 0)  # no search entry for the undone ToDo
        self.assertEqual(list(getattr(frappe.local, "_realtime_log", [])), realtime_before)
        self.assertEqual(len(frappe.db.after_commit._functions), callbacks_before)
        self.assertEqual(self._todos(), set())

    def test_a_controller_that_commits_cannot_end_the_batch(self):
        creator = DocumentCreate()
        held = []

        def commits_then_creates(entry):
            # A controller's own frappe.db.commit() would make the records before it permanent.
            held.append("commit" in vars(frappe.db))
            frappe.db.commit()  # nosemgrep: frappe-manual-commit — the controller this test imitates
            return creator.execute(dict(entry))

        documents = [self._todo("First"), {"doctype": "Note", "data": {"content": "no title"}}]
        with self.assertRaises(document_batch.BatchProblem):
            document_batch.write_all(documents, commits_then_creates, "created")

        self.assertEqual(held, [True, True])
        self.assertNotIn("commit", vars(frappe.db))  # given back afterwards
        self.assertEqual(self._todos(), set())

    def test_one_record_without_data_is_still_refused(self):
        # data was required before documents came in; it still is for one record.
        response = self._run("create_document", {"doctype": "ToDo"})

        self.assertFalse(response["success"], response)
        # Refused before any insert is tried, not by the ToDo's own missing description.
        self.assertIn("Pass doctype and data", response["result"]["error"])

    def test_a_callers_own_commit_stand_in_is_given_back(self):
        creator = DocumentCreate()

        def stand_in(*args, **kwargs):
            return None

        frappe.db.commit = stand_in  # as the dry run's held_back sets one
        try:
            document_batch.write_all([self._todo("Kept")], lambda e: creator.execute(dict(e)), "created")
            self.assertIs(vars(frappe.db).get("commit"), stand_in)
        finally:
            del frappe.db.commit

    def test_a_search_entry_that_fails_does_not_report_the_written_records_as_failed(self):
        with patch.object(
            document_batch, "update_global_search", autospec=True, side_effect=RuntimeError("redis down")
        ):
            response = self._run("create_document", {"documents": [self._todo("Written")]})

        self.assertTrue(response["success"], response)  # a retry would create it twice
        self.assertEqual(self._todos(), {f"Written {self.tag}"})

    # -- update_document ---------------------------------------------------------------------

    def _make_todos(self, *texts):
        return [
            frappe.get_doc({"doctype": "ToDo", "description": f"{t} {self.tag}"}).insert().name for t in texts
        ]

    def test_one_call_updates_several_records(self):
        first, second = self._make_todos("Old one", "Old two")

        response = self._run(
            "update_document",
            {
                "documents": [
                    {"doctype": "ToDo", "name": first, "data": {"description": f"New one {self.tag}"}},
                    {"doctype": "ToDo", "name": second, "data": {"priority": "High"}},
                ]
            },
        )

        self.assertTrue(response["success"], response)
        self.assertEqual(
            response["result"]["updated"],
            [{"doctype": "ToDo", "name": first}, {"doctype": "ToDo", "name": second}],
        )
        self.assertEqual(frappe.db.get_value("ToDo", first, "description"), f"New one {self.tag}")
        self.assertEqual(frappe.db.get_value("ToDo", second, "priority"), "High")

    def test_one_update_failing_changes_none(self):
        (first,) = self._make_todos("Keep me")

        response = self._run(
            "update_document",
            {
                "documents": [
                    {"doctype": "ToDo", "name": first, "data": {"description": f"Changed {self.tag}"}},
                    {"doctype": "ToDo", "name": first, "data": {"status": "Not A Status"}},
                ]
            },
        )

        self.assertFalse(response["success"], response)
        self.assertIn("Document 2 (ToDo", response["result"]["error"])
        self.assertEqual(frappe.db.get_value("ToDo", first, "description"), f"Keep me {self.tag}")

    def test_a_failed_update_list_puts_a_moved_file_back(self):
        # Making a File public moves it on disk, and Frappe moves it back after a rollback
        file_doc = frappe.get_doc(
            {"doctype": "File", "file_name": f"batch-{self.tag}.txt", "content": b"secret", "is_private": 1}
        ).insert()
        (todo,) = self._make_todos("Keep me")
        try:
            response = self._run(
                "update_document",
                {
                    "documents": [
                        {"doctype": "File", "name": file_doc.name, "data": {"is_private": 0}},
                        {"doctype": "ToDo", "name": todo, "data": {"status": "Not A Status"}},
                    ]
                },
            )

            self.assertFalse(response["success"], response)
            file_doc.reload()
            self.assertEqual(file_doc.is_private, 1)
            self.assertTrue(os.path.exists(file_doc.get_full_path()), file_doc.get_full_path())
            public = frappe.get_site_path("public", "files", f"batch-{self.tag}.txt")
            self.assertFalse(os.path.exists(public), "the file is still served publicly")
        finally:
            frappe.delete_doc("File", file_doc.name, force=True, ignore_permissions=True)

    def test_an_update_list_is_checked_before_anything_is_written(self):
        (first,) = self._make_todos("Keep me")

        response = self._run(
            "update_document",
            {
                "documents": [
                    {"doctype": "ToDo", "name": first, "data": {"description": f"Changed {self.tag}"}},
                    {"doctype": "ToDo", "data": {"priority": "High"}},
                    {"doctype": "ToDo", "name": "no-such-todo", "data": {"priority": "High"}},
                ]
            },
        )

        self.assertFalse(response["success"], response)
        self.assertEqual(response["result"]["problems"][0]["document"], 2)
        self.assertEqual(frappe.db.get_value("ToDo", first, "description"), f"Keep me {self.tag}")

    def test_an_update_without_data_is_still_refused(self):
        (first,) = self._make_todos("Untouched")
        modified = frappe.db.get_value("ToDo", first, "modified")

        response = self._run("update_document", {"doctype": "ToDo", "name": first})

        self.assertFalse(response["success"], response)
        self.assertEqual(frappe.db.get_value("ToDo", first, "modified"), modified)  # not saved

    def test_an_update_list_and_a_single_record_together_are_refused(self):
        (first,) = self._make_todos("Keep me")

        response = self._run(
            "update_document",
            {
                "doctype": "ToDo",
                "name": first,
                "data": {"priority": "High"},
                "documents": [{"doctype": "ToDo", "name": first, "data": {"priority": "Low"}}],
            },
        )

        self.assertFalse(response["success"], response)
        self.assertIn("not both", response["result"]["error"])

    def test_an_update_list_of_more_than_a_hundred_is_refused(self):
        documents = [{"doctype": "ToDo", "name": f"x{i}", "data": {"priority": "High"}} for i in range(101)]

        response = self._run("update_document", {"documents": documents})

        self.assertFalse(response["success"], response)
        self.assertIn("At most 100", response["result"]["error"])

    def test_a_record_the_user_may_not_change_stops_the_update_list(self):
        (others,) = self._make_todos("Administrator's")
        user = self.make_throwaway_user("updater", roles=("Blogger",))
        frappe.set_user(user)
        # A user may create a ToDo that names them.
        mine = (
            frappe.get_doc({"doctype": "ToDo", "description": f"Mine {self.tag}", "allocated_to": user})
            .insert()
            .name
        )

        response = self._run(
            "update_document",
            {
                "documents": [
                    {"doctype": "ToDo", "name": mine, "data": {"priority": "High"}},
                    {"doctype": "Role", "name": "System Manager", "data": {"disabled": 1}},
                    {"doctype": "Role", "name": "Guest", "data": {"disabled": 1}},
                ]
            },
        )

        self.assertFalse(response["success"], response)
        self.assertEqual([p["document"] for p in response["result"]["problems"]], [2, 3])
        frappe.set_user("Administrator")
        self.assertNotEqual(frappe.db.get_value("ToDo", mine, "priority"), "High")
        self.assertFalse(frappe.db.get_value("Role", "System Manager", "disabled"))
        self.assertTrue(others)
