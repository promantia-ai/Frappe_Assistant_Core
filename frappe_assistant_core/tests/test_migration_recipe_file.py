# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""A recipe moves between sites as a JSON file (#34420 P2.6, RC-7, RC-8, RC-10, M-D3)."""

import json
import unittest

import frappe

from frappe_assistant_core.tests.migration_recipe_case import RECIPE, DeltaRecipeCase


@unittest.skipUnless("erpnext" in frappe.get_installed_apps(), "needs ERPNext's Item Group")
class TestRecipeFile(DeltaRecipeCase):
    def setUp(self):
        super().setUp()
        self.recipe = self._delta_recipe()
        self.recipe.source_site, self.recipe.source_plan = "rehearsal.example", "IMP-7"
        self.recipe.save()

    def _export(self):
        exported = self._run("export_migration_recipe", {"recipe": self.recipe.name})
        self.assertTrue(exported["success"], exported)
        return exported["result"]

    def _content(self, result):
        return json.loads(frappe.get_doc("File", result["file_id"]).get_content())

    def _upload(self, data) -> str:
        body = data if isinstance(data, bytes) else json.dumps(data).encode()
        return self._file("recipe.json", body).file_url

    def _import(self, data):
        return self._run("import_migration_recipe", {"file_url": self._upload(data)})

    # -- export -----------------------------------------------------------------------------------

    def test_the_export_is_a_private_file_attached_to_the_recipe(self):
        result = self._export()

        file_doc = frappe.get_doc("File", result["file_id"])
        self.assertEqual(file_doc.is_private, 1)
        self.assertEqual(
            (file_doc.attached_to_doctype, file_doc.attached_to_name), (RECIPE, self.recipe.name)
        )
        self.assertTrue(file_doc.file_name.endswith(".json"))

    def test_the_file_carries_the_format_version_the_fac_version_and_the_origin(self):
        data = self._content(self._export())

        self.assertEqual(data["format_version"], 1)
        self.assertEqual(data["fac_version"], self.recipe.fac_version)
        self.assertEqual(data["origin"]["site"], "rehearsal.example")
        self.assertEqual(data["origin"]["plan"], "IMP-7")
        self.assertEqual(len(data["steps"]), 5)

    def test_a_steps_mapping_is_json_in_the_file_not_a_string_of_json(self):
        step = self._content(self._export())["steps"][1]

        self.assertEqual(step["header_signature"], ["Invoice", "Date", "Customer"])
        self.assertEqual(step["column_map"]["1"], "posting_date")

    def test_a_user_who_cannot_read_the_recipe_cannot_export_it(self):
        frappe.set_user(self.make_desk_user("noexport"))

        refused = self._run("export_migration_recipe", {"recipe": self.recipe.name})

        self.assertFalse(refused["success"])

    def test_a_recipe_that_does_not_exist_is_not_exported(self):
        self.assertFalse(self._run("export_migration_recipe", {"recipe": "nope"})["success"])

    # -- import -----------------------------------------------------------------------------------

    def test_an_exported_recipe_imports_as_the_same_recipe_with_its_origin_and_a_note(self):
        data = self._content(self._export())

        imported = self._import(data)

        self.assertTrue(imported["success"], imported)
        doc = frappe.get_doc(RECIPE, imported["result"]["recipe"])
        self.assertNotEqual(doc.name, self.recipe.name)
        self.assertEqual((doc.source_site, doc.source_plan), ("rehearsal.example", "IMP-7"))
        self.assertEqual(doc.created_by, "Administrator")
        self.assertIn("Imported by Administrator", doc.imported_note)
        self.assertIn("recipe.json", doc.imported_note)
        self.assertEqual(
            [(s.position, s.target_doctype, s.opening_engine, s.is_transaction_step) for s in doc.steps],
            [
                (s.position, s.target_doctype, s.opening_engine, s.is_transaction_step)
                for s in self.recipe.steps
            ],
        )
        self.assertEqual(json.loads(doc.steps[1].column_map), json.loads(self.recipe.steps[1].column_map))

    def test_an_imported_recipe_can_be_run(self):
        imported = self._import(self._content(self._export()))["result"]["recipe"]
        invoices = self._invoices("I1,2026-04-01,C")

        ran = self._run("run_migration_recipe", {"recipe": imported, "sessions": [invoices]})

        self.assertTrue(ran["success"], ran)

    def test_a_recipe_of_another_format_is_refused_with_both_versions_named(self):
        data = self._content(self._export())
        data["format_version"] = 2
        data["fac_version"] = "9.9.9"
        before = frappe.db.count(RECIPE)

        refused = self._import(data)

        self.assertFalse(refused["success"])
        self.assertIn("format 2", refused["error"])
        self.assertIn("9.9.9", refused["error"])
        self.assertIn("format 1", refused["error"])
        self.assertEqual(frappe.db.count(RECIPE), before)  # nothing was made

    def test_a_file_that_is_not_json_is_refused(self):
        refused = self._import(b"Name,Group\nA,B\n")

        self.assertFalse(refused["success"])
        self.assertIn("isn't JSON", refused["error"])

    def test_json_that_is_not_a_recipe_is_refused(self):
        self.assertFalse(self._import({"hello": "world"})["success"])
        self.assertFalse(self._import(["a"])["success"])

    def test_a_recipe_with_no_steps_or_too_many_is_refused(self):
        data = self._content(self._export())

        self.assertFalse(self._import({**data, "steps": []})["success"])
        self.assertFalse(self._import({**data, "steps": data["steps"][:1] * 201})["success"])

    def test_an_unknown_opening_engine_is_refused(self):
        data = self._content(self._export())
        data["steps"][0]["opening_engine"] = "rm -rf"

        refused = self._import(data)

        self.assertFalse(refused["success"])
        self.assertIn("opening engine", refused["error"])

    def test_a_step_field_that_is_not_text_is_refused(self):
        data = self._content(self._export())
        data["steps"][0]["target_doctype"] = {"x": 1}

        self.assertFalse(self._import(data)["success"])

    def test_unknown_keys_in_the_file_are_never_read(self):
        data = self._content(self._export())
        data["steps"][0]["owner"] = "attacker@example.com"
        data["owner"] = "attacker@example.com"
        data["doctype"] = "User"

        imported = self._import(data)

        self.assertTrue(imported["success"], imported)
        doc = frappe.get_doc(RECIPE, imported["result"]["recipe"])
        self.assertEqual(doc.doctype, RECIPE)
        self.assertEqual(doc.owner, "Administrator")

    def test_a_file_the_user_cannot_read_is_not_found(self):
        url = self._upload(self._content(self._export()))
        frappe.set_user(self.make_desk_user("nofile"))

        refused = self._run("import_migration_recipe", {"file_url": url})

        self.assertFalse(refused["success"])

    def test_a_user_who_cannot_create_recipes_cannot_import_one(self):
        url = self._upload(self._content(self._export()))
        before = frappe.db.count(RECIPE)
        frappe.set_user(self.make_desk_user("noimport"))

        refused = self._run("import_migration_recipe", {"file_url": url})

        self.assertFalse(refused["success"])
        self.assertEqual(frappe.db.count(RECIPE), before)

    def test_without_a_file_it_says_what_to_give(self):
        self.assertFalse(self._run("import_migration_recipe", {})["success"])
