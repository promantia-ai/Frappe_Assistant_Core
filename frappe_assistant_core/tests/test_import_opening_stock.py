"""
Opening stock (#33996): the old system's closing stock becomes an Opening Stock Stock
Reconciliation at the cut-off date. Every row is checked first; nothing is written until the
user approves; totals per warehouse are compared with the file's after.
"""

import unittest

import frappe
from frappe.utils import nowdate

from frappe_assistant_core.core.tool_registry import get_tool_registry
from frappe_assistant_core.plugins.core import import_opening_stock as opening_stock
from frappe_assistant_core.tests.base_test import BaseAssistantTest

HEADER = "Item,Warehouse,Qty,Rate"


@unittest.skipUnless("erpnext" in frappe.get_installed_apps(), "needs ERPNext's Stock Reconciliation")
class OpeningStockCase(BaseAssistantTest):
    """A company, two warehouses and items of every kind, made for each test."""

    def setUp(self):
        super().setUp()
        frappe.set_user("Administrator")
        self.registry = get_tool_registry()
        self.tag = frappe.generate_hash(length=6)
        self.company = frappe.get_all("Company", pluck="name", order_by="creation", limit=1)[0]
        self.abbr = frappe.db.get_value("Company", self.company, "abbr")
        self.files_before = set(frappe.get_all("File", pluck="name"))
        self.temporary = self._temporary_account()
        self.stores = self._warehouse(f"OS Stores {self.tag}")
        self.shop = self._warehouse(f"OS Shop {self.tag}")
        self.item = self._item(f"OS-ITEM-{self.tag}")
        self.item2 = self._item(f"OS-ITEM2-{self.tag}")
        self.date = nowdate()

    def tearDown(self):
        frappe.set_user("Administrator")
        for name in set(frappe.get_all("File", pluck="name")) - self.files_before:
            frappe.delete_doc("File", name, force=True, ignore_permissions=True)
        super().tearDown()

    # -- fixtures ------------------------------------------------------------------------

    def _temporary_account(self):
        name = frappe.db.get_value(
            "Account", {"company": self.company, "account_type": "Temporary", "is_group": 0}
        )
        if name:
            return name
        parent = frappe.db.get_value(
            "Account", {"company": self.company, "root_type": "Asset", "is_group": 1}, order_by="lft"
        )
        return (
            frappe.get_doc(
                {
                    "doctype": "Account",
                    "account_name": "Temporary Opening",
                    "company": self.company,
                    "parent_account": parent,
                    "account_type": "Temporary",
                    "is_group": 0,
                }
            )
            .insert()
            .name
        )

    def _warehouse(self, name, is_group=0):
        return (
            frappe.get_doc(
                {
                    "doctype": "Warehouse",
                    "warehouse_name": name,
                    "company": self.company,
                    "is_group": is_group,
                }
            )
            .insert()
            .name
        )

    def _item(self, code, **flags):
        doc = {
            "doctype": "Item",
            "item_code": code,
            "item_name": code,
            "item_group": frappe.db.get_value("Item Group", {"is_group": 0}) or "All Item Groups",
            "stock_uom": "Nos",
            "is_stock_item": 1,
            **flags,
        }
        return frappe.get_doc(doc).insert().name

    def _run(self, tool_name, arguments):
        return self.registry.get_tool(tool_name)._safe_execute(arguments)

    def _file(self, lines, header=HEADER):
        content = ("\n".join([header, *lines]) + "\n").encode()
        return frappe.get_doc(
            {"doctype": "File", "file_name": f"stock-{self.tag}.csv", "content": content, "is_private": 1}
        ).insert()

    def _session(self, lines, header=HEADER):
        started = self._run("start_import_session", {"file_id": self._file(lines, header).name})
        self.assertTrue(started["success"], started)
        return started["result"]["session_id"]

    def _setup(self, session, **changes):
        arguments = {
            "session": session,
            "company": self.company,
            "posting_date": self.date,
            "columns": {
                "Item": "item_code",
                "Warehouse": "warehouse",
                "Qty": "qty",
                "Rate": "valuation_rate",
            },
            **changes,
        }
        return self._run("set_opening_stock", arguments)

    def _doc(self, session):
        return frappe.get_doc("FAC Import Session", session)


