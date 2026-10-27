# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""A finished migration to make recipes from (#34420). A base for the recipe tests, with no tests of its own."""

import frappe

from frappe_assistant_core.tests.test_import_run import ImportRunCase
from frappe_assistant_core.utils import import_plan

SESSION = "FAC Import Session"
RECIPE = "FAC Migration Recipe"
GROUP_MAPPING = {"Name": "item_group_name", "Parent": "parent_item_group", "Is Group": "is_group"}


class RecipeCase(ImportRunCase):
    def setUp(self):
        super().setUp()
        self.system = f"odoo{self.tag}"
        self.g1, self.g2 = f"RG1 {self.tag}", f"RG2 {self.tag}"
        frappe.db.delete("DefaultValue", {"defkey": ["like", "fac_old_id::Item Group::%"]})

    def _mapped(self, file_name, content, doctype, mapping, **arguments):
        started = self._run("start_import_session", {"file_url": self._file(file_name, content).file_url})
        session = started["result"]["session_id"]
        mapped = self._run(
            "set_column_mapping", {"session": session, "mapping": mapping, "doctype": doctype, **arguments}
        )
        self.assertTrue(mapped["success"], mapped)
        return session

    def _opened(self, file_name, content):
        """A new file, opened and not mapped: what a recipe run is given."""
        started = self._run("start_import_session", {"file_url": self._file(file_name, content).file_url})
        self.assertTrue(started["success"], started)
        return started["result"]["session_id"]

    def _finished_plan(self):
        """An Item Groups file (with a column the recipe doesn't map) imported by one plan."""
        groups = self._mapped(
            "groups.csv",
            (
                f"ID,Name,Parent,Is Group,Notes\nG-{self.tag}-1,{self.g1},All Item Groups,1,x\n"
                f"G-{self.tag}-2,{self.g2},{self.g1},0,y\n"
            ).encode(),
            "Item Group",
            GROUP_MAPPING,
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
        self._approve(plan)
        self.assertEqual(import_plan.get_plan(plan).status, "Completed")
        return plan, groups

    def _approve(self, plan):
        figures = {"plan": plan, **import_plan.card_figures(import_plan.get_plan(plan))}
        with self._each_row_commits():
            response = self._run("run_migration_plan", figures)
        self.assertTrue(response["success"], response)
        return response

    def _recipe(self):
        plan, _groups = self._finished_plan()
        saved = self._run("save_migration_recipe", {"plan": plan})
        self.assertTrue(saved["success"], saved)
        return saved["result"]["recipe"]
