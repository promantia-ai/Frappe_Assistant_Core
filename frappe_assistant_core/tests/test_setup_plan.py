# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Discovery, building and changing a setup plan (#34421 P3.3, SP-1 to SP-3, S-D4, S-D5)."""

import json
import unittest

import frappe

from frappe_assistant_core.tests.test_import_run import ImportRunCase
from frappe_assistant_core.utils import compat, setup_plan

PLAN = "FAC Setup Plan"


@unittest.skipUnless("erpnext" in frappe.get_installed_apps(), "needs ERPNext's Company and Stock Settings")
class TestSetupPlan(ImportRunCase):
    def setUp(self):
        super().setUp()
        self.company = frappe.get_all("Company", pluck="name", limit=1)[0]
        self.abbr = frappe.db.get_value("Company", self.company, "abbr")

    def _answers(self, **extra):
        return {
            "valuation_method": "FIFO",
            "allow_negative_stock": 0,
            "warehouses": [{"name": f"Chennai {self.tag}"}],
            "item_groups": [{"name": f"Fabric {self.tag}"}],
            "uoms": [f"Bolt {self.tag}"],
            "payment_terms": [{"name": f"Net 30 {self.tag}", "due_days": 30}],
            "price_lists": [{"name": f"Wholesale {self.tag}"}],
            "fit_gap": [{"requirement": "Three-step PO approval", "suggestion": "A workflow"}],
            **extra,
        }

    def _save(self, **extra):
        saved = self._run(
            "setup_plan", {"action": "save", "company": self.company, "answers": self._answers(**extra)}
        )
        self.assertTrue(saved["success"], saved)
        return saved["result"]

    # -- discovery ---------------------------------------------------------------------------------

    def test_discovery_lists_the_areas_in_order(self):
        started = self._run("setup_plan", {"action": "discover", "company": self.company})["result"]

        areas = [a["area"] for a in started["areas"]]
        expected = [k for k in setup_plan.AREA_KEYS if k != "gst" or compat.has_india_compliance()]
        self.assertEqual(areas, expected)
        self.assertEqual(started["company"], self.company)

    def test_an_area_the_site_already_answers_is_shown_set_and_not_asked(self):
        areas = {
            a["area"]: a
            for a in self._run("setup_plan", {"action": "discover", "company": self.company})["result"][
                "areas"
            ]
        }

        self.assertTrue(areas["company"]["set"])
        self.assertNotIn("questions", areas["company"])
        self.assertIn(self.company, areas["company"]["summary"])

    def test_an_area_not_set_comes_with_plain_word_questions(self):
        areas = {
            a["area"]: a
            for a in self._run("setup_plan", {"action": "discover", "company": self.company})["result"][
                "areas"
            ]
        }

        self.assertFalse(areas["naming"]["set"])
        self.assertTrue(areas["naming"]["questions"])
        self.assertEqual(areas["chart_of_accounts"]["hand_off"], "import_chart_of_accounts")

    def test_gst_is_asked_only_where_india_compliance_is_installed(self):
        areas = [
            a["area"]
            for a in self._run("setup_plan", {"action": "discover", "company": self.company})["result"][
                "areas"
            ]
        ]

        self.assertEqual("gst" in areas, compat.has_india_compliance())

    def test_without_a_company_it_uses_the_default_or_asks(self):
        started = self._run("setup_plan", {"action": "discover"})

        self.assertTrue(started["success"] or "company" in started["error"].lower(), started)

    def test_a_company_that_does_not_exist_is_refused(self):
        self.assertFalse(
            self._run("setup_plan", {"action": "discover", "company": "No Such Company"})["success"]
        )

    # -- building ----------------------------------------------------------------------------------

    def test_the_answers_become_items_by_area_and_a_draft_plan(self):
        plan = self._save()

        doc = frappe.get_doc(PLAN, plan["plan"])
        self.assertEqual(doc.status, "Draft")
        areas = [i["area"] for i in plan["items"]]
        self.assertEqual(areas, sorted(areas, key=setup_plan.AREA_KEYS.index))
        targets = {(i.target_doctype, i.target_name) for i in doc.items}
        self.assertIn(("Warehouse", f"Chennai {self.tag} - {self.abbr}"), targets)
        self.assertIn(("Item Group", f"Fabric {self.tag}"), targets)
        self.assertIn(("Stock Settings", "Stock Settings"), targets)

    def test_nothing_is_written_to_the_site(self):
        self._save()

        self.assertFalse(frappe.db.exists("Warehouse", f"Chennai {self.tag} - {self.abbr}"))
        self.assertFalse(frappe.db.exists("Item Group", f"Fabric {self.tag}"))
        self.assertFalse(frappe.db.exists("UOM", f"Bolt {self.tag}"))

    def test_fit_gap_items_are_listed_apart(self):
        plan = self._save()

        self.assertEqual(plan["fit_gap"][0]["requirement"], "Three-step PO approval")
        self.assertNotIn("Three-step", json.dumps(plan["items"]))

    def test_an_item_the_site_already_has_is_marked_already_set(self):
        name = f"Existing {self.tag}"
        frappe.get_doc({"doctype": "UOM", "uom_name": name}).insert()

        plan = self._save(uoms=[name])

        item = next(i for i in plan["items"] if i["target"] == f"UOM: {name}")
        self.assertTrue(item["already_set"])
        new = next(i for i in plan["items"] if i["target"].startswith("Warehouse"))
        self.assertFalse(new["already_set"])

    def test_a_setting_already_as_planned_is_already_set(self):
        current = frappe.db.get_single_value("Stock Settings", "valuation_method")

        plan = self._save(valuation_method=current)

        item = next(i for i in plan["items"] if i["target"] == "Stock Settings: Stock Settings")
        self.assertTrue(item["already_set"])

    def test_a_valuation_method_erpnext_does_not_have_is_refused(self):
        refused = self._run(
            "setup_plan",
            {"action": "save", "company": self.company, "answers": self._answers(valuation_method="Magic")},
        )

        self.assertFalse(refused["success"])
        self.assertIn("Magic", refused["error"])

    def test_an_item_for_a_field_this_site_does_not_have_is_left_out_and_said(self):
        plan = self._save(
            items=[
                {
                    "area": "stock",
                    "decision": "x",
                    "target_doctype": "Stock Settings",
                    "values": {"no_such_field": 1},
                }
            ]
        )

        self.assertTrue(any("no_such_field" in p for p in plan["problems"]), plan["problems"])
        self.assertFalse(any("no_such_field" in json.dumps(i) for i in plan["items"]))

    def test_an_item_for_a_doctype_this_site_does_not_have_is_left_out_and_said(self):
        plan = self._save(
            items=[{"area": "stock", "decision": "x", "target_doctype": "Imaginary", "values": {"a": 1}}]
        )

        self.assertTrue(any("Imaginary" in p for p in plan["problems"]))

    def test_an_area_that_is_not_one_of_the_interviews_is_refused(self):
        refused = self._run(
            "setup_plan",
            {
                "action": "save",
                "company": self.company,
                "answers": {
                    "items": [{"area": "weather", "target_doctype": "UOM", "values": {"uom_name": "x"}}]
                },
            },
        )

        self.assertFalse(refused["success"])

    def test_a_customisation_item_needs_its_record_created_first_and_says_so(self):
        plan = self._save(
            items=[
                {
                    "area": "items",
                    "decision": "A field for the fabric width",
                    "target_doctype": "Custom Field",
                    "values": {"dt": "Item", "fieldname": f"width_{self.tag}", "fieldtype": "Data"},
                }
            ]
        )

        item = next(i for i in plan["items"] if i["customisation"])
        self.assertIsNone(item["created_record"])
        self.assertTrue(any("create_document" in p for p in plan["problems"]), plan["problems"])

    def test_the_financial_year_must_end_after_it_starts(self):
        refused = self._run(
            "setup_plan",
            {
                "action": "save",
                "company": self.company,
                "answers": {"fiscal_year": {"from": "2027-04-01", "to": "2027-03-31"}},
            },
        )

        self.assertFalse(refused["success"])

    def test_answers_that_make_nothing_are_not_saved(self):
        before = frappe.db.count(PLAN)

        refused = self._run("setup_plan", {"action": "save", "company": self.company, "answers": {}})

        self.assertFalse(refused["success"])
        self.assertEqual(frappe.db.count(PLAN), before)

    def test_a_user_who_cannot_create_plans_cannot_save_one(self):
        frappe.set_user(self.make_desk_user("noplan"))

        refused = self._run(
            "setup_plan", {"action": "save", "company": self.company, "answers": self._answers()}
        )

        self.assertFalse(refused["success"])

    # -- changing a draft --------------------------------------------------------------------------

    def _number(self, plan, target):
        return next(i["number"] for i in plan["items"] if i["target"] == target)

    def test_a_decision_changed_in_chat_changes_the_item_and_writes_nothing(self):
        plan = self._save()
        number = self._number(plan, "Stock Settings: Stock Settings")

        changed = self._run(
            "setup_plan",
            {
                "action": "update",
                "plan": plan["plan"],
                "items": {
                    str(number): {
                        "decision": "Value stock by Moving Average",
                        "values": {"valuation_method": "Moving Average"},
                    }
                },
            },
        )["result"]

        item = next(i for i in changed["items"] if i["number"] == number)
        self.assertEqual(item["decision"], "Value stock by Moving Average")
        doc = frappe.get_doc(PLAN, plan["plan"])
        self.assertEqual(json.loads(doc.items[number - 1].values)["valuation_method"], "Moving Average")
        self.assertNotEqual(
            frappe.db.get_single_value("Stock Settings", "valuation_method"), "Moving Average"
        )

    def test_an_item_can_be_removed_and_one_added(self):
        plan = self._save()
        number = self._number(plan, f"UOM: Bolt {self.tag}")

        changed = self._run(
            "setup_plan",
            {
                "action": "update",
                "plan": plan["plan"],
                "remove": [number],
                "add": [
                    {
                        "area": "items",
                        "decision": "Unit Roll",
                        "target_doctype": "UOM",
                        "target_name": f"Roll {self.tag}",
                        "values": {"uom_name": f"Roll {self.tag}"},
                    }
                ],
            },
        )["result"]

        targets = [i["target"] for i in changed["items"]]
        self.assertNotIn(f"UOM: Bolt {self.tag}", targets)
        self.assertIn(f"UOM: Roll {self.tag}", targets)

    def test_a_change_to_an_item_that_does_not_exist_is_refused(self):
        plan = self._save()

        refused = self._run(
            "setup_plan", {"action": "update", "plan": plan["plan"], "items": {"99": {"decision": "x"}}}
        )

        self.assertFalse(refused["success"])
        self.assertIn("no item 99", refused["error"])

    def test_a_change_to_a_field_the_site_does_not_have_is_refused(self):
        plan = self._save()
        number = self._number(plan, "Stock Settings: Stock Settings")

        refused = self._run(
            "setup_plan",
            {"action": "update", "plan": plan["plan"], "items": {str(number): {"values": {"nope": 1}}}},
        )

        self.assertFalse(refused["success"])

    def test_an_approved_plan_cannot_be_changed(self):
        plan = self._save()
        frappe.db.set_value(PLAN, plan["plan"], "status", "Approved")

        refused = self._run(
            "setup_plan", {"action": "update", "plan": plan["plan"], "failure_rule": "continue"}
        )

        self.assertFalse(refused["success"])
        self.assertIn("only a Draft", refused["error"])

    def test_the_failure_rule_can_be_changed_on_a_draft(self):
        plan = self._save()

        changed = self._run(
            "setup_plan", {"action": "update", "plan": plan["plan"], "failure_rule": "continue"}
        )["result"]

        self.assertEqual(changed["failure_rule"], "continue")

    def test_a_plan_that_does_not_exist_is_refused(self):
        self.assertFalse(
            self._run(
                "setup_plan", {"action": "update", "plan": "FAC-SETUP-0000-00000", "failure_rule": "stop"}
            )["success"]
        )
