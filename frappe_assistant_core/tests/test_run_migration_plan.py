# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Running a migration plan after one approval (#33995)."""

import unittest
from unittest.mock import patch

import frappe
from erpnext.setup.doctype.item_group.item_group import ItemGroup

from frappe_assistant_core.tests.base_test import item_hsn_fields
from frappe_assistant_core.tests.test_import_plan import set_plan
from frappe_assistant_core.tests.test_import_run import ImportRunCase
from frappe_assistant_core.utils import import_plan, import_run

SESSION = "FAC Import Session"


@unittest.skipUnless("erpnext" in frappe.get_installed_apps(), "needs ERPNext's Item Group and Item")
class TestRunMigrationPlan(ImportRunCase):
    def _mapped(self, file_name, content, doctype, mapping):
        started = self._run("start_import_session", {"file_url": self._file(file_name, content).file_url})
        session = started["result"]["session_id"]
        mapped = self._run("set_column_mapping", {"session": session, "mapping": mapping, "doctype": doctype})
        self.assertTrue(mapped["success"], mapped)
        return session

    def _ready_plan(self, rule="leave_out"):
        """A plan of an Item Group tree (G1 > G2) and two Items in G2, given items first."""
        t = self.tag
        self.g1, self.g2 = f"G1 {t}", f"G2 {t}"
        self.items = [f"I1 {t}", f"I2 {t}"]
        groups = self._mapped(
            "groups.csv",
            f"Name,Parent,Is Group\n{self.g1},All Item Groups,1\n{self.g2},{self.g1},0\n".encode(),
            "Item Group",
            {"Name": "item_group_name", "Parent": "parent_item_group", "Is Group": "is_group"},
        )
        # with India Compliance an items file carries the HSN code every Item needs there
        hsn = "".join(f",{code}" for code in item_hsn_fields().values())
        items = self._mapped(
            "items.csv",
            "".join(f"{code},{code},{self.g2},Nos{hsn}\n" for code in self.items)
            .join([f"Code,Name,Group,UOM{',HSN' if hsn else ''}\n", ""])
            .encode(),
            "Item",
            {
                "Code": "item_code",
                "Name": "item_name",
                "Group": "item_group",
                "UOM": "stock_uom",
                **({"HSN": "gst_hsn_code"} if hsn else {}),
            },
        )
        plan = self._run("plan_migration", {"sessions": [items, groups], "failure_rule": rule})["result"][
            "plan"
        ]
        answered = self._run(
            "plan_questions", {"plan": plan, "answers": [{"id": "plan:failure_rule", "rule": rule}]}
        )["result"]
        self.assertTrue(answered["ready"], answered)
        return plan, groups, items

    def _approve(self, plan, **changes):
        # what plan_questions hands FACO for the approval card, with the changes a test makes to it
        figures = {"plan": plan, **import_plan.card_figures(import_plan.get_plan(plan)), **changes}
        with self._each_row_commits():
            return self._run("run_migration_plan", figures)

    def test_one_approval_imports_every_file_in_order(self):
        plan, groups, items = self._ready_plan()

        response = self._approve(plan)

        self.assertTrue(response["success"], response)
        self.assertEqual(frappe.db.get_value("Item Group", self.g2, "parent_item_group"), self.g1)
        for code in self.items:
            self.assertEqual(frappe.db.get_value("Item", code, "item_group"), self.g2)
        self.assertEqual(import_plan.get_plan(plan).status, "Completed")
        self.assertEqual(
            [frappe.db.get_value(SESSION, s, "status") for s in (groups, items)], ["Completed"] * 2
        )

    def test_another_users_session_in_the_plan_is_never_imported(self):
        plan, _groups, _items = self._ready_plan()
        g3 = f"G3 {self.tag}"
        theirs = self._mapped(
            "their-groups.csv",
            f"Name,Parent,Is Group\n{g3},All Item Groups,0\n".encode(),
            "Item Group",
            {"Name": "item_group_name", "Parent": "parent_item_group", "Is Group": "is_group"},
        )
        checked = self._run("dry_run_import", {"session": theirs})["result"]
        self.assertEqual(checked["status"], "Ready to Import", checked)
        # Another user's session, put in this plan: inserts and saves refuse that (#34429), so a
        # direct database write stands in for any way round them.
        other = self.make_throwaway_user("other", roles=("Desk User",))
        frappe.db.set_value(SESSION, theirs, {"user": other, "migration_plan": plan})
        before = frappe.db.get_value(SESSION, theirs, ["status", "validated_fingerprint"], as_dict=True)

        self.assertEqual(import_plan.card_figures(import_plan.get_plan(plan))["rows"], 4)  # 2 groups, 2 items
        response = self._approve(plan)

        self.assertTrue(response["success"], response)
        self.assertEqual(import_plan.get_plan(plan).status, "Completed")
        self.assertFalse(frappe.db.exists("Item Group", g3))
        self.assertEqual(
            frappe.db.get_value(SESSION, theirs, ["status", "validated_fingerprint"], as_dict=True), before
        )
        self.assertFalse(frappe.get_all("FAC Import Session Data Import", filters={"parent": theirs}))
        self.assertFalse(frappe.get_all("FAC Import Session Record", filters={"parent": theirs}))
        self.assertNotIn(theirs, frappe.as_json(import_plan.get_plan(plan).progress))

    def test_the_card_must_match_the_plan(self):
        plan, _groups, _items = self._ready_plan()

        response = self._approve(plan, rows=3)

        self.assertFalse(response["success"], response)
        self.assertIn("4 rows", response["result"]["error"])
        self.assertFalse(frappe.db.exists("Item Group", self.g1))

    def test_a_plan_changed_since_its_check_is_not_run(self):
        plan, _groups, items = self._ready_plan()
        self._run(
            "set_column_mapping",
            {"session": items, "mapping": {"Name": "description"}, "doctype": "Item"},
        )

        response = self._approve(plan)

        self.assertFalse(response["success"], response)
        self.assertIn("items.csv", response["result"]["error"])
        self.assertFalse(frappe.db.exists("Item Group", self.g1))

    def test_a_file_the_user_cant_import_stops_the_run_before_it_starts(self):
        plan, _groups, _items = self._ready_plan()
        real = import_run.cannot_import

        with patch.object(
            import_run,
            "cannot_import",
            side_effect=lambda doctype, *a: "no Item for you" if doctype == "Item" else real(doctype, *a),
        ):
            response = self._approve(plan)

        self.assertFalse(response["success"], response)
        self.assertIn("items.csv", response["result"]["error"])
        self.assertFalse(frappe.db.exists("Item Group", self.g1))

    def _g2_fails(self):
        real = ItemGroup.validate

        def validate(doc, *args, **kwargs):
            if doc.name == self.g2:
                frappe.throw(frappe._("G2 refused for the test"))
            return real(doc, *args, **kwargs)

        return patch.object(ItemGroup, "validate", autospec=True, side_effect=validate)

    def test_rows_whose_parent_failed_are_left_out_and_the_plan_is_partial(self):
        plan, _groups, items = self._ready_plan()

        with self._g2_fails():
            response = self._approve(plan)

        self.assertTrue(response["success"], response)
        self.assertTrue(frappe.db.exists("Item Group", self.g1))
        self.assertFalse(any(frappe.db.exists("Item", code) for code in self.items))
        doc = import_plan.get_plan(plan)
        self.assertEqual(doc.status, "Partially Completed")
        self.assertTrue(
            any("2 rows left out" in step.message for step in doc.steps), [s.message for s in doc.steps]
        )
        reported = {f["session"]: f for f in import_plan.result(doc)["files"]}
        self.assertEqual(reported[items]["left_out"], 2)

    def test_the_stop_rule_stops_before_the_file(self):
        # G2 now fails the groups file's own check, just before its import: the plan stops there.
        plan, groups, _items = self._ready_plan(rule="stop")

        with self._g2_fails():
            self._approve(plan)

        doc = import_plan.get_plan(plan)
        self.assertEqual(doc.status, "Stopped")
        self.assertIn("groups.csv", doc.steps[-1].message)
        self.assertFalse(frappe.db.exists("Item Group", self.g1))
        self.assertFalse(any(frappe.db.exists("Item", code) for code in self.items))
        self.assertEqual(frappe.db.get_value(SESSION, groups, "status"), "Dry Run Failed")

    def test_stopping_keeps_what_is_imported(self):
        plan, groups, items = self._ready_plan()
        real = import_plan._file_done

        def done_then_stop(plan_name, session_name):
            finished = real(plan_name, session_name)
            import_plan.request_stop(plan_name)
            return finished

        with patch.object(import_plan, "_file_done", side_effect=done_then_stop):
            self._approve(plan)

        self.assertEqual(import_plan.get_plan(plan).status, "Stopped")
        self.assertTrue(frappe.db.exists("Item Group", self.g2))
        self.assertFalse(any(frappe.db.exists("Item", code) for code in self.items))

    def test_a_plan_not_running_cant_be_stopped(self):
        plan, _groups, _items = self._ready_plan()

        response = self._run("stop_migration_plan", {"plan": plan})

        self.assertFalse(response["success"], response)
        self.assertIn("not running", response["result"]["error"])

    def test_the_run_is_privileged_and_stop_is_write(self):
        from frappe_assistant_core.utils.tool_category_detector import PRIVILEGED_TOOLS, WRITE_TOOLS

        self.assertIn("run_migration_plan", PRIVILEGED_TOOLS)
        self.assertIn("stop_migration_plan", WRITE_TOOLS)


