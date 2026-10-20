# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Plan the migration order across several files (#33995): each file's session joins one plan
(a session with no file of its own, linked through parent_session), and dry_run_import on the
plan says what to import first and why, what each file still needs, and where each step stands.
The dependencies come from each file's mapped Link fields, not a written list.
"""

import unittest
from unittest.mock import patch

import frappe

from frappe_assistant_core.plugins.core import import_plan, import_signoff
from frappe_assistant_core.tests import test_import_run as run_tests
from frappe_assistant_core.tests.test_import_opening_invoices import OpeningInvoicesCase
from frappe_assistant_core.tests.test_import_opening_stock import OpeningStockCase

ITEM_GROUPS = {"Item Group Name": "item_group_name", "Parent Item Group": "parent_item_group"}
ITEMS = {"Item Code": "item_code", "Item Group": "item_group", "Default Unit of Measure": "stock_uom"}


class PlanSteps:
    """Opening sessions for files, each joined to a plan, and reading the plan."""

    def _open(self, name, header, rows, mapping=None, doctype=None, plan_with=None):
        content = ("\n".join([",".join(header), *[",".join(row) for row in rows]]) + "\n").encode()
        file_doc = frappe.get_doc(
            {"doctype": "File", "file_name": name, "content": content, "is_private": 1}
        ).insert()
        arguments = {"file_url": file_doc.file_url}
        if plan_with:
            arguments["plan_with"] = plan_with
        reply = self.registry.get_tool("start_import_session")._safe_execute(arguments)
        self.assertTrue(reply["success"], reply)
        session = reply["result"]["session_id"]
        if mapping:
            mapped = self.registry.get_tool("set_column_mapping")._safe_execute(
                {"session": session, "mapping": mapping, "doctype": doctype}
            )
            self.assertTrue(mapped["success"], mapped)
        return session, reply["result"]

    def _plan(self, plan):
        reply = self.registry.get_tool("dry_run_import")._safe_execute({"session": plan})
        self.assertTrue(reply["success"], reply)
        return reply["result"]

    def _steps(self, result):
        return {step["file"]: step for step in result["steps"]}


@unittest.skipUnless("erpnext" in frappe.get_installed_apps(), "needs ERPNext's Item and Customer")
class TestPlan(PlanSteps, run_tests.ImportRunCase):
    def _items(self, group, plan_with=None, mapping=ITEMS):
        return self._open(
            "items.csv",
            list(ITEMS),
            [[f"ITEM-{self.tag}", group, "Nos"]],
            mapping,
            "Item",
            plan_with,
        )

    def _item_groups(self, plan_with=None, names=None):
        rows = [[name, "All Item Groups"] for name in names or [f"Raw {self.tag}"]]
        return self._open("item groups.csv", list(ITEM_GROUPS), rows, ITEM_GROUPS, "Item Group", plan_with)

    # -- the plan is kept on a parent session ----------------------------------------------------

    def test_a_file_joins_the_plan_of_another_and_the_plan_is_made_once(self):
        items, _first = self._items(f"Raw {self.tag}")
        groups, joined = self._item_groups(plan_with=items)
        customers, again = self._open(
            "customers.csv",
            list(run_tests.MAPPING),
            [[f"Acme {self.tag}", self.group, "Company"]],
            plan_with=groups,
        )

        plan = joined["plan_session"]
        self.assertEqual(again["plan_session"], plan)
        for session in (items, groups, customers):
            self.assertEqual(frappe.db.get_value("FAC Import Session", session, "parent_session"), plan)
        doc = frappe.get_doc("FAC Import Session", plan)
        self.assertEqual(
            (doc.file_source, doc.source_file, doc.user), ("Migration Plan", None, "Administrator")
        )

    def test_files_opened_at_the_same_moment_share_one_plan(self):
        # An AI client opens several files in parallel, each "with" the first. The second call
        # loaded the first session before the first call linked it to the plan it made: it must
        # find that plan (reading the link under a row lock), not make another.
        items, _ = self._items(f"Raw {self.tag}")
        stale = frappe.get_doc("FAC Import Session", items)  # read before the other request's link
        _groups, first = self._item_groups(plan_with=items)

        with patch("frappe_assistant_core.plugins.core.import_mapping.load_session", return_value=stale):
            plan = import_plan.join(items)

        self.assertEqual(plan, first["plan_session"])
        self.assertEqual(
            frappe.db.count(
                "FAC Import Session",
                {"file_source": "Migration Plan", "owner": "Administrator", "name": plan},
            ),
            1,
        )
        self.assertEqual(frappe.db.count("FAC Import Session", {"parent_session": items}), 0)

    def test_a_session_of_another_user_cant_be_planned_with(self):
        items, _ = self._items(f"Raw {self.tag}")
        other = self.make_throwaway_user("plan", roles=("Sales Manager", "Item Manager", "Stock User"))
        frappe.set_user(other)
        file_doc = frappe.get_doc(
            {"doctype": "File", "file_name": "mine.csv", "content": b"Item Code\nX\n", "is_private": 1}
        ).insert()

        reply = self.registry.get_tool("start_import_session")._safe_execute(
            {"file_url": file_doc.file_url, "plan_with": items}
        )

        self.assertFalse(reply["success"], reply)
        frappe.set_user("Administrator")
        self.assertIsNone(frappe.db.get_value("FAC Import Session", items, "parent_session"))

    def test_the_sessions_a_preset_opens_from_one_file_share_a_plan(self):
        # A Tally ledgers export opens a session per target (customers, suppliers): they need an order
        from frappe_assistant_core.tests.test_apply_import_preset import SHIPPED, xlsx

        rows, _expected = SHIPPED["tally_prime.ledgers"]
        file_doc = frappe.get_doc(
            {
                "doctype": "File",
                "file_name": f"ledgers-{self.tag}.xlsx",
                "content": xlsx(rows),
                "is_private": 1,
            }
        ).insert()

        reply = self.registry.get_tool("apply_import_preset")._safe_execute({"file_id": file_doc.name})

        self.assertTrue(reply["success"], reply)
        result = reply["result"]
        opened = [s["session_id"] for s in result["sessions"]]
        self.assertGreater(len(opened), 1, result)
        for session in opened:
            self.assertEqual(
                frappe.db.get_value("FAC Import Session", session, "parent_session"), result["plan_session"]
            )
        self.assertEqual(len(self._plan(result["plan_session"])["steps"]), len(opened))

    # -- the order, from the Link fields ---------------------------------------------------------

    def test_items_come_after_the_item_groups_they_link_to_whatever_order_they_came_in(self):
        items, _ = self._items(f"Raw {self.tag}")
        _groups, joined = self._item_groups(plan_with=items)

        result = self._plan(joined["plan_session"])

        self.assertEqual([step["file"] for step in result["steps"]], ["item groups.csv", "items.csv"])
        steps = self._steps(result)
        self.assertEqual(steps["items.csv"]["after"], [1])
        self.assertIn("Item Group", steps["items.csv"]["reason"])
        self.assertIn("Item Group", steps["item groups.csv"]["reason"])

    def test_a_link_column_not_imported_makes_no_dependency(self):
        mapping = {**ITEMS, "Item Group": "Don't Import"}
        items, _ = self._items(f"Raw {self.tag}", mapping=mapping)
        _groups, joined = self._item_groups(plan_with=items)

        steps = self._steps(self._plan(joined["plan_session"]))

        self.assertEqual(steps["items.csv"]["after"], [])

    def test_masters_come_before_transactions_and_customers_need_their_group(self):
        groups, _ = self._open(
            "customer groups.csv",
            ["Customer Group Name", "Parent Customer Group"],
            [[f"Wholesale {self.tag}", "All Customer Groups"]],
            {"Customer Group Name": "customer_group_name", "Parent Customer Group": "parent_customer_group"},
            "Customer Group",
        )
        _customers, joined = self._open(
            "customers.csv",
            list(run_tests.MAPPING),
            [[f"Acme {self.tag}", f"Wholesale {self.tag}", "Company"]],
            run_tests.MAPPING,
            "Customer",
            plan_with=groups,
        )

        result = self._plan(joined["plan_session"])

        self.assertEqual([s["file"] for s in result["steps"]], ["customer groups.csv", "customers.csv"])
        self.assertEqual(self._steps(result)["customers.csv"]["after"], [1])

    # -- what isn't in yet -----------------------------------------------------------------------

    def test_a_record_no_file_brings_and_erpnext_lacks_is_named(self):
        items, _ = self._items(f"Ghost {self.tag}")
        _groups, joined = self._item_groups(plan_with=items)
        self.registry.get_tool("dry_run_import")._safe_execute({"session": items})

        steps = self._steps(self._plan(joined["plan_session"]))

        [missing] = steps["items.csv"]["missing"]
        self.assertEqual((missing["doctype"], missing["count"], missing["in_step"]), ("Item Group", 1, None))
        self.assertIn(f"Ghost {self.tag}", missing["values"])

    def test_a_record_a_file_of_the_plan_brings_points_to_its_step(self):
        items, _ = self._items(f"Raw {self.tag}")
        _groups, joined = self._item_groups(plan_with=items)
        self.registry.get_tool("dry_run_import")._safe_execute({"session": items})

        result = self._plan(joined["plan_session"])

        [missing] = self._steps(result)["items.csv"]["missing"]
        self.assertEqual((missing["doctype"], missing["in_step"]), ("Item Group", 1))
        self.assertEqual(result["next"]["step"], 1)

    # -- where each step stands ------------------------------------------------------------------

    def test_steps_are_done_or_pending_and_the_next_is_the_first_ready_one(self):
        items, _ = self._items(f"Raw {self.tag}")
        groups, joined = self._item_groups(plan_with=items)
        checked = self.registry.get_tool("dry_run_import")._safe_execute({"session": groups})["result"]
        self.assertEqual(checked["status"], "Ready to Import", checked)
        with self._each_row_commits():
            imported = self.registry.get_tool("start_import")._safe_execute(
                {"session": groups, "doctype": "Item Group", "rows": 1, "import_type": run_tests.INSERT}
            )
        self.assertTrue(imported["success"], imported)

        result = self._plan(joined["plan_session"])

        steps = self._steps(result)
        self.assertEqual(
            (steps["item groups.csv"]["state"], steps["items.csv"]["state"]), ("done", "pending")
        )
        self.assertEqual(result["next"]["step"], 2)
        self.assertIn("dry_run_import", result["next"]["do"])
        self.assertEqual((result["done"], result["pending"]), (1, 1))

    def test_reading_the_plan_writes_nothing(self):
        items, _ = self._items(f"Raw {self.tag}")
        _groups, joined = self._item_groups(plan_with=items)
        plan = joined["plan_session"]
        before = frappe.db.get_value("FAC Import Session", plan, "modified")

        self._plan(plan)

        self.assertEqual(frappe.db.get_value("FAC Import Session", plan, "modified"), before)
        self.assertEqual(frappe.db.count("FAC Import Session", {"parent_session": plan}), 2)

    def test_the_plan_is_not_a_file_in_the_sign_off(self):
        items, _ = self._items(f"Raw {self.tag}")
        _groups, joined = self._item_groups(plan_with=items)

        names = [s.name for s in import_signoff.sessions_of()]

        self.assertNotIn(joined["plan_session"], names)
        self.assertIn(items, names)

    def test_a_plan_cant_be_imported_or_mapped(self):
        items, _ = self._items(f"Raw {self.tag}")
        _groups, joined = self._item_groups(plan_with=items)
        plan = joined["plan_session"]

        for tool, arguments in (
            ("set_column_mapping", {"session": plan, "mapping": ITEMS, "doctype": "Item"}),
            (
                "start_import",
                {"session": plan, "doctype": "Item", "rows": 1, "import_type": run_tests.INSERT},
            ),
            ("dry_run_import", {"session": plan, "fixes": [{"group": "g1", "action": "leave_out"}]}),
        ):
            reply = self.registry.get_tool(tool)._safe_execute(arguments)
            self.assertFalse(reply["success"], (tool, reply))
            self.assertIn("plan", reply["error"].lower(), tool)


class TestPlanWithOpeningStock(PlanSteps, OpeningStockCase):
    def test_opening_stock_waits_for_the_items_it_counts(self):
        # Opening stock's rows link to Item and Warehouse: the Items file goes first
        stock = self._session([f"{self.item},{self.stores},5,10"])
        self.assertTrue(self._setup(stock)["success"])
        _items, joined = self._open(
            "items.csv",
            list(ITEMS),
            [[f"NEW-{self.tag}", "Products", "Nos"]],
            ITEMS,
            "Item",
            plan_with=stock,
        )

        result = self._plan(joined["plan_session"])

        self.assertEqual([s["imports"] for s in result["steps"]], ["Item", "Opening Stock"])
        opening = result["steps"][1]
        self.assertEqual(opening["after"], [1])
        self.assertEqual(opening["kind"], "opening")


class TestPlanWithOpeningInvoices(PlanSteps, OpeningInvoicesCase):
    def test_opening_invoices_wait_for_the_customers_they_are_owed_by(self):
        # Opening invoices keep the party column in the session's own column map
        receivables = self._session([f"{self.customer},INV-1,{self.date},{self.date},100"])
        self.assertTrue(self._setup(receivables)["success"])
        _customers, joined = self._open(
            "customers.csv",
            list(run_tests.MAPPING),
            [[f"New {self.tag}", "Commercial", "Company"]],
            run_tests.MAPPING,
            "Customer",
            plan_with=receivables,
        )

        result = self._plan(joined["plan_session"])

        self.assertEqual([s["imports"] for s in result["steps"]], ["Customer", "Opening Sales Invoice"])
        self.assertEqual(result["steps"][1]["after"], [1])
        self.assertIn("Customer", result["steps"][1]["reason"])
