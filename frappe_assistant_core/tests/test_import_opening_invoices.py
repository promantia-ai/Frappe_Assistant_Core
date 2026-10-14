# frappe_assistant_core/tests/test_import_opening_invoices.py
"""
Opening receivables and payables (#33997): the old system's unpaid invoices and bills become
opening Sales or Purchase Invoices, built by ERPNext's Opening Invoice Creation Tool. Every row is
checked first; nothing is written until the user approves; totals are compared with the file's after.
"""

import unittest
from unittest.mock import patch

import frappe
from frappe.utils import add_days, nowdate

from frappe_assistant_core.core.tool_registry import get_tool_registry
from frappe_assistant_core.plugins.core import import_dry_run as dry_run
from frappe_assistant_core.plugins.core import import_opening_invoices as opening_invoices
from frappe_assistant_core.tests.base_test import BaseAssistantTest

HEADER = "Customer,Invoice,Date,Due,Amount"
COLUMNS = {
    "Customer": "party",
    "Invoice": "invoice_number",
    "Date": "posting_date",
    "Due": "due_date",
    "Amount": "outstanding_amount",
}


@unittest.skipUnless(
    "erpnext" in frappe.get_installed_apps(), "needs ERPNext's Opening Invoice Creation Tool"
)
class OpeningInvoicesCase(BaseAssistantTest):
    """A company with a Temporary Opening account, two customers and a supplier, made for each test."""

    def setUp(self):
        super().setUp()
        frappe.set_user("Administrator")
        self.registry = get_tool_registry()
        self.tag = frappe.generate_hash(length=6)
        self.company = frappe.get_all("Company", pluck="name", order_by="creation", limit=1)[0]
        self.currency = frappe.get_cached_value("Company", self.company, "default_currency")
        self.date = nowdate()
        if not opening_invoices.in_fiscal_year(self.date, self.company):
            self.skipTest("today is in no fiscal year of the test company")
        self.files_before = set(frappe.get_all("File", pluck="name"))
        self.temporary = self._temporary_account()
        self.customer = self._party("Customer", f"OI Cust {self.tag}")
        self.customer2 = self._party("Customer", f"OI Cust2 {self.tag}")
        self.supplier = self._party("Supplier", f"OI Supp {self.tag}")
        self.supplier2 = self._party("Supplier", f"OI Supp2 {self.tag}")

    def tearDown(self):
        frappe.set_user("Administrator")
        for name in set(frappe.get_all("File", pluck="name")) - self.files_before:
            frappe.delete_doc("File", name, force=True, ignore_permissions=True)
        super().tearDown()

    # -- fixtures ------------------------------------------------------------------------

    def _temporary_account(self):
        name = opening_invoices.temporary_account(self.company)
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

    def _party(self, doctype, title, **extra):
        field = opening_invoices.PARTY_NAME_FIELDS[doctype]
        group_field, group_doctype = {
            "Customer": ("customer_group", "Customer Group"),
            "Supplier": ("supplier_group", "Supplier Group"),
        }[doctype]
        # the site's default group can be a group node, which a party can't belong to
        group = frappe.db.get_value(group_doctype, {"is_group": 0})
        if group:
            extra.setdefault(group_field, group)
        return frappe.get_doc({"doctype": doctype, field: title, **extra}).insert().name

    def _run(self, tool_name, arguments):
        return self.registry.get_tool(tool_name)._safe_execute(arguments)

    def _file(self, lines, header=HEADER):
        content = ("\n".join([header, *lines]) + "\n").encode()
        return frappe.get_doc(
            {"doctype": "File", "file_name": f"open-{self.tag}.csv", "content": content, "is_private": 1}
        ).insert()

    def _session(self, lines, header=HEADER):
        started = self._run("start_import_session", {"file_id": self._file(lines, header).name})
        self.assertTrue(started["success"], started)
        return started["result"]["session_id"]

    def _setup(self, session, **changes):
        arguments = {
            "session": session,
            "company": self.company,
            "invoice_type": "Sales",
            "cut_off": self.date,
            "columns": dict(COLUMNS),
            **changes,
        }
        return self._run("set_opening_invoices", arguments)

    def _doc(self, session):
        return frappe.get_doc("FAC Import Session", session)

    def _number(self, n):
        return f"OI-{self.tag}-{n}"

    def _check(self, session):
        reply = self._run("dry_run_import", {"session": session})
        self.assertTrue(reply["success"], reply)
        return reply["result"]

    def _ready(self, lines, **setup):
        """A session whose sheet passed the check, with that check's reply."""
        session = self._session(lines)
        self.assertTrue(self._setup(session, **setup)["success"])
        return session, self._check(session)

    def _card(self, session, result, **changes):
        """The values the approval card shows: what the last check reported."""
        return {
            "session": session,
            "company": self.company,
            "invoice_type": result["opening_invoices"]["invoice_type"],
            "rows": result["ready"],
            "total_amount": result["opening_invoices"]["total_amount"],
            **changes,
        }

    def _groups(self, result):
        return {g["cause"]: g for g in result["groups"]}