class TestOpeningStockSetup(OpeningStockCase):
    def test_tool_is_registered_and_raises_no_approval_card(self):
        from frappe_assistant_core.plugins.core.plugin import CorePlugin
        from frappe_assistant_core.utils.tool_category_detector import detect_tool_category

        self.assertIn("set_opening_stock", CorePlugin().get_tools())
        self.assertEqual(detect_tool_category(self.registry.get_tool("set_opening_stock")), "read_only")

    def test_setup_stores_the_settings_and_maps_the_columns(self):
        session = self._session([f"{self.item},{self.stores},5,10"])

        response = self._setup(session)

        self.assertTrue(response["success"], response)
        doc = self._doc(session)
        self.assertEqual((doc.status, doc.target_doctype), ("Mapped", "Stock Reconciliation"))
        options = frappe.parse_json(doc.template_options)
        self.assertEqual(
            options["column_to_field_map"],
            {"0": "items.item_code", "1": "items.warehouse", "2": "items.qty", "3": "items.valuation_rate"},
        )
        stock = options["opening_stock"]
        self.assertEqual(
            (stock["company"], stock["posting_date"], stock["posting_time"], stock["difference_account"]),
            (self.company, self.date, "23:59:59", self.temporary),
        )

    def test_a_date_outside_every_fiscal_year_is_refused(self):
        session = self._session([f"{self.item},{self.stores},5,10"])

        response = self._setup(session, posting_date="1901-01-01")

        self.assertFalse(response["success"], response)
        self.assertIn("fiscal year", frappe.as_json(response))
        self.assertEqual(self._doc(session).status, "File Read")

    def test_a_profit_and_loss_difference_account_is_refused(self):
        session = self._session([f"{self.item},{self.stores},5,10"])
        expense = frappe.db.get_value(
            "Account", {"company": self.company, "root_type": "Expense", "is_group": 0}
        )

        response = self._setup(session, difference_account=expense)

        self.assertFalse(response["success"], response)
        self.assertIn("Balance Sheet", frappe.as_json(response))

    def test_an_unknown_column_or_field_is_refused(self):
        session = self._session([f"{self.item},{self.stores},5,10"])

        response = self._setup(session, columns={"Item": "item_code", "Nope": "qty", "Qty": "price"})

        text = frappe.as_json(response)
        self.assertIn("Nope", text)
        self.assertIn("price", text)

    def test_item_warehouse_and_qty_columns_are_required(self):
        session = self._session([f"{self.item},{self.stores},5,10"])

        response = self._setup(session, columns={"Item": "item_code", "Qty": "qty"})

        self.assertIn("warehouse", frappe.as_json(response))

    def test_a_user_who_cannot_submit_stock_reconciliation_is_refused(self):
        frappe.set_user(self.make_throwaway_user("clerk", roles=("Sales User",)))

        problem = opening_stock.setup_problem(self.company, self.date, self.temporary)

        self.assertIn("can't do both", problem)
        frappe.set_user("Administrator")
        self.assertIsNone(opening_stock.setup_problem(self.company, self.date, self.temporary))


class TestReadingLines(OpeningStockCase):
    def test_numbers_are_read_the_accountants_way(self):
        self.assertEqual(opening_stock.number("1,250.50"), 1250.5)
        self.assertEqual(opening_stock.number("(12)"), -12)
        self.assertEqual(opening_stock.number(" 7 "), 7)
        self.assertIsNone(opening_stock.number(""))
        with self.assertRaises(ValueError):
            opening_stock.number("ten")

    def test_a_totals_row_is_recognised(self):
        self.assertTrue(opening_stock.is_totals_row({"item_code": "Grand Total", "qty": "40"}))
        self.assertTrue(opening_stock.is_totals_row({"item_code": "", "qty": "40"}))
        self.assertTrue(opening_stock.is_totals_row({"item_code": "Sub-total", "qty": "4"}))
        self.assertFalse(opening_stock.is_totals_row({"item_code": "Total Care Kit", "qty": "4"}))
        self.assertFalse(opening_stock.is_totals_row({"item_code": "", "qty": ""}))

    def test_lines_keep_the_spreadsheet_row_numbers_and_skip_blank_lines(self):
        session = self._session([f"{self.item},{self.stores},5,10", ",,,", f"{self.item2},{self.shop},3,4"])
        self._setup(session)

        lines = opening_stock.read_lines(self._doc(session))

        self.assertEqual([line.row for line in lines], [2, 4])
        self.assertEqual(lines[1].cells["item_code"], self.item2)
        self.assertEqual(lines[1].cells["qty"], "3")
