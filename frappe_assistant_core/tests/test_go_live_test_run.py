# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
The go-live test run (#34424): buying then selling end to end on the site's own setup, as the user,
every document rolled back. Run through the tool as FACO runs it; nothing is mocked.
"""

import unittest

import frappe

from frappe_assistant_core.core.tool_registry import get_tool_registry
from frappe_assistant_core.tests.base_test import BaseAssistantTest, item_hsn_fields
from frappe_assistant_core.utils import go_live_test_run as test_run

#: (DocType, child DocType) of every document the run makes
MADE = (
    ("Material Request", "Material Request Item"),
    ("Purchase Order", "Purchase Order Item"),
    ("Purchase Receipt", "Purchase Receipt Item"),
    ("Purchase Invoice", "Purchase Invoice Item"),
    ("Quotation", "Quotation Item"),
    ("Sales Order", "Sales Order Item"),
    ("Delivery Note", "Delivery Note Item"),
    ("Sales Invoice", "Sales Invoice Item"),
)


@unittest.skipUnless("erpnext" in frappe.get_installed_apps(), "needs ERPNext")
class TestGoLiveTestRun(BaseAssistantTest):
    """An item bought and sold, a customer and a supplier, made for each test on the site's first company."""

    def setUp(self):
        super().setUp()
        frappe.set_user("Administrator")
        self.tag = frappe.generate_hash(length=6)
        self.company = frappe.get_all("Company", pluck="name", order_by="creation", limit=1)[0]
        self.warehouse = frappe.db.get_value(
            "Warehouse", {"company": self.company, "is_group": 0, "disabled": 0}
        )
        if not self.warehouse:
            self.skipTest("the test company has no warehouse")
        self.item = (
            frappe.get_doc(
                {
                    "doctype": "Item",
                    "item_code": f"GL-ITEM-{self.tag}",
                    "item_group": frappe.db.get_value("Item Group", {"is_group": 0}),
                    "stock_uom": "Nos",
                    "is_stock_item": 1,
                    "is_sales_item": 1,
                    "is_purchase_item": 1,
                    **item_hsn_fields(),
                }
            )
            .insert()
            .name
        )
        self.customer = self._party("Customer", "customer_name", "customer_group", "Customer Group")
        self.supplier = self._party("Supplier", "supplier_name", "supplier_group", "Supplier Group")

    def _party(self, doctype, name_field, group_field, group_doctype):
        # the site's default group can be a group node, which a party can't belong to
        return (
            frappe.get_doc(
                {
                    "doctype": doctype,
                    name_field: f"GL {doctype} {self.tag}",
                    group_field: frappe.db.get_value(group_doctype, {"is_group": 0}),
                }
            )
            .insert()
            .name
        )

    def _run(self, **arguments):
        arguments = {
            "company": self.company,
            "item": self.item,
            "customer": self.customer,
            "supplier": self.supplier,
            "warehouse": self.warehouse,
            **arguments,
        }
        return get_tool_registry().get_tool("go_live_test_run")._safe_execute(arguments)

    def _left(self):
        """Every record of the run's item, by DocType: all zero when nothing was kept."""
        left = {doctype: frappe.db.count(child, {"item_code": self.item}) for doctype, child in MADE}
        left["Item Price"] = frappe.db.count("Item Price", {"item_code": self.item})
        left["Bin"] = frappe.db.count("Bin", {"item_code": self.item})
        left["Stock Ledger Entry"] = frappe.db.count("Stock Ledger Entry", {"item_code": self.item})
        left["Payment Entry"] = frappe.db.count(
            "Payment Entry", {"party": ["in", [self.customer, self.supplier]]}
        )
        left["GL Entry"] = frappe.db.count("GL Entry", {"party": ["in", [self.customer, self.supplier]]})
        return left

    def test_both_flows_pass_end_to_end_and_nothing_is_kept(self):
        reply = self._run()
        self.assertTrue(reply["success"], reply)
        result = reply["result"]
        steps = [(s["flow"], s["step"], s["status"]) for s in result["steps"]]
        self.assertEqual(
            steps,
            [
                ("buy", d, "passed")
                for d in (
                    "Material Request",
                    "Purchase Order",
                    "Purchase Receipt",
                    "Purchase Invoice",
                    "Payment Entry",
                )
            ]
            + [
                ("sell", d, "passed")
                for d in ("Quotation", "Sales Order", "Delivery Note", "Sales Invoice", "Payment Entry")
            ],
            result["steps"],
        )
        self.assertTrue(result["ready"])
        self.assertEqual(set(self._left().values()), {0}, self._left())
        self.assertFalse(getattr(frappe.local, "fac_holding_back", 0))

    def test_without_a_price_the_run_uses_100_and_says_so(self):
        # both flows: the new item has no stock until the buying flow receives it
        result = self._run()["result"]
        self.assertTrue(result["ready"], result["steps"])
        self.assertEqual(result["steps"][-1]["amount"], 100)
        self.assertTrue(result["notes"])
        self.assertTrue(all("no price" in note for note in result["notes"]), result["notes"])

    def test_a_given_rate_is_used(self):
        result = self._run(flows=["buy"], rate=250, qty=2)["result"]
        self.assertTrue(result["ready"], result["steps"])
        self.assertEqual(result["steps"][-1]["amount"], 500)
        self.assertFalse(result["notes"])

    def test_a_failing_step_says_why_and_the_rest_of_its_flow_is_not_run(self):
        # A supplier the client put on hold: their purchase orders are refused
        frappe.db.set_value("Supplier", self.supplier, {"on_hold": 1, "hold_type": "All"})
        result = self._run()["result"]
        self.assertIn("steps", result, result)
        buy = {s["step"]: s for s in result["steps"] if s["flow"] == "buy"}
        self.assertEqual(buy["Material Request"]["status"], "passed")
        self.assertEqual(buy["Purchase Order"]["status"], "failed")
        self.assertIn(self.supplier, buy["Purchase Order"]["message"])
        for step in ("Purchase Receipt", "Purchase Invoice", "Payment Entry"):
            self.assertEqual(buy[step]["status"], "not run")
        self.assertFalse(result["ready"])
        self.assertEqual(set(self._left().values()), {0}, self._left())

    def test_a_user_who_may_not_read_the_company_is_refused_before_anything_runs(self):
        # the run picks and names records as the user: one they may not read is not found to them
        user = self.make_desk_user("golive")
        self.assert_cannot(user, "read", "Company", self.company)
        frappe.set_user(user)
        reply = self._run(flows=["sell"])
        frappe.set_user("Administrator")
        self.assertFalse(reply["success"], reply)
        self.assertIn("not found", reply["error"])
        self.assertEqual(set(self._left().values()), {0}, self._left())

    def test_an_item_that_is_not_there_is_refused_before_anything_runs(self):
        reply = self._run(item=f"No Such Item {self.tag}")
        self.assertFalse(reply["success"])
        self.assertIn("not found", reply["error"])

    def test_without_item_customer_or_supplier_it_takes_the_sites_first_ones(self):
        used = test_run.setup({"company": self.company})
        for key in ("item", "customer", "supplier", "warehouse"):
            self.assertTrue(used[key], key)
        self.assertEqual(used["flows"], ["buy", "sell"])