class TestSetup(OpeningInvoicesCase):
    def test_maps_columns_and_stores_settings(self):
        session = self._session([f"{self.customer},{self._number(1)},{self.date},{self.date},100"])
        reply = self._setup(session)
        self.assertTrue(reply["success"], reply)
        doc = self._doc(session)
        self.assertEqual(doc.status, "Mapped")
        self.assertEqual(doc.target_doctype, "Sales Invoice")
        stored = opening_invoices.settings(doc)
        self.assertEqual(stored["party_type"], "Customer")
        self.assertEqual(stored["temporary_opening_account"], self.temporary)
        self.assertEqual(stored["cut_off"], self.date)
        self.assertEqual(stored["columns"], {"invoice_number": 1})
        mapping = reply["result"]["column_to_field_map"]
        self.assertEqual(mapping["0"], "customer")
        self.assertEqual(mapping["4"], "outstanding_amount")
        self.assertEqual(mapping["1"], "Don't Import")

    def test_purchase_maps_the_party_to_supplier(self):
        session = self._session([f"{self.supplier},B1,{self.date},{self.date},100"])
        reply = self._setup(session, invoice_type="Purchase")
        self.assertTrue(reply["success"], reply)
        self.assertEqual(self._doc(session).target_doctype, "Purchase Invoice")
        self.assertEqual(reply["result"]["column_to_field_map"]["0"], "supplier")

    def test_every_wrong_column_is_listed_at_once(self):
        session = self._session([f"{self.customer},1,{self.date},{self.date},100"])
        reply = self._setup(session, columns={"Nope": "party", "Amount": "bogus"})
        self.assertFalse(reply["success"])
        error = frappe.as_json(reply)
        self.assertIn("Nope", error)
        self.assertIn("bogus", error)
        self.assertIn("party, outstanding_amount", error)

    def test_refuses_wrong_settings(self):
        session = self._session([f"{self.customer},1,{self.date},{self.date},100"])
        for changes, words in (
            ({"invoice_type": "Both"}, "Sales"),
            ({"cut_off": "soon"}, "isn't a date"),
            ({"cut_off": "1990-01-31"}, "fiscal year"),
            ({"company": "No Such Company"}, "doesn't exist"),
        ):
            reply = self._setup(session, **changes)
            self.assertFalse(reply["success"], changes)
            self.assertIn(words, frappe.as_json(reply))

    def test_refuses_without_submit_permission(self):
        session = self._session([f"{self.customer},1,{self.date},{self.date},100"])
        real = frappe.has_permission

        def no_submit(doctype=None, ptype="read", *args, **kwargs):
            if doctype == "Sales Invoice" and ptype == "submit":
                return False
            return real(doctype, ptype, *args, **kwargs)

        with patch.object(opening_invoices.frappe, "has_permission", side_effect=no_submit):
            reply = self._setup(session)
        self.assertFalse(reply["success"])
        self.assertIn("Accounts Manager", frappe.as_json(reply))

    def test_dry_run_goes_to_this_engine(self):
        session = self._session([f"{self.customer},{self._number(1)},{self.date},{self.date},100"])
        self._setup(session)
        self.assertIs(dry_run.special_engine(self._doc(session)), opening_invoices)


class TestReadingLines(OpeningInvoicesCase):
    def test_dates_as_files_write_them(self):
        self.assertEqual(opening_invoices.date_of("2026-03-04"), "2026-03-04")  # ISO is never day-first
        self.assertEqual(opening_invoices.date_of("2026-03-04 00:00:00"), "2026-03-04")
        self.assertEqual(opening_invoices.date_of("31/03/2026"), "2026-03-31")
        self.assertIsNone(opening_invoices.date_of("  "))
        with self.assertRaises(ValueError):
            opening_invoices.date_of("soon")

    def test_totals_rows(self):
        self.assertTrue(opening_invoices.is_totals_row({"party": "Grand Total", "outstanding_amount": "500"}))
        self.assertTrue(opening_invoices.is_totals_row({"party": "", "outstanding_amount": "500"}))
        self.assertFalse(opening_invoices.is_totals_row({"party": "Acme", "outstanding_amount": "500"}))
        self.assertFalse(opening_invoices.is_totals_row({"party": "Total", "outstanding_amount": ""}))

    def test_keys(self):
        sales, purchase = {"invoice_type": "Sales"}, {"invoice_type": "Purchase"}
        numbered = {"party": " Acme ", "invoice_number": "INV-1", "posting_date": "2026-01-01"}
        self.assertEqual(opening_invoices.invoice_key(sales, numbered), "inv-1")
        self.assertEqual(opening_invoices.invoice_key(purchase, numbered), "acme|inv-1")
        bare = {"party": "Acme", "posting_date": "2026-01-01", "outstanding_amount": "1,250"}
        self.assertEqual(opening_invoices.invoice_key(sales, bare), "acme|2026-01-01|1,250")

    def test_lines_have_every_mapped_field_with_value_maps_applied(self):
        session = self._session(
            [
                f'Old Name,{self._number(1)},{self.date},,"1,250.50"',
                ",,,,",
                f"{self.customer2},{self._number(2)},{self.date},{self.date},10",
            ]
        )
        self._setup(session)
        doc = self._doc(session)
        doc.transformation_rules = frappe.as_json(
            {
                "value_maps": [
                    {
                        "parent": "Sales Invoice",
                        "field": "customer",
                        "label": "Customer",
                        "from": "Old Name",
                        "to": self.customer,
                    }
                ]
            }
        )
        doc.save()
        lines = opening_invoices.read_lines(doc)
        self.assertEqual([line.row for line in lines], [2, 4])  # the blank row is skipped
        first = lines[0].cells
        self.assertEqual(first["party"], self.customer)
        self.assertEqual(first["invoice_number"], self._number(1))
        self.assertEqual(first["outstanding_amount"], "1,250.50")
        self.assertEqual(first["due_date"], "")
