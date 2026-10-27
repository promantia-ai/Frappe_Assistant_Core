# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""FAC Setup Plan DocType (#34421 P3.2, S-D1)."""

import json
import unittest

import frappe

from frappe_assistant_core.tests.base_test import BaseAssistantTest

PLAN = "FAC Setup Plan"


@unittest.skipUnless("erpnext" in frappe.get_installed_apps(), "needs ERPNext's Company")
class TestSetupPlanDocType(BaseAssistantTest):
    def setUp(self):
        super().setUp()
        frappe.set_user("Administrator")
        self.company = frappe.get_all("Company", pluck="name", limit=1)[0]

    def _plan(self, **extra):
        return frappe.get_doc(
            {
                "doctype": PLAN,
                "title": "Setup",
                "company": self.company,
                "items": [
                    {
                        "area": "stock",
                        "decision": "FIFO",
                        "target_doctype": "Stock Settings",
                        "target_name": "Stock Settings",
                        "values": json.dumps({"valuation_method": "FIFO"}),
                    }
                ],
                **extra,
            }
        ).insert()

    def test_a_new_plan_is_a_draft_that_stops_at_the_first_failure(self):
        plan = self._plan()

        self.assertEqual((plan.status, plan.failure_rule), ("Draft", "stop"))
        self.assertRegex(plan.name, r"^FAC-SETUP-\d{4}-\d{5}$")

    def test_an_item_starts_pending_and_keeps_its_values_as_json(self):
        plan = frappe.get_doc(PLAN, self._plan().name)

        item = plan.items[0]
        self.assertEqual((item.state, item.already_set, item.is_customisation), ("Pending", 0, 0))
        self.assertEqual(json.loads(item.values), {"valuation_method": "FIFO"})

    def test_fit_gap_items_are_kept_apart_from_the_plan_items(self):
        plan = self._plan(
            gaps=[{"requirement": "Three-step approval of purchase orders", "suggestion": "A workflow"}]
        )

        self.assertEqual(len(frappe.get_doc(PLAN, plan.name).gaps), 1)
        self.assertEqual(len(frappe.get_doc(PLAN, plan.name).items), 1)

    def test_a_draft_plan_can_be_changed(self):
        plan = self._plan()
        plan.items[0].decision = "Moving average"
        plan.save()

        self.assertEqual(frappe.get_doc(PLAN, plan.name).items[0].decision, "Moving average")

    def test_an_approved_plan_can_not_have_its_items_changed(self):
        plan = self._plan()
        plan.status = "Approved"
        plan.save()
        plan.items[0].decision = "Moving average"

        with self.assertRaises(frappe.ValidationError) as caught:
            plan.save()

        self.assertIn("can't change", str(caught.exception))

    def test_a_run_may_write_the_state_and_message_of_an_item_of_an_approved_plan(self):
        plan = self._plan()
        plan.status = "Running"
        plan.save()
        plan.items[0].state = "Done"
        plan.items[0].message = "Set."
        plan.save()

        self.assertEqual(frappe.get_doc(PLAN, plan.name).items[0].state, "Done")

    def test_only_a_system_manager_can_open_a_plan(self):
        plan = self._plan()
        frappe.set_user(self.make_desk_user("setupplan"))

        self.assertFalse(frappe.has_permission(PLAN, "read", doc=plan))