@unittest.skipUnless("erpnext" in frappe.get_installed_apps(), "needs ERPNext's Item Group and Item")
class TestARunThatGoesWrong(TestRunMigrationPlan):
    """What the review of the run found (#33995): the plan never stays Running with nothing to
    carry it on, stopping never breaks it, and a file changed after the approval isn't imported."""

    def test_an_error_between_files_ends_the_plan_instead_of_leaving_it_running(self):
        plan, _groups, _items = self._ready_plan()
        real = import_plan._prepare

        def second_file_breaks(plan_doc, child):
            if child.file_name == "items.csv":
                raise RuntimeError("the disk is full")
            return real(plan_doc, child)

        with patch.object(import_plan, "_prepare", side_effect=second_file_breaks):
            self._approve(plan)

        doc = import_plan.get_plan(plan)
        self.assertEqual(doc.status, "Stopped")
        self.assertIn("the disk is full", doc.steps[-1].message)
        self.assertTrue(frappe.db.exists("Item Group", self.g2))  # the file before it stays imported

    def test_a_plan_runs_in_the_background_also_in_developer_mode(self):
        # Data Import runs in the request in developer mode, but a plan imports every file: run in
        # a chat request it outlasts the client's timeout, and the whole run rolls back.
        in_test = frappe.flags.in_test
        frappe.flags.in_test = False  # as a chat request runs, outside the test runner
        try:
            with patch.object(import_plan.frappe, "enqueue") as enqueue, patch.dict(
                frappe.conf, {"developer_mode": 1}
            ):
                import_plan._enqueue("IMP-0", "run_job", user="Administrator")
        finally:
            frappe.flags.in_test = in_test

        self.assertFalse(enqueue.call_args.kwargs["now"])

    def test_a_finished_files_job_hands_the_plan_to_a_job_of_its_own(self):
        plan, groups, _items = self._ready_plan()
        set_plan(
            plan, status="Running", progress=frappe.as_json({"current": groups, "user": "Administrator"})
        )

        with patch.object(import_plan, "_enqueue") as enqueue, patch.object(
            import_plan, "_next_file"
        ) as next_file:
            import_plan.child_finished(groups)

        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[1], "continue_job")
        next_file.assert_not_called()  # nothing of the plan runs inside the file's job

    def test_stopping_doesnt_change_the_plan_the_run_is_saving(self):
        plan, _groups, _items = self._ready_plan()
        set_plan(plan, status="Running")
        before = frappe.db.get_value(SESSION, plan, "modified")

        with patch.object(import_plan, "alive", return_value=True):
            response = self._run("stop_migration_plan", {"plan": plan})

        self.assertTrue(response["success"], response)
        stopped = import_plan.get_plan(plan)
        self.assertEqual((stopped.stop_requested, stopped.status), (1, "Running"))
        self.assertEqual(frappe.db.get_value(SESSION, plan, "modified"), before)

    def test_stopping_a_plan_nothing_is_running_for_ends_it(self):
        plan, _groups, _items = self._ready_plan()
        set_plan(plan, status="Running")

        with patch.object(import_plan, "alive", return_value=False):
            response = self._run("stop_migration_plan", {"plan": plan})

        self.assertTrue(response["success"], response)
        self.assertEqual(import_plan.get_plan(plan).status, "Stopped")

    def test_a_file_changed_after_the_approval_isnt_imported(self):
        plan, groups, items = self._ready_plan()
        real = import_plan._file_done

        def groups_done_then_items_change(plan_name, session_name):
            finished = real(plan_name, session_name)
            if session_name == groups:
                frappe.db.set_value(SESSION, items, "validated_fingerprint", "changed")
            return finished

        with patch.object(import_plan, "_file_done", side_effect=groups_done_then_items_change):
            self._approve(plan)

        doc = import_plan.get_plan(plan)
        self.assertEqual(doc.status, "Stopped")
        self.assertIn("items.csv changed after the plan was approved", doc.steps[-1].message)
        self.assertFalse(any(frappe.db.exists("Item", code) for code in self.items))

    def test_a_session_of_a_running_plan_cant_be_changed(self):
        plan, _groups, items = self._ready_plan()
        set_plan(plan, status="Running")

        response = self._run("set_column_mapping", {"session": items, "mapping": {"Name": "description"}})

        self.assertFalse(response["success"], response)
        self.assertIn(plan, str(response))

    def test_masters_that_cant_all_be_made_leave_none_behind(self):
        plan, groups, _items = self._ready_plan()
        first, second = f"M1 {self.tag}", f"M2 {self.tag}"
        entries = [
            {
                "session": groups,
                "doctype": "Customer Group",
                "name": name,
                "data": {"customer_group_name": name, "parent_customer_group": "All Customer Groups"},
            }
            for name in (first, second)
        ]
        set_plan(plan, masters=frappe.as_json(entries))
        from frappe_assistant_core.utils import import_fixes

        real = import_fixes.create_master

        def second_refused(entry):
            if entry["name"] == second:
                return {"success": False, "error": "refused for the test", "doctype": entry["doctype"]}
            return real(entry)

        with patch.object(import_fixes, "create_master", side_effect=second_refused):
            with self.assertRaises(import_plan.PlanProblem):
                import_plan.create_planned(import_plan.get_plan(plan))

        self.assertFalse(frappe.db.exists("Customer Group", first))
        self.assertEqual(frappe.get_doc(SESSION, groups).created_records, [])

    def test_a_master_the_user_cant_create_stops_the_approval(self):
        plan, groups, _items = self._ready_plan()
        name = f"M {self.tag}"
        set_plan(
            plan,
            masters=frappe.as_json(
                [
                    {
                        "session": groups,
                        "doctype": "Customer Group",
                        "name": name,
                        "data": {"customer_group_name": name},
                    }
                ]
            ),
        )
        # A new master changes what the files are checked against: they are checked again first.
        self.assertTrue(self._run("plan_questions", {"plan": plan})["result"]["ready"])

        with patch.object(
            import_plan.frappe,
            "has_permission",
            side_effect=lambda doctype, *a, **k: doctype != "Customer Group",
        ):
            problem = import_plan.run_problem(import_plan.get_plan(plan))

        self.assertIn("can't be created", problem)
        self.assertIn("Customer Group", problem)

    def test_a_stopped_plans_files_can_join_a_new_plan(self):
        plan, groups, items = self._ready_plan()
        set_plan(plan, status="Stopped")

        response = self._run("plan_migration", {"sessions": [groups, items]})

        self.assertTrue(response["success"], response)
        self.assertNotEqual(response["result"]["plan"], plan)
