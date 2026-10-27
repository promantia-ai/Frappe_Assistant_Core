# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Saving a finished migration as a recipe (#34420 P2.2, RC-1, M-D1, M-D2)."""

import json
import unittest

import frappe

from frappe_assistant_core.tests.test_import_run import ImportRunCase
from frappe_assistant_core.utils import import_keys as keys
from frappe_assistant_core.utils import import_plan, migration_recipe

SESSION = "FAC Import Session"
RECIPE = "FAC Migration Recipe"


@unittest.skipUnless("erpnext" in frappe.get_installed_apps(), "needs ERPNext's Item Group and Item")
class TestSaveRecipe(ImportRunCase):
    def setUp(self):
        super().setUp()
        self.system = f"odoo{self.tag}"
        self.g1, self.g2 = f"RG1 {self.tag}", f"RG2 {self.tag}"
        self.secret = f"Secret-{self.tag}"  # a cell value that must never reach a recipe
        frappe.db.delete("DefaultValue", {"defkey": ["like", "fac_old_id::Item Group::%"]})

    def _mapped(self, file_name, content, doctype, mapping, **arguments):
        started = self._run("start_import_session", {"file_url": self._file(file_name, content).file_url})
        session = started["result"]["session_id"]
        mapped = self._run(
            "set_column_mapping", {"session": session, "mapping": mapping, "doctype": doctype, **arguments}
        )
        self.assertTrue(mapped["success"], mapped)
        return session

    def _finished_plan(self):
        """Two Item Group files imported by one plan: the parent group, then its child."""
        groups = self._mapped(
            "groups.csv",
            (
                f"ID,Name,Parent,Is Group\nG-{self.tag}-1,{self.g1},All Item Groups,1\n"
                f"G-{self.tag}-2,{self.g2},{self.g1},0\n"
            ).encode(),
            "Item Group",
            {"Name": "item_group_name", "Parent": "parent_item_group", "Is Group": "is_group"},
            source_key_column="ID",
            source_system=self.system,
        )
        self.assertTrue(self._run("choose_old_id_storage", {"session": groups, "use": "key_map"})["success"])
        plan = self._run("plan_migration", {"sessions": [groups], "failure_rule": "leave_out"})["result"][
            "plan"
        ]
        self._run(
            "plan_questions", {"plan": plan, "answers": [{"id": "plan:failure_rule", "rule": "leave_out"}]}
        )
        figures = {"plan": plan, **import_plan.card_figures(import_plan.get_plan(plan))}
        with self._each_row_commits():
            self.assertTrue(self._run("run_migration_plan", figures)["success"])
        self.assertEqual(import_plan.get_plan(plan).status, "Completed")
        return plan, groups

    def _save(self, **arguments):
        return self._run("save_migration_recipe", arguments)

    def test_a_completed_plan_is_saved_with_a_step_per_file(self):
        plan, groups = self._finished_plan()

        saved = self._save(plan=plan)

        self.assertTrue(saved["success"], saved)
        doc = frappe.get_doc(RECIPE, saved["result"]["recipe"])
        self.assertEqual(doc.source_plan, plan)
        self.assertEqual(doc.source_site, frappe.local.site)
        self.assertEqual(doc.failure_rule, "leave_out")
        self.assertEqual([step.target_doctype for step in doc.steps], ["Item Group"])
        step = doc.steps[0]
        self.assertEqual(json.loads(step.header_signature), ["ID", "Name", "Parent", "Is Group"])
        self.assertEqual(json.loads(step.column_map)["1"], "item_group_name")
        self.assertEqual(json.loads(step.transformation_rules)["source_key"]["system"], self.system)

    def test_the_result_lists_the_steps_for_the_user(self):
        plan, _groups = self._finished_plan()

        steps = self._save(plan=plan)["result"]["steps"]

        self.assertEqual(steps[0]["target"], "Item Group")
        self.assertEqual(steps[0]["position"], 1)
        self.assertFalse(steps[0]["transaction_step"])

    def test_no_row_of_the_files_is_in_the_recipe(self):
        plan, _groups = self._finished_plan()

        doc = frappe.get_doc(RECIPE, self._save(plan=plan)["result"]["recipe"])

        text = json.dumps(doc.as_dict(), default=str)
        for cell in (self.g1, self.g2, f"G-{self.tag}-1", f"G-{self.tag}-2"):
            self.assertNotIn(cell, text)

    def test_which_rows_were_left_out_and_the_run_state_stay_behind(self):
        plan, groups = self._finished_plan()
        frappe.db.set_value(
            SESSION,
            groups,
            {
                "excluded_rows": "[3]",
                "transformation_rules": json.dumps({"left_out": {"3": self.secret}, "value_maps": []}),
            },
        )

        text = json.dumps(
            frappe.get_doc(RECIPE, self._save(plan=plan)["result"]["recipe"]).as_dict(), default=str
        )

        self.assertNotIn(self.secret, text)
        self.assertNotIn("left_out", text)
        self.assertNotIn("stop_requested", text)

    def test_a_plan_that_has_not_run_is_refused(self):
        groups = self._mapped(
            "g2.csv",
            f"Name,Parent,Is Group\n{self.g1},All Item Groups,1\n".encode(),
            "Item Group",
            {"Name": "item_group_name", "Parent": "parent_item_group", "Is Group": "is_group"},
        )
        plan = self._run("plan_migration", {"sessions": [groups]})["result"]["plan"]

        refused = self._save(plan=plan)

        self.assertFalse(refused["success"])
        self.assertIn("Completed or Partially Completed", refused["error"])
        self.assertFalse(frappe.db.exists(RECIPE, {"source_plan": plan}))

    def test_one_session_that_completed_is_saved_as_a_one_step_recipe(self):
        _plan, groups = self._finished_plan()

        saved = self._save(session=groups)

        self.assertTrue(saved["success"], saved)
        doc = frappe.get_doc(RECIPE, saved["result"]["recipe"])
        self.assertEqual(len(doc.steps), 1)
        self.assertEqual(doc.source_plan, groups)

    def test_a_session_that_did_not_complete_is_refused(self):
        groups = self._mapped(
            "g3.csv",
            f"Name,Parent,Is Group\n{self.g1},All Item Groups,1\n".encode(),
            "Item Group",
            {"Name": "item_group_name", "Parent": "parent_item_group", "Is Group": "is_group"},
        )

        refused = self._save(session=groups)

        self.assertFalse(refused["success"])
        self.assertIn("Completed", refused["error"])

    def test_plan_and_session_together_are_refused(self):
        refused = self._save(plan="a", session="b")

        self.assertFalse(refused["success"])

    def test_a_user_who_cannot_create_recipes_is_refused(self):
        plan, _groups = self._finished_plan()
        frappe.set_user(self.make_desk_user("norecipe"))

        refused = self._save(plan=plan)

        self.assertFalse(refused["success"])
        self.assertFalse(frappe.db.exists(RECIPE, {"source_plan": plan}))

    def test_a_preset_session_keeps_its_preset_id(self):
        plan, groups = self._finished_plan()
        options = frappe.parse_json(frappe.db.get_value(SESSION, groups, "template_options") or "{}")
        options["preset"] = {"id": "zoho_books", "target": "Customers"}
        frappe.db.set_value(SESSION, groups, "template_options", json.dumps(options))

        doc = frappe.get_doc(RECIPE, self._save(plan=plan)["result"]["recipe"])

        self.assertEqual(doc.steps[0].preset_id, "zoho_books")

    def test_opening_engine_settings_keep_no_dates(self):
        value = migration_recipe._without_dates(
            {
                "cut_off_date": "2026-03-31",
                "warehouse": "Main",
                "posting_date": "2026-04-01",
                "rows": [{"on": "2026-01-02"}],
            }
        )

        self.assertEqual(value, {"warehouse": "Main", "rows": [{}]})

    def test_the_keys_of_the_plan_are_not_changed_by_saving(self):
        plan, _groups = self._finished_plan()
        before = keys.resolve("Item Group", self.system, [f"G-{self.tag}-1"])

        self._save(plan=plan)

        self.assertEqual(keys.resolve("Item Group", self.system, [f"G-{self.tag}-1"]), before)
