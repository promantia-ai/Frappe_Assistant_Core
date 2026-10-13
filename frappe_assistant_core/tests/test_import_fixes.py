# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Explain errors in plain language and fix them together (#33991): each dry-run group gets a
plain cause and its fixes; a picked fix becomes a value map or rows left out, a missing record
is created with create_document, and the file is checked again.

Customer again, as in the dry run's tests: Customer Group is a Link to a tree whose group
nodes can't be used, and Customer Type is a Select.
"""

import json
import unittest
from unittest.mock import patch

import frappe

from frappe_assistant_core.core.tool_registry import get_tool_registry
from frappe_assistant_core.plugins.core import import_dry_run, import_fixes
from frappe_assistant_core.tests.base_test import BaseAssistantTest

MAPPING = {
    "Customer Name": "customer_name",
    "Customer Group": "customer_group",
    "Customer Type": "customer_type",
    "Notes": "Don't Import",
}


@unittest.skipUnless("erpnext" in frappe.get_installed_apps(), "needs ERPNext's Customer")
class TestImportFixes(BaseAssistantTest):
    def setUp(self):
        super().setUp()
        frappe.set_user("Administrator")
        self.registry = get_tool_registry()
        self.files_before = set(frappe.get_all("File", pluck="name"))
        self.tag = frappe.generate_hash(length=6)
        # The ticket's example: the file says "Retail", ERPNext has "Retail Customers".
        self.existing_group = f"Retail Customers {self.tag}"
        self.missing = f"Retail {self.tag}"
        frappe.get_doc(
            {
                "doctype": "Customer Group",
                "customer_group_name": self.existing_group,
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

    def _rows(self):
        t = self.tag
        return [
            [f"Alpha {t}", self.existing_group, "Company", ""],  # row 2: ready
            [f"Beta {t}", self.missing, "Company", ""],  # row 3: missing link
            [f"Gamma {t}", self.missing, "Company", ""],  # row 4: missing link, same group
            [f"Delta {t}", self.existing_group, "Robot", ""],  # row 5: not an allowed option
            [f"Alpha {t}", self.existing_group, "Company", ""],  # row 6: duplicate of row 2
        ]

    def _session(self, rows=None):
        """Upload a CSV, open its session, map it and check it, as the earlier steps do."""
        lines = ["Customer Name,Customer Group,Customer Type,Notes"]
        lines += [",".join(row) for row in rows or self._rows()]
        file_doc = frappe.get_doc(
            {
                "doctype": "File",
                "file_name": "customers.csv",
                "content": ("\n".join(lines) + "\n").encode(),
                "is_private": 1,
            }
        ).insert()
        session = self._run("start_import_session", {"file_url": file_doc.file_url})["result"]["session_id"]
        mapped = self._run(
            "set_column_mapping", {"session": session, "mapping": MAPPING, "doctype": "Customer"}
        )
        self.assertTrue(mapped["success"], mapped)
        checked = self._run("dry_run_import", {"session": session})
        self.assertTrue(checked["success"], checked)
        return session, checked["result"]

    def _fix(self, session, *fixes):
        response = self._run("dry_run_import", {"session": session, "fixes": list(fixes)})
        self.assertTrue(response["success"], response)
        return response["result"]

    def _fix_fails(self, session, *fixes):
        response = self._run("dry_run_import", {"session": session, "fixes": list(fixes)})
        self.assertFalse(response["success"], response)
        return response["result"]

    @staticmethod
    def _group(result, cause):
        return next(g for g in result["groups"] if g["cause"] == cause)

    def _doc(self, session):
        return frappe.get_doc("FAC Import Session", session)

    # -- registration ------------------------------------------------------------------

    def test_fixes_are_an_input_of_the_dry_run_and_raise_no_approval_card(self):
        from frappe_assistant_core.utils.tool_category_detector import detect_tool_category

        tool = self.registry.get_tool("dry_run_import")
        self.assertIn("fixes", tool.inputSchema["properties"])
        self.assertNotIn("fixes", tool.inputSchema["required"])
        self.assertEqual(detect_tool_category(tool), "read_only")

    # -- criterion 1: a plain cause and at least one fix per group ------------------------

    def test_every_group_has_a_plain_explanation_and_a_fix(self):
        _session, result = self._session()

        self.assertEqual(len(result["groups"]), 3)
        for group in result["groups"]:
            self.assertTrue(group["explanation"], group)
            self.assertGreaterEqual(len(group["fixes"]), 1, group)
        link = self._group(result, "Missing link")
        self.assertEqual(
            link["explanation"], f"2 rows: there is no Customer Group called '{self.missing}' in ERPNext."
        )

    # -- criterion 2: a missing link offers map (closest first), create, leave out -------------

    def test_a_missing_link_offers_the_closest_names_create_and_leave_out(self):
        _session, result = self._session()

        fixes = self._group(result, "Missing link")["fixes"]

        self.assertEqual([f["action"] for f in fixes], ["map", "create", "leave_out"])
        self.assertEqual(fixes[0]["closest"][0], self.existing_group)
        self.assertNotIn("All Customer Groups", fixes[0]["closest"])  # a group node fails as a link
        self.assertEqual(
            fixes[1],
            {"action": "create", "use": "create_document", "doctype": "Customer Group", "name": self.missing},
        )
        self.assertEqual(fixes[2], {"action": "leave_out", "rows": 2})

    def test_a_group_node_is_never_offered_however_close(self):
        frappe.get_doc(
            {
                "doctype": "Customer Group",
                "customer_group_name": f"{self.missing} Group",
                "parent_customer_group": "All Customer Groups",
                "is_group": 1,
            }
        ).insert()

        closest = import_fixes.closest_names("Customer Group", self.missing)

        self.assertIn(self.existing_group, closest)
        self.assertNotIn(f"{self.missing} Group", closest)

    def test_closest_names_are_ranked_by_the_engine_and_all_exist(self):
        candidates = ["Commercial", "Retail Customers", "Wholesale", "Retail"]

        self.assertEqual(import_fixes.rank("Retail", candidates), ["Retail", "Retail Customers"])
        self.assertEqual(import_fixes.rank("Retial", candidates)[0], "Retail")
        for name in import_fixes.closest_names("Customer Group", self.missing):
            self.assertTrue(frappe.db.exists("Customer Group", name))

    def test_a_value_outside_the_options_is_explained_with_the_choices(self):
        _session, result = self._session()

        option = self._group(result, "Not an allowed option")

        self.assertIn("isn't one of the choices for Customer Type", option["explanation"])
        self.assertIn("Company", option["fixes"][0]["closest"])

    # -- criterion 3: a fix becomes a rule or a master, rows re-checked, new count -----------

    def test_mapping_a_value_becomes_a_rule_and_the_rows_are_checked_again(self):
        session, result = self._session()
        link = self._group(result, "Missing link")

        fixed = self._fix(session, {"group": link["id"], "action": "map", "to": self.existing_group})

        self.assertEqual((fixed["ready"], fixed["failed"]), (3, 2))
        self.assertEqual(fixed["previous"], {"failed": 4, "fixed": 2})
        self.assertNotIn("Missing link", [g["cause"] for g in fixed["groups"]])
        self.assertIn(f"'{self.missing}' becomes '{self.existing_group}' in 2 rows", fixed["applied"][0])
        rules = json.loads(self._doc(session).transformation_rules)["value_maps"]
        self.assertEqual(
            [(r["field"], r["from"], r["to"]) for r in rules],
            [("customer_group", self.missing, self.existing_group)],
        )
        self.assertEqual(self._doc(session).steps[-2].step, "Fix Errors")

    def test_a_select_value_can_be_mapped_to_an_allowed_option(self):
        session, result = self._session()
        option = self._group(result, "Not an allowed option")

        fixed = self._fix(session, {"group": option["id"], "action": "map", "to": "Company"})

        self.assertNotIn("Not an allowed option", [g["cause"] for g in fixed["groups"]])

    def test_mapping_to_a_name_that_does_not_exist_is_refused_and_nothing_is_saved(self):
        session, result = self._session()
        link = self._group(result, "Missing link")

        response = self._fix_fails(session, {"group": link["id"], "action": "map", "to": "Made Up Group"})

        self.assertIn("There is no Customer Group called 'Made Up Group'", response["problems"][0]["error"])
        self.assertFalse(self._doc(session).transformation_rules)

    def test_one_wrong_fix_saves_none(self):
        session, result = self._session()
        link = self._group(result, "Missing link")
        option = self._group(result, "Not an allowed option")

        response = self._fix_fails(
            session,
            {"group": link["id"], "action": "map", "to": self.existing_group},
            {"group": option["id"], "action": "map", "to": "Robotic"},
        )

        self.assertEqual([p["group"] for p in response["problems"]], [option["id"]])
        doc = self._doc(session)
        self.assertFalse(doc.transformation_rules)
        self.assertEqual(doc.status, "Dry Run Failed")

    # -- criterion 4: rows left out are excluded and listed ----------------------------------

    def test_rows_left_out_are_skipped_and_listed_with_their_reason(self):
        session, result = self._session()
        link = self._group(result, "Missing link")

        fixed = self._fix(session, {"group": link["id"], "action": "leave_out", "reason": "Old shop"})

        self.assertEqual((fixed["ready"], fixed["failed"], fixed["excluded"]), (1, 2, 2))
        self.assertEqual(fixed["left_out"], [{"reason": "Old shop", "count": 2, "rows": "3-4"}])
        doc = self._doc(session)
        self.assertEqual(json.loads(doc.excluded_rows), [3, 4])  # the dry run's own format
        self.assertEqual(json.loads(doc.transformation_rules)["left_out"], {"3": "Old shop", "4": "Old shop"})

    # -- criterion 5: every group handled, the file is ready with the final count -------------

    def test_once_every_group_is_handled_the_file_is_ready_with_the_final_count(self):
        session, result = self._session()
        link = self._group(result, "Missing link")

        # "Create" is done the way FACO does it: create_document, then check again.
        created = self._run(
            "create_document",
            {
                "doctype": link["fixes"][1]["doctype"],
                "data": {
                    "customer_group_name": link["value"],
                    "parent_customer_group": "All Customer Groups",
                },
            },
        )
        self.assertTrue(created["success"], created)
        rechecked = self._run("dry_run_import", {"session": session})["result"]
        self.assertEqual((rechecked["ready"], rechecked["failed"]), (3, 2))
        groups = {g["cause"]: g["id"] for g in rechecked["groups"]}

        fixed = self._fix(
            session,
            {"group": groups["Not an allowed option"], "action": "map", "to": "Company"},
            {"group": groups["Duplicate in the file"], "action": "leave_out", "reason": "Repeated"},
        )

        self.assertEqual((fixed["ready"], fixed["failed"], fixed["excluded"]), (4, 0, 1))
        self.assertEqual(fixed["status"], "Ready to Import")
        self.assertIn("The file is ready: 4 rows will be imported. 1 row left out", fixed["message"])
        self.assertEqual(fixed["left_out"], [{"reason": "Repeated", "count": 1, "rows": "6"}])
        self.assertEqual(fixed["groups"], [])
        doc = self._doc(session)
        self.assertEqual((doc.steps[-1].step, doc.steps[-1].outcome), ("Dry Run", "Success"))

    # -- when a fix can't be applied ---------------------------------------------------------

    def test_a_session_not_checked_yet_is_refused(self):
        session, _result = self._session()
        self._run("set_column_mapping", {"session": session, "mapping": {"Notes": "Don't Import"}})

        response = self._fix_fails(session, {"group": "G1", "action": "leave_out"})

        self.assertIn("Check the file first", response["error"])

    def test_a_group_from_an_older_check_is_refused(self):
        session, _result = self._session()

        response = self._fix_fails(session, {"group": "G99", "action": "leave_out"})

        self.assertIn("no group G99", response["problems"][0]["error"])

    def test_fixes_sent_while_a_background_check_runs_are_refused_not_dropped(self):
        session, result = self._session()
        link = self._group(result, "Missing link")
        with patch.object(import_dry_run, "BACKGROUND_ROWS", 2), patch("frappe.enqueue"):
            self._run("dry_run_import", {"session": session})  # now running in the background

            response = self._fix_fails(session, {"group": link["id"], "action": "leave_out"})

        self.assertIn("still being checked", response["error"])
        self.assertFalse(self._doc(session).excluded_rows)

    def test_a_large_file_saves_the_fixes_and_checks_again_in_the_background(self):
        session, result = self._session()
        link = self._group(result, "Missing link")

        with patch.object(import_dry_run, "BACKGROUND_ROWS", 2), patch("frappe.enqueue") as enqueue:
            started = self._fix(session, {"group": link["id"], "action": "map", "to": self.existing_group})

        self.assertEqual(started["state"], "running")
        self.assertIn(f"'{self.missing}' becomes '{self.existing_group}'", started["applied"][0])
        self.assertEqual(enqueue.call_args.kwargs["session_name"], session)

        import_dry_run.run_job(session, "Administrator")  # what the queued job runs

        summary = json.loads(self._doc(session).validation_summary)
        self.assertEqual((summary["ready"], summary["failed"]), (3, 2))

    def test_a_background_check_leaves_the_same_explained_reply_for_the_model(self):
        session, result = self._session()

        with patch.object(import_dry_run, "BACKGROUND_ROWS", 2), patch("frappe.enqueue"):
            started = self._run("dry_run_import", {"session": session})["result"]
        self.assertEqual(started["state"], "running")
        import_dry_run.run_job(session, "Administrator")  # what the queued job runs

        # FACO reads it with get_document: the explanations and fixes are there, as they are
        # in a reply that came back at once.
        summary = json.loads(self._doc(session).validation_summary)
        link = self._group(summary, "Missing link")
        self.assertEqual(
            link["explanation"], f"2 rows: there is no Customer Group called '{self.missing}' in ERPNext."
        )
        self.assertEqual(link["fixes"][0]["closest"][0], self.existing_group)
        self.assertEqual(summary["message"], result["message"])

    def test_a_background_check_with_nothing_failing_says_the_file_is_ready(self):
        session, _result = self._session([[f"Clean {self.tag}", self.existing_group, "Company", ""]])

        with patch.object(import_dry_run, "BACKGROUND_ROWS", 0), patch("frappe.enqueue"):
            self._run("dry_run_import", {"session": session})
        import_dry_run.run_job(session, "Administrator")

        summary = json.loads(self._doc(session).validation_summary)
        self.assertIn("The file is ready: 1 row will be imported.", summary["message"])

    def test_a_group_keeps_its_id_so_a_fix_from_an_earlier_reply_hits_the_same_rows(self):
        session, first = self._session()
        link = self._group(first, "Missing link")
        option = self._group(first, "Not an allowed option")
        self._fix(session, {"group": link["id"], "action": "map", "to": self.existing_group})

        # The user's next answer, acted on with the id from the first reply.
        self._fix(session, {"group": option["id"], "action": "leave_out", "reason": "Robots"})

        self.assertEqual(json.loads(self._doc(session).excluded_rows), [5])

    def test_the_id_of_a_group_already_fixed_is_refused(self):
        session, first = self._session()
        link = self._group(first, "Missing link")
        self._fix(session, {"group": link["id"], "action": "map", "to": self.existing_group})

        response = self._fix_fails(session, {"group": link["id"], "action": "leave_out"})

        self.assertIn(f"no group {link['id']}", response["problems"][0]["error"])
        self.assertFalse(self._doc(session).excluded_rows)

    def test_a_different_file_puts_the_rows_left_out_back_to_be_checked(self):
        session, result = self._session()
        link = self._group(result, "Missing link")
        self._fix(session, {"group": link["id"], "action": "leave_out", "reason": "Old shop"})
        # The corrected export: same columns, but other rows are at rows 3 and 4 now.
        lines = ["Customer Name,Customer Group,Customer Type,Notes"]
        lines += [",".join(row) for row in reversed(self._rows())]
        corrected = frappe.get_doc(
            {
                "doctype": "File",
                "file_name": "customers-corrected.csv",
                "content": ("\n".join(lines) + "\n").encode(),
                "is_private": 1,
            }
        ).insert()

        response = self._run("dry_run_import", {"session": session, "file_url": corrected.file_url})

        self.assertTrue(response["success"], response)
        self.assertEqual(response["result"]["excluded"], 0)
        self.assertIn("2 rows left out before are checked again", response["result"]["notice"])
        doc = self._doc(session)
        self.assertFalse(doc.excluded_rows)
        self.assertNotIn("left_out", json.loads(doc.transformation_rules or "{}"))

    def test_the_same_file_linked_again_keeps_the_rows_left_out(self):
        session, result = self._session()
        link = self._group(result, "Missing link")
        self._fix(session, {"group": link["id"], "action": "leave_out", "reason": "Old shop"})
        same = frappe.db.get_value("File", self._doc(session).source_file, "file_url")

        response = self._run("dry_run_import", {"session": session, "file_url": same})

        self.assertTrue(response["success"], response)
        self.assertEqual(response["result"]["excluded"], 2)
        self.assertEqual(json.loads(self._doc(session).excluded_rows), [3, 4])

    def test_fixes_sent_with_the_sessions_own_file_url_are_applied(self):
        session, result = self._session()
        link = self._group(result, "Missing link")
        own = frappe.db.get_value("File", self._doc(session).source_file, "file_url")

        # What FACO sent in a live demo: the fixes, with the file_url the session already has.
        response = self._run(
            "dry_run_import",
            {
                "session": session,
                "file_url": own,
                "fixes": [{"group": link["id"], "action": "map", "to": self.existing_group}],
            },
        )

        self.assertTrue(response["success"], response)
        self.assertNotIn("Missing link", [g["cause"] for g in response["result"]["groups"]])
        self.assertNotIn("Link File", [step.step for step in self._doc(session).steps])  # nothing re-linked

    def test_a_new_file_and_fixes_in_one_call_are_refused(self):
        session, result = self._session()
        link = self._group(result, "Missing link")
        other = frappe.get_doc(
            {
                "doctype": "File",
                "file_name": "customers-other.csv",
                "content": f"Customer Name,Customer Group,Customer Type,Notes\nZeta {self.tag},x,Company,\n".encode(),
                "is_private": 1,
            }
        ).insert()

        response = self._run(
            "dry_run_import",
            {
                "session": session,
                "file_url": other.file_url,
                "fixes": [{"group": link["id"], "action": "leave_out"}],
            },
        )

        self.assertFalse(response["success"], response)
        doc = self._doc(session)
        self.assertFalse(doc.excluded_rows)
        self.assertNotEqual(frappe.db.get_value("File", doc.source_file, "file_url"), other.file_url)

    def test_closest_names_are_empty_for_a_doctype_the_user_cannot_read(self):
        frappe.set_user(self.make_throwaway_user("noread", roles=("Blogger",)))

        self.assertEqual(import_fixes.closest_names("Customer Group", self.missing), [])

    def test_an_empty_fixes_list_is_a_plain_check(self):
        session, _result = self._session()

        # FAC Cloud fills optional inputs in: a plain check arrives with "fixes": [] and file_url "".
        response = self._run("dry_run_import", {"session": session, "file_url": "", "fixes": []})

        self.assertTrue(response["success"], response)
        self.assertEqual((response["result"]["ready"], response["result"]["failed"]), (1, 4))

    def test_create_is_not_a_fix_it_is_done_with_create_document(self):
        session, result = self._session()
        link = self._group(result, "Missing link")

        response = self._fix_fails(session, {"group": link["id"], "action": "create"})

        self.assertIn("create_document", response["problems"][0]["error"])
        self.assertFalse(frappe.db.exists("Customer Group", self.missing))

    def test_a_duplicate_can_only_be_left_out(self):
        session, result = self._session()
        duplicate = self._group(result, "Duplicate in the file")

        self.assertEqual([f["action"] for f in duplicate["fixes"]], ["leave_out"])
        response = self._fix_fails(session, {"group": duplicate["id"], "action": "map", "to": "x"})
        self.assertIn("Only a missing link", response["problems"][0]["error"])

    def test_a_child_table_value_is_mapped_on_the_child_table_not_the_parent(self):
        # Sales Invoice has project on the invoice and on each item line (items.project).
        company = next(iter(frappe.get_all("Company", pluck="name", limit=1)), None)
        if not company:
            self.skipTest("needs a Company for the Project")
        project = frappe.get_doc(
            {"doctype": "Project", "project_name": f"Site {self.tag}", "company": company}
        ).insert()
        ghost = f"Ghost {self.tag}"
        lines = ["Customer,Project,Item Code,Quantity,Item Project"]
        lines += [
            f"Cust {self.tag},{project.name},Item {self.tag},1,{ghost}",
            f"Cust {self.tag},{project.name},Item {self.tag},1,{ghost}",
        ]
        file_doc = frappe.get_doc(
            {
                "doctype": "File",
                "file_name": "invoices.csv",
                "content": ("\n".join(lines) + "\n").encode(),
                "is_private": 1,
            }
        ).insert()
        session = self._run("start_import_session", {"file_url": file_doc.file_url})["result"]["session_id"]
        mapping = {
            "Customer": "customer",
            "Project": "project",
            "Item Code": "items.item_code",
            "Quantity": "items.qty",
            "Item Project": "items.project",
        }
        mapped = self._run(
            "set_column_mapping", {"session": session, "mapping": mapping, "doctype": "Sales Invoice"}
        )
        self.assertTrue(mapped["success"], mapped)
        checked = self._run("dry_run_import", {"session": session})["result"]
        item_project = next(g for g in checked["groups"] if g.get("value") == ghost)

        fixed = self._fix(session, {"group": item_project["id"], "action": "map", "to": project.name})

        self.assertNotIn(ghost, [g.get("value") for g in fixed["groups"]])
        rule = json.loads(self._doc(session).transformation_rules)["value_maps"][0]
        self.assertEqual((rule["parent"], rule["field"]), ("Sales Invoice Item", "project"))

    def test_another_users_session_is_refused(self):
        session, result = self._session()
        frappe.set_user(self.make_throwaway_user("other", roles=("Blogger",)))

        response = self._run(
            "dry_run_import", {"session": session, "fixes": [{"group": "G1", "action": "leave_out"}]}
        )

        self.assertFalse(response["success"])
        frappe.set_user("Administrator")
        self.assertFalse(self._doc(session).excluded_rows)
