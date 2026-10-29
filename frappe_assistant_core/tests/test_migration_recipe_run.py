# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Running a recipe on new files (#34420 P2.3, RC-2, RC-3, RC-4, RC-7, RC-9)."""

import json
import unittest

import frappe

from frappe_assistant_core.tests.migration_recipe_case import RECIPE, SESSION, RecipeCase
from frappe_assistant_core.utils import import_keys as keys
from frappe_assistant_core.utils import import_plan


@unittest.skipUnless("erpnext" in frappe.get_installed_apps(), "needs ERPNext's Item Group")
class TestRunRecipe(RecipeCase):
    def setUp(self):
        super().setUp()
        self.n1, self.n2 = f"RN1 {self.tag}", f"RN2 {self.tag}"

    def _new_file(self, header="ID,Name,Parent,Is Group,Notes", name="fresh.csv", rows=None):
        rows = rows or [
            f"G-{self.tag}-8,{self.n1},All Item Groups,1,x",
            f"G-{self.tag}-9,{self.n2},{self.n1},0,y",
        ]
        return self._opened(name, ("\n".join([header, *rows]) + "\n").encode())

    def _run_recipe(self, recipe, *sessions):
        return self._run("migration_recipe", {"action": "run", "recipe": recipe, "sessions": list(sessions)})

    def test_a_fresh_file_is_matched_mapped_and_planned_in_the_recipes_order(self):
        recipe = self._recipe()
        fresh = self._new_file()

        ran = self._run_recipe(recipe, fresh)

        self.assertTrue(ran["success"], ran)
        result = ran["result"]
        self.assertEqual([m["session"] for m in result["matched"]], [fresh])
        self.assertEqual(result["unmatched_sessions"], [])
        session = frappe.get_doc(SESSION, fresh)
        self.assertEqual(session.status, "Mapped")
        self.assertEqual(session.target_doctype, "Item Group")
        self.assertEqual(session.migration_plan, result["plan"])
        self.assertEqual(import_plan.get_plan(result["plan"]).failure_rule, "leave_out")

    def test_a_file_opened_when_a_recipe_is_saved_says_so(self):
        # "import this" with a fresher export: the open is where FACO learns a recipe could run it
        recipe = self._recipe()

        opened = self._run(
            "start_import_session",
            {"file_url": self._file("fresh.csv", f"ID,Name\nG-{self.tag}-8,{self.n1}\n".encode()).file_url},
        )

        self.assertTrue(opened["success"], opened)
        self.assertIn(recipe, [r["name"] for r in opened["result"]["recipes"]])
        self.assertIn("migration_recipe", opened["result"]["note"])

    def test_columns_in_another_order_are_mapped_by_their_header(self):
        recipe = self._recipe()
        fresh = self._new_file(
            header="Notes,Is Group,Parent,Name,ID",
            rows=[f"x,1,All Item Groups,{self.n1},G-{self.tag}-8"],
        )

        self._run_recipe(recipe, fresh)

        mapped = frappe.parse_json(frappe.get_doc(SESSION, fresh).template_options)["column_to_field_map"]
        # the old ID column is kept out of the import, as it was in the first run
        self.assertEqual(
            mapped, {"1": "is_group", "2": "parent_item_group", "3": "item_group_name", "4": "Don't Import"}
        )
        self.assertEqual(
            keys.source_key(frappe.get_doc(SESSION, fresh)), {"position": 4, "system": self.system}
        )

    def test_nothing_new_means_the_plan_asks_only_the_failure_rule(self):
        recipe = self._recipe()
        fresh = self._new_file()
        plan = self._run_recipe(recipe, fresh)["result"]["plan"]

        asked = self._run("dry_run_import", {"plan": plan})["result"]

        self.assertEqual([q["kind"] for q in asked["questions"]], ["failure_rule"], asked)

    def test_a_new_column_is_one_question_and_the_plan_waits_for_it(self):
        recipe = self._recipe()
        fresh = self._new_file(
            header="ID,Name,Parent,Is Group,Notes,Colour",
            rows=[f"G-{self.tag}-8,{self.n1},All Item Groups,1,x,red"],
        )
        plan = self._run_recipe(recipe, fresh)["result"]["plan"]

        asked = self._run("dry_run_import", {"plan": plan})["result"]

        new = [q for q in asked["questions"] if q["kind"] == "recipe_new"]
        self.assertEqual([q["what"] for q in new], ["new_columns"])
        self.assertEqual(new[0]["columns"], ["Colour"])
        self.assertFalse(asked["ready"])

    def test_answering_the_new_column_question_makes_the_plan_ready(self):
        recipe = self._recipe()
        fresh = self._new_file(
            header="ID,Name,Parent,Is Group,Notes,Colour",
            rows=[f"G-{self.tag}-8,{self.n1},All Item Groups,1,x,red"],
        )
        plan = self._run_recipe(recipe, fresh)["result"]["plan"]
        asked = self._run("dry_run_import", {"plan": plan})["result"]
        ask_id = next(q["id"] for q in asked["questions"] if q["kind"] == "recipe_new")

        answered = self._run(
            "dry_run_import",
            {
                "plan": plan,
                "answers": [{"id": ask_id, "ok": True}, {"id": "plan:failure_rule", "rule": "leave_out"}],
            },
        )["result"]

        self.assertTrue(answered["ready"], answered)

    def test_a_column_the_file_no_longer_has_is_one_question(self):
        recipe = self._recipe()
        fresh = self._new_file(
            header="ID,Name,Parent,Is Group", rows=[f"G-{self.tag}-8,{self.n1},All Item Groups,1"]
        )  # 4 of the recipe's 5 columns: 80%
        plan = self._run_recipe(recipe, fresh)["result"]["plan"]

        asked = self._run("dry_run_import", {"plan": plan})["result"]

        gone = [q for q in asked["questions"] if q.get("what") == "gone_columns"]
        self.assertEqual(gone[0]["columns"], ["Notes"])

    def test_a_file_with_too_few_of_the_columns_matches_nothing(self):
        recipe = self._recipe()
        other = self._new_file(header="ID,Name,Parent", rows=[f"G-{self.tag}-8,{self.n1},All Item Groups"])

        ran = self._run_recipe(recipe, other)

        self.assertFalse(ran["success"])
        self.assertIn("matches a step", ran["error"])
        self.assertIsNone(frappe.db.get_value(SESSION, other, "migration_plan"))

    def test_unmatched_files_and_steps_are_listed(self):
        recipe = self._recipe()
        fresh = self._new_file()
        stray = self._opened("stray.csv", b"Colour,Size\nred,L\n")

        result = self._run_recipe(recipe, fresh, stray)["result"]

        self.assertEqual(result["unmatched_sessions"], [stray])
        self.assertEqual(result["unmatched_steps"], [])

    def test_a_step_with_no_file_is_listed(self):
        recipe = self._recipe()
        stray = self._opened("stray.csv", b"Colour,Size\nred,L\n")

        ran = self._run_recipe(recipe, stray)

        self.assertFalse(ran["success"])

    def test_the_run_imports_the_new_records_and_the_recipe_keeps_how_it_went(self):
        recipe = self._recipe()
        fresh = self._new_file()
        plan = self._run_recipe(recipe, fresh)["result"]["plan"]
        self._run(
            "dry_run_import", {"plan": plan, "answers": [{"id": "plan:failure_rule", "rule": "leave_out"}]}
        )

        self._approve(plan)

        self.assertTrue(frappe.db.exists("Item Group", self.n1))
        self.assertEqual(
            keys.resolve("Item Group", self.system, [f"G-{self.tag}-9"]), {f"G-{self.tag}-9": self.n2}
        )
        doc = frappe.get_doc(RECIPE, recipe)
        self.assertTrue(doc.last_run_result.startswith("Completed"))
        self.assertEqual(doc.last_run_site, frappe.local.site)
        self.assertTrue(doc.last_run_on)

    def test_a_field_the_site_does_not_have_is_listed_and_left_unmapped(self):
        recipe = self._recipe()
        doc = frappe.get_doc(RECIPE, recipe)
        mapping = json.loads(doc.steps[0].column_map)
        mapping["4"] = "ghost_field"  # the Notes column was mapped to a field of the other site
        doc.steps[0].column_map = json.dumps(mapping)
        doc.save()
        fresh = self._new_file()

        plan = self._run_recipe(recipe, fresh)["result"]["plan"]

        mapped = frappe.parse_json(frappe.get_doc(SESSION, fresh).template_options)["column_to_field_map"]
        self.assertNotIn("ghost_field", mapped.values())
        asked = self._run("dry_run_import", {"plan": plan})["result"]
        missing = [q for q in asked["questions"] if q.get("what") == "not_on_site"]
        self.assertEqual(missing[0]["fields"], ["ghost_field"])

    def test_a_doctype_the_site_does_not_have_leaves_its_file_unmapped(self):
        recipe = self._recipe()
        doc = frappe.get_doc(RECIPE, recipe)
        doc.steps[0].target_doctype = "Imaginary Thing"
        doc.save()
        fresh = self._new_file()

        ran = self._run_recipe(recipe, fresh)["result"]

        self.assertEqual(frappe.get_doc(SESSION, fresh).status, "File Read")
        self.assertIn("isn't on this site", json.dumps(ran["applied"]))

    def test_the_saved_value_maps_come_with_the_recipe(self):
        recipe = self._recipe()
        doc = frappe.get_doc(RECIPE, recipe)
        maps = [
            {
                "parent": "Item Group",
                "field": "parent_item_group",
                "from": "Old Root",
                "to": "All Item Groups",
            }
        ]
        doc.steps[0].transformation_rules = json.dumps(
            {**json.loads(doc.steps[0].transformation_rules or "{}"), "value_maps": maps}
        )
        doc.save()
        fresh = self._new_file()

        self._run_recipe(recipe, fresh)

        rules = frappe.parse_json(frappe.get_doc(SESSION, fresh).transformation_rules)
        self.assertEqual(rules["value_maps"], maps)

    def test_a_preset_step_finds_its_file_by_preset_and_target(self):
        recipe = self._recipe()
        doc = frappe.get_doc(RECIPE, recipe)
        doc.steps[0].preset_id = "zoho_books"
        doc.steps[0].preset_target = "Customers"
        doc.save()
        plain = self._new_file()
        made = self._new_file(name="from_preset.csv")
        other = self._new_file(name="other_target.csv")
        for name, target in ((made, "Customers"), (other, "Vendors")):
            frappe.db.set_value(
                SESSION,
                name,
                "template_options",
                json.dumps({"preset": {"id": "zoho_books", "target": target}}),
            )

        found = self._run_recipe(recipe, plain, made, other)["result"]

        self.assertEqual([m["session"] for m in found["matched"]], [made])
        self.assertEqual(sorted(found["unmatched_sessions"]), sorted([plain, other]))

    def test_an_opening_step_is_handed_back_to_be_set_up_not_planned(self):
        recipe = self._recipe()
        doc = frappe.get_doc(RECIPE, recipe)
        doc.steps[0].opening_engine = "stock"
        doc.steps[0].engine_settings = json.dumps({"warehouse": "Main"})
        doc.save()
        fresh = self._new_file()

        result = self._run_recipe(recipe, fresh)["result"]

        self.assertEqual(
            result["applied"][0]["opening"], {"engine": "stock", "settings": {"warehouse": "Main"}}
        )
        self.assertNotIn("plan", result)
        self.assertEqual(frappe.get_doc(SESSION, fresh).status, "File Read")

    def test_a_user_without_the_recipe_is_refused_and_nothing_is_written(self):
        recipe = self._recipe()
        fresh = self._new_file()
        frappe.set_user(self.make_desk_user("norun"))

        ran = self._run_recipe(recipe, fresh)

        self.assertFalse(ran["success"])
        self.assertEqual(frappe.get_doc(SESSION, fresh).status, "File Read")

    def test_a_recipe_that_does_not_exist_is_refused(self):
        fresh = self._new_file()

        ran = self._run_recipe("no-such-recipe", fresh)

        self.assertFalse(ran["success"])

    def test_a_plan_that_is_not_from_a_recipe_has_no_recipe_questions(self):
        groups = self._mapped(
            "plain.csv",
            f"Name,Parent,Is Group\n{self.n1},All Item Groups,1\n".encode(),
            "Item Group",
            {"Name": "item_group_name", "Parent": "parent_item_group", "Is Group": "is_group"},
        )
        plan = self._run("plan_migration", {"sessions": [groups]})["result"]["plan"]

        asked = self._run("dry_run_import", {"plan": plan})["result"]

        self.assertNotIn("recipe_new", [q["kind"] for q in asked["questions"]])
