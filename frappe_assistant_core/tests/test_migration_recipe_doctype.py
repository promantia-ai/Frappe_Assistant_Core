# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""FAC Migration Recipe DocType (#34420 P2.1, M-D1)."""

import json

import frappe

from frappe_assistant_core.tests.base_test import BaseAssistantTest

RECIPE = "FAC Migration Recipe"


class TestMigrationRecipeDocType(BaseAssistantTest):
    def setUp(self):
        super().setUp()
        frappe.set_user("Administrator")

    def _recipe(self, **step):
        step = {
            "position": 1,
            "sheet_name": "Customers",
            "target_doctype": "Customer",
            "header_signature": json.dumps(["Name", "Group"]),
            "column_map": json.dumps({"0": "customer_name"}),
            **step,
        }
        return frappe.get_doc({"doctype": RECIPE, "title": "Rehearsal", "steps": [step]}).insert()

    def test_a_saved_recipe_gets_its_origin_and_format_filled_in(self):
        doc = self._recipe()

        self.assertEqual(doc.format_version, 1)
        self.assertEqual(doc.created_by, "Administrator")
        self.assertTrue(doc.created_on)
        self.assertTrue(doc.fac_version)

    def test_a_step_keeps_its_mapping_as_json(self):
        doc = self._recipe(opening_engine="stock", is_transaction_step=1)

        step = frappe.get_doc(RECIPE, doc.name).steps[0]
        self.assertEqual(json.loads(step.column_map), {"0": "customer_name"})
        self.assertEqual(step.opening_engine, "stock")
        self.assertEqual(step.is_transaction_step, 1)

    def test_a_step_with_broken_json_is_refused_with_its_position(self):
        with self.assertRaises(frappe.ValidationError) as caught:
            self._recipe(column_map="{not json")

        self.assertIn("Step 1", str(caught.exception))

    def test_only_a_system_manager_can_open_a_recipe(self):
        doc = self._recipe()
        user = self.make_desk_user("recipe")

        frappe.set_user(user)
        self.assertFalse(frappe.has_permission(RECIPE, "read", doc=doc))
