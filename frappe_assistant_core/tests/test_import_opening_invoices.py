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
from frappe_assistant_core.plugins.core import import_fixes
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


class TestCheck(OpeningInvoicesCase):
    def _line(self, party, number, date, due, amount):
        return f"{party},{number},{date},{due},{amount}"

    def test_ready_rows_and_totals_and_nothing_written(self):
        before = frappe.db.count("Sales Invoice")
        session, result = self._ready(
            [
                self._line(self.customer, self._number(1), self.date, self.date, '"1,250.50"'),
                self._line(self.customer2, self._number(2), self.date, "", "100"),
            ]
        )
        self.assertEqual((result["ready"], result["failed"]), (2, 0), result)
        totals = result["opening_invoices"]
        self.assertEqual((totals["rows"], totals["parties"], totals["total_amount"]), (2, 2, 1350.5))
        self.assertEqual(self._doc(session).status, "Ready to Import")
        self.assertEqual(frappe.db.count("Sales Invoice"), before)

    def test_party_found_by_customer_name_when_named_otherwise(self):
        renamed = frappe.rename_doc("Customer", self.customer2, f"CUST-{self.tag}", force=True)
        title = frappe.db.get_value("Customer", renamed, "customer_name")
        session = self._session([self._line(title, self._number(1), self.date, "", "10")])
        self._setup(session)
        self.assertEqual(opening_invoices.ready_lines(self._doc(session))[0].party, renamed)

    def test_each_cause_is_one_group(self):
        disabled = self._party("Customer", f"OI Off {self.tag}", disabled=1)
        other = "USD" if self.currency != "USD" else "EUR"
        foreign = self._party("Customer", f"OI Foreign {self.tag}", default_currency=other)
        session, result = self._ready(
            [
                self._line(f"Nobody {self.tag}", self._number(1), self.date, "", "10"),
                self._line(disabled, self._number(2), self.date, "", "10"),
                self._line(foreign, self._number(3), self.date, "", "10"),
                self._line(self.customer, self._number(4), self.date, "", "0"),
                self._line(self.customer, self._number(5), self.date, "", "-5"),
                self._line(self.customer, self._number(6), "1990-01-15", "", "10"),
                self._line(self.customer, self._number(7), self.date, add_days(self.date, -1), "10"),
                self._line(self.customer, self._number(8), self.date, "", "10"),
                self._line(self.customer2, self._number(8), self.date, "", "20"),
                self._line(self.customer, self._number(9), "soon", "", "10"),
                self._line(self.customer, self._number(10), self.date, "", "ten"),
                ",,,,1000",
            ]
        )
        groups = self._groups(result)
        self.assertEqual(groups[dry_run.CAUSE_MISSING_LINK]["value"], f"Nobody {self.tag}")
        self.assertEqual(groups[opening_invoices.CAUSE_PARTY_DISABLED]["value"], disabled)
        self.assertEqual(groups[opening_invoices.CAUSE_FOREIGN_CURRENCY]["count"], 1)
        self.assertEqual(groups[opening_invoices.CAUSE_NOT_POSITIVE]["count"], 2)  # zero and negative
        self.assertEqual([e["row"] for e in groups[opening_invoices.CAUSE_NO_FISCAL_YEAR]["examples"]], [7])
        self.assertEqual([e["row"] for e in groups[opening_invoices.CAUSE_DUE_BEFORE]["examples"]], [8])
        self.assertEqual([e["row"] for e in groups[opening_invoices.CAUSE_NUMBER_TWICE]["examples"]], [10])
        self.assertEqual([e["row"] for e in groups[dry_run.CAUSE_BAD_DATE]["examples"]], [11])
        self.assertEqual([e["row"] for e in groups[dry_run.CAUSE_BAD_VALUE]["examples"]], [12])
        self.assertEqual(
            [e["row"] for e in groups[opening_invoices.opening_stock.CAUSE_TOTALS_ROW]["examples"]], [13]
        )
        self.assertEqual(result["ready"], 1)  # only row 9

    def test_number_already_used(self):
        session = self._session([self._line(self.customer, self._number(1), self.date, "", "10")])
        self._setup(session)
        line = opening_invoices.ready_lines(self._doc(session))[0]
        opening_invoices.make_invoice(opening_invoices.settings(self._doc(session)), line)
        result = self._check(session)
        self.assertEqual(
            [e["row"] for e in self._groups(result)[opening_invoices.CAUSE_NUMBER_USED]["examples"]], [2]
        )

    def test_suppliers_may_share_a_bill_number_but_one_supplier_may_not(self):
        session, result = self._ready(
            [
                self._line(self.supplier, "INV-1", self.date, "", "10"),
                self._line(self.supplier2, "INV-1", self.date, "", "20"),
                self._line(self.supplier, "INV-1", self.date, "", "30"),
            ],
            invoice_type="Purchase",
        )
        self.assertEqual(result["ready"], 2, result)
        self.assertEqual(
            [e["row"] for e in self._groups(result)[opening_invoices.CAUSE_NUMBER_TWICE]["examples"]], [4]
        )

    def test_rejected_by_erpnext_is_one_group_and_rolled_back(self):
        frappe.get_doc(
            {
                "doctype": "Accounting Period",
                "period_name": f"OI Closed {self.tag}",
                "company": self.company,
                "start_date": self.date,
                "end_date": self.date,
                "closed_documents": [{"document_type": "Sales Invoice", "closed": 1}],
            }
        ).insert()
        before = frappe.db.count("GL Entry")
        invoices_before = frappe.db.count("Sales Invoice")
        session, result = self._ready(
            [
                self._line(self.customer, self._number(1), self.date, "", "10"),
                self._line(self.customer2, self._number(2), self.date, "", "20"),
            ]
        )
        rejected = self._groups(result)[dry_run.CAUSE_REJECTED]
        self.assertEqual(rejected["count"], 2)
        self.assertEqual(result["ready"], 0)
        self.assertEqual(frappe.db.count("GL Entry"), before)
        self.assertEqual(frappe.db.count("Sales Invoice"), invoices_before)
        for n in (1, 2):
            self.assertFalse(frappe.db.exists("Sales Invoice", self._number(n)))

    # -- post at the cut-off, repeated rows, excluded rows, bills ---------------------------

    def _at_cutoff(self, session):
        doc = self._doc(session)
        doc.transformation_rules = frappe.as_json({"post_at_cutoff": True})
        doc.save()
        return self._doc(session)

    def test_post_at_cutoff_moves_an_old_invoice_to_the_cutoff(self):
        later = add_days(self.date, 10)
        session = self._session(
            [
                self._line(self.customer, self._number(1), "1990-01-15", "1990-02-15", "10"),
                self._line(self.customer, self._number(2), "1990-01-15", later, "20"),
            ]
        )
        self._setup(session)
        first, second = opening_invoices.ready_lines(self._at_cutoff(session))
        self.assertEqual(
            (first.posting_date, first.due_date, first.original_date), (self.date, self.date, "1990-01-15")
        )
        self.assertEqual(
            (second.posting_date, second.due_date, second.original_date), (self.date, later, "1990-01-15")
        )

    def test_identical_unnumbered_rows_are_both_ready_with_their_own_keys(self):
        session = self._session([self._line(self.customer, "", self.date, "", "10")] * 2)
        self._setup(session)
        ready = opening_invoices.ready_lines(self._doc(session))
        self.assertEqual(len(ready), 2)
        self.assertEqual(sorted(line.key[-2:] for line in ready), ["#1", "#2"])
        self.assertEqual(len({line.key for line in ready}), 2)

    def test_excluded_rows_are_marked_and_not_checked(self):
        session = self._session(
            [
                self._line(f"Nobody {self.tag}", self._number(1), self.date, "", "10"),
                self._line(self.customer, self._number(2), self.date, "", "10"),
            ]
        )
        self._setup(session)
        doc = self._doc(session)
        doc.excluded_rows = frappe.as_json([2])
        doc.save()
        checker = opening_invoices._InvoiceChecker("Sales Invoice")
        ready = opening_invoices._check_lines(self._doc(session), checker)
        self.assertEqual([line.row for line in ready], [3])
        self.assertEqual(checker.rows[0], {"row": 2, "status": "excluded"})
        self.assertEqual(checker.groups, {})  # the missing customer in row 2 was never checked

    def test_purchase_number_used_only_for_the_same_supplier(self):
        session = self._session([self._line(self.supplier, "BILL-1", self.date, "", "10")])
        self._setup(session, invoice_type="Purchase")
        doc = self._doc(session)
        opening_invoices.make_invoice(opening_invoices.settings(doc), opening_invoices.ready_lines(doc)[0])
        other = self._session(
            [
                self._line(self.supplier, "BILL-1", self.date, "", "10"),
                self._line(self.supplier2, "BILL-1", self.date, "", "20"),
            ]
        )
        self._setup(other, invoice_type="Purchase")
        result = self._check(other)
        used = self._groups(result)[opening_invoices.CAUSE_NUMBER_USED]
        self.assertEqual([e["row"] for e in used["examples"]], [2])
        self.assertEqual(result["ready"], 1)

    def test_purchase_invoice_carries_the_bill_number_and_dates(self):
        session = self._session(
            [
                self._line(self.supplier, "BILL-2", self.date, "", "10"),
                self._line(self.supplier, "BILL-3", "1990-01-15", "", "10"),
            ]
        )
        self._setup(session, invoice_type="Purchase")
        doc = self._at_cutoff(session)
        invoices = opening_invoices.settings(doc)
        plain, old = opening_invoices.ready_lines(doc)
        built = opening_invoices.build_invoice(invoices, plain)
        self.assertEqual((built.bill_no, str(built.bill_date)), ("BILL-2", self.date))
        built = opening_invoices.build_invoice(invoices, old)
        self.assertEqual((built.bill_no, str(built.bill_date)), ("BILL-3", "1990-01-15"))
        self.assertEqual(str(built.posting_date), self.date)
        self.assertIn("1990-01-15", built.remarks)

    def test_two_disabled_parties_are_two_groups_with_their_own_values(self):
        first = self._party("Customer", f"OI Off1 {self.tag}", disabled=1)
        second = self._party("Customer", f"OI Off2 {self.tag}", disabled=1)
        session, result = self._ready(
            [
                self._line(first, self._number(1), self.date, "", "10"),
                self._line(second, self._number(2), self.date, "", "10"),
                self._line(first, self._number(3), self.date, "", "10"),
            ]
        )
        disabled = [g for g in result["groups"] if g["cause"] == opening_invoices.CAUSE_PARTY_DISABLED]
        self.assertEqual(sorted(g["value"] for g in disabled), sorted([first, second]))
        self.assertEqual(sorted(g["count"] for g in disabled), [1, 2])


class TestReadingLines(OpeningInvoicesCase):
    def test_dates_as_files_write_them(self):
        self.assertEqual(opening_invoices.date_of("2026-03-04"), "2026-03-04")  # ISO is never day-first
        self.assertEqual(opening_invoices.date_of("2026-03-04 00:00:00"), "2026-03-04")
        self.assertEqual(opening_invoices.date_of("31/03/2026"), "2026-03-31")
        self.assertIsNone(opening_invoices.date_of("  "))
        with self.assertRaises(ValueError):
            opening_invoices.date_of("soon")

    def test_ambiguous_date_follows_the_site_format(self):
        for site_format, expected in (("dd-mm-yyyy", "2026-03-04"), ("mm-dd-yyyy", "2026-04-03")):
            with patch("frappe.utils.data.get_user_date_format", return_value=site_format):
                self.assertEqual(opening_invoices.date_of("04/03/2026"), expected)

    def test_impossible_iso_date_is_refused(self):
        with self.assertRaises(ValueError):
            opening_invoices.date_of("2026-13-45")

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
        self.assertEqual(opening_invoices.invoice_key(purchase, bare), "acme|2026-01-01|1,250")

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


class TestFixes(OpeningInvoicesCase):
    def _fix(self, session, group, action, **extra):
        return self._run(
            "dry_run_import", {"session": session, "fixes": [{"group": group, "action": action, **extra}]}
        )

    def test_post_at_cutoff_moves_old_invoices_to_the_cut_off(self):
        session, result = self._ready([f"{self.customer},{self._number(1)},1990-01-15,1990-02-15,10"])
        group = self._groups(result)[opening_invoices.CAUSE_NO_FISCAL_YEAR]
        fixes = [f["action"] for f in group["fixes"]]
        self.assertEqual(fixes, ["post_at_cutoff", "leave_out"])
        result = self._fix(session, group["id"], "post_at_cutoff")["result"]
        self.assertEqual(result["ready"], 1, result)
        line = opening_invoices.ready_lines(self._doc(session))[0]
        self.assertEqual(
            (line.posting_date, line.due_date, line.original_date), (self.date, self.date, "1990-01-15")
        )

    def test_post_at_cutoff_only_for_that_group(self):
        session, result = self._ready([f"{self.customer},{self._number(1)},{self.date},,0"])
        group = self._groups(result)[opening_invoices.CAUSE_NOT_POSITIVE]
        reply = self._fix(session, group["id"], "post_at_cutoff")
        self.assertFalse(reply["success"], reply)
        self.assertIn("no fiscal year", frappe.as_json(reply))

    def test_disabled_party_can_be_mapped(self):
        disabled = self._party("Customer", f"OI Off {self.tag}", disabled=1)
        session, result = self._ready([f"{disabled},{self._number(1)},{self.date},,10"])
        group = self._groups(result)[opening_invoices.CAUSE_PARTY_DISABLED]
        self.assertEqual(group["fixes"][0]["action"], "map")
        result = self._fix(session, group["id"], "map", to=self.customer)["result"]
        self.assertEqual(result["ready"], 1, result)

    def test_mapping_a_disabled_party_to_nobody_is_refused_and_changes_nothing(self):
        disabled = self._party("Customer", f"OI Off {self.tag}", disabled=1)
        session, result = self._ready([f"{disabled},{self._number(1)},{self.date},,10"])
        group = self._groups(result)[opening_invoices.CAUSE_PARTY_DISABLED]
        before = self._doc(session).transformation_rules
        reply = self._fix(session, group["id"], "map", to=f"Nobody {self.tag}")
        self.assertFalse(reply["success"], reply)
        self.assertIn("There is no", frappe.as_json(reply))
        self.assertEqual(self._doc(session).transformation_rules, before)

    def test_every_new_cause_has_its_own_explanation_and_fixes(self):
        disabled = self._party("Customer", f"OI Off {self.tag}", disabled=1)
        other = "USD" if self.currency != "USD" else "EUR"
        foreign = self._party("Customer", f"OI Foreign {self.tag}", default_currency=other)

        def line(party, number, date, due, amount):
            return f"{party},{number},{date},{due},{amount}"

        session = self._session(
            [
                line(disabled, self._number(1), self.date, "", "10"),
                line(foreign, self._number(2), self.date, "", "10"),
                line(self.customer, self._number(3), self.date, "", "0"),
                line(self.customer, self._number(4), "1990-01-15", "", "10"),
                line(self.customer, self._number(5), self.date, add_days(self.date, -1), "10"),
                line(self.customer, self._number(6), self.date, "", "10"),
                line(self.customer2, self._number(6), self.date, "", "20"),
                line(self.customer, self._number(7), self.date, "", "10"),
            ]
        )
        self.assertTrue(self._setup(session)["success"])
        used = [
            ready
            for ready in opening_invoices.ready_lines(self._doc(session))
            if ready.invoice_number == self._number(7)
        ]
        opening_invoices.make_invoice(opening_invoices.settings(self._doc(session)), used[0])
        result = self._check(session)
        groups = self._groups(result)
        expected = {
            opening_invoices.CAUSE_NO_FISCAL_YEAR: ["post_at_cutoff", "leave_out"],
            opening_invoices.CAUSE_PARTY_DISABLED: ["map", "leave_out"],
        }
        self.assertEqual(set(opening_invoices.NEW_CAUSES) - set(groups), set(), "every new cause is produced")
        for cause in opening_invoices.NEW_CAUSES:
            group = groups[cause]
            self.assertRegex(group["explanation"], r"^(1 row|\d+ rows)", group)
            self.assertTrue(group["message"], group)
            self.assertIn(group["message"], group["explanation"])
            self.assertNotIn("would fail", group["explanation"])
            self.assertTrue(group["fix_in_file"], group)
            self.assertEqual([f["action"] for f in group["fixes"]], expected.get(cause, ["leave_out"]), cause)


class TestParties(OpeningInvoicesCase):
    def _missing(self, title, **setup):
        session, result = self._ready([f"{title},{self._number(1)},{self.date},,10"], **setup)
        self.assertEqual(result["failed"], 1, result)
        return session, result

    def _create(self, session, documents):
        # The plan leaves a group or territory out when the site's default is a group node (a
        # party can't belong to one): the user then picks a leaf, as this does.
        filled = []
        for document in documents:
            data = dict(document["data"])
            for field, link in opening_invoices.PARTY_LINKS[document["doctype"]].items():
                if field not in data:
                    data[field] = frappe.db.get_value(link, {"is_group": 0})
            filled.append({**document, "data": data})
        return self._run("create_document", {"documents": filled, "import_session": session})

    def test_plan_lists_the_missing_customer(self):
        session, result = self._missing(f"New Cust {self.tag}")
        plan = result["masters_plan"]
        self.assertEqual(len(plan), 1, plan)
        entry = plan[0]
        self.assertEqual(
            (entry["doctype"], entry["name"], entry["rows"]), ("Customer", f"New Cust {self.tag}", 1)
        )
        self.assertEqual(entry["data"]["customer_name"], f"New Cust {self.tag}")
        self.assertEqual(entry["data"]["customer_type"], "Company")
        for field in ("customer_group", "territory"):
            if field in entry["data"]:
                doctype = opening_invoices.PARTY_LINKS["Customer"][field]
                self.assertFalse(frappe.db.get_value(doctype, entry["data"][field], "is_group"))

    def test_created_customer_makes_the_rows_ready(self):
        session, result = self._missing(f"New Cust {self.tag}")
        entry = result["masters_plan"][0]
        reply = self._create(session, [{"doctype": "Customer", "data": entry["data"]}])
        self.assertTrue(reply["success"], reply)
        self.assertEqual(self._check(session)["ready"], 1)
        records = [r.record_doctype for r in self._doc(session).created_records]
        self.assertIn("Customer", records)

    def test_customer_named_by_series_is_still_found(self):
        before = frappe.defaults.get_global_default("cust_master_name")
        frappe.defaults.set_global_default("cust_master_name", "Naming Series")
        try:
            session, result = self._missing(f"Series Cust {self.tag}")
            data = result["masters_plan"][0]["data"]
            reply = self._create(session, [{"doctype": "Customer", "data": data}])
            self.assertTrue(reply["success"], reply)
            name = reply["result"]["created"][0]["name"]
            self.assertNotEqual(name, f"Series Cust {self.tag}")
            self.assertEqual(self._check(session)["ready"], 1)
        finally:
            frappe.defaults.set_global_default("cust_master_name", before)

    def test_only_the_planned_fields_and_leaf_groups(self):
        session, result = self._missing(f"New Cust {self.tag}")
        data = dict(result["masters_plan"][0]["data"])
        reply = self._create(session, [{"doctype": "Customer", "data": {**data, "email_id": "a@b.c"}}])
        self.assertFalse(reply["success"])
        self.assertIn("email_id", str(reply["result"]))
        group = frappe.db.get_value("Customer Group", {"is_group": 1})
        reply = self._create(session, [{"doctype": "Customer", "data": {**data, "customer_group": group}}])
        self.assertFalse(reply["success"])
        self.assertIn(group, str(reply["result"]))

    def test_missing_supplier_is_planned_and_created(self):
        session, result = self._missing(f"New Supp {self.tag}", invoice_type="Purchase")
        entry = result["masters_plan"][0]
        self.assertEqual(entry["doctype"], "Supplier")
        self.assertEqual(entry["data"]["supplier_name"], f"New Supp {self.tag}")
        reply = self._create(session, [{"doctype": "Supplier", "data": entry["data"]}])
        self.assertTrue(reply["success"], reply)
        self.assertEqual(self._check(session)["ready"], 1)

    def _set_default(self, field, value):
        frappe.db.set_single_value("Selling Settings", field, value)

    def test_plan_says_what_to_choose_when_a_default_is_a_group_node(self):
        group = frappe.db.get_value("Customer Group", {"is_group": 1})
        leaf = frappe.db.get_value("Customer Group", {"is_group": 0})
        before = frappe.db.get_single_value("Selling Settings", "customer_group")
        self._set_default("customer_group", group)
        try:
            session, result = self._missing(f"Pick Cust {self.tag}")
            entry = result["masters_plan"][0]
            self.assertNotIn("customer_group", entry["data"])
            self.assertIn(leaf, entry["choose"]["customer_group"])
            self.assertLessEqual(len(entry["choose"]["customer_group"]), 20)
            self.assertEqual(
                set(entry["choose"]), {"customer_group", "territory"} - set(entry["data"]), entry
            )
            for name in entry["choose"]["customer_group"]:
                self.assertFalse(frappe.db.get_value("Customer Group", name, "is_group"))
        finally:
            self._set_default("customer_group", before)

    def test_plan_has_no_choose_when_every_default_is_a_leaf(self):
        group_leaf = frappe.db.get_value("Customer Group", {"is_group": 0})
        territory_leaf = frappe.db.get_value("Territory", {"is_group": 0})
        before = {
            f: frappe.db.get_single_value("Selling Settings", f) for f in ("customer_group", "territory")
        }
        self._set_default("customer_group", group_leaf)
        self._set_default("territory", territory_leaf)
        try:
            session, result = self._missing(f"Leaf Cust {self.tag}")
            entry = result["masters_plan"][0]
            self.assertEqual(entry["data"]["customer_group"], group_leaf)
            self.assertEqual(entry["data"]["territory"], territory_leaf)
            self.assertNotIn("choose", entry)
            reply = self._run(
                "create_document",
                {"documents": [{"doctype": "Customer", "data": entry["data"]}], "import_session": session},
            )
            self.assertTrue(reply["success"], reply)
        finally:
            for field, value in before.items():
                self._set_default(field, value)

    def test_supplier_plan_chooses_only_the_supplier_group(self):
        group = frappe.db.get_value("Supplier Group", {"is_group": 1})
        before = frappe.db.get_single_value("Buying Settings", "supplier_group")
        frappe.db.set_single_value("Buying Settings", "supplier_group", group)
        try:
            session, result = self._missing(f"Pick Supp {self.tag}", invoice_type="Purchase")
            entry = result["masters_plan"][0]
            self.assertNotIn("supplier_group", entry["data"])
            self.assertEqual(set(entry["choose"]), {"supplier_group"})
        finally:
            frappe.db.set_single_value("Buying Settings", "supplier_group", before)

    def test_cannot_create_a_party_without_permission_or_when_the_site_forbids_it(self):
        session, _result = self._missing(f"New Cust {self.tag}")
        doc = self._doc(session)
        self.assertEqual(import_fixes.cannot_create("Customer", doc), "")
        real = frappe.has_permission

        def no_create(doctype=None, ptype="read", *args, **kwargs):
            if doctype == "Customer" and ptype == "create":
                return False
            return real(doctype, ptype, *args, **kwargs)

        with patch.object(opening_invoices.frappe, "has_permission", side_effect=no_create):
            self.assertIn("aren't allowed", import_fixes.cannot_create("Customer", doc))
        with patch.dict(frappe.conf, {"fac_import_never_create_doctypes": ["Customer"]}):
            self.assertIn("doesn't allow", import_fixes.cannot_create("Customer", doc))

    def test_a_party_not_in_the_plan_or_already_existing_is_refused(self):
        session, result = self._missing(f"New Cust {self.tag}")
        data = result["masters_plan"][0]["data"]
        other = {**data, "customer_name": f"Other Cust {self.tag}"}
        reply = self._create(session, [{"doctype": "Customer", "data": other}])
        self.assertFalse(reply["success"])
        self.assertIn("isn't one of the masters", str(reply))
        self._party("Customer", f"New Cust {self.tag}")
        reply = self._create(session, [{"doctype": "Customer", "data": data}])
        self.assertFalse(reply["success"])
        self.assertIn("already exists", str(reply))


class TestCreating(OpeningInvoicesCase):
    def _lines(self, n=2, invoice_type="Sales"):
        party = self.customer if invoice_type == "Sales" else self.supplier
        return [f"{party},{self._number(i)},{self.date},,{10 * i}" for i in range(1, n + 1)]

    def _import(self, session, result, **changes):
        return self._run("import_opening_invoices", self._card(session, result, **changes))

    def test_imports_sales_invoices_named_by_their_numbers(self):
        session, result = self._ready(self._lines(2))
        reply = self._import(session, result)
        self.assertTrue(reply["success"], reply)
        doc = self._doc(session)
        self.assertEqual(doc.status, "Completed")
        invoice = frappe.get_doc("Sales Invoice", self._number(2))
        self.assertEqual((invoice.docstatus, invoice.is_opening, invoice.customer), (1, "Yes", self.customer))
        self.assertEqual(invoice.outstanding_amount, 20)
        self.assertEqual(invoice.items[0].income_account, self.temporary)
        self.assertEqual(
            {r.record_name for r in doc.created_records if r.record_doctype == "Sales Invoice"},
            {self._number(1), self._number(2)},
        )

    def test_purchase_keeps_the_bill_number(self):
        session, result = self._ready(self._lines(1, "Purchase"), invoice_type="Purchase")
        self.assertTrue(self._import(session, result)["success"])
        name = next(r.record_name for r in self._doc(session).created_records)
        bill = frappe.db.get_value(
            "Purchase Invoice", name, ["bill_no", "bill_date", "docstatus"], as_dict=True
        )
        self.assertEqual((bill.bill_no, str(bill.bill_date), bill.docstatus), (self._number(1), self.date, 1))
        self.assertNotEqual(name, self._number(1))

    def test_old_invoice_posted_at_cut_off_keeps_its_date(self):
        session, result = self._ready(
            [f"{self.supplier},{self._number(1)},1990-01-15,,10"], invoice_type="Purchase"
        )
        group = self._groups(result)[opening_invoices.CAUSE_NO_FISCAL_YEAR]
        result = self._run(
            "dry_run_import",
            {"session": session, "fixes": [{"group": group["id"], "action": "post_at_cutoff"}]},
        )["result"]
        self.assertTrue(self._import(session, result)["success"])
        name = next(r.record_name for r in self._doc(session).created_records)
        bill = frappe.db.get_value(
            "Purchase Invoice", name, ["posting_date", "bill_date", "remarks"], as_dict=True
        )
        self.assertEqual((str(bill.posting_date), str(bill.bill_date)), (self.date, "1990-01-15"))
        self.assertIn("1990-01-15", bill.remarks)

    def test_card_must_match_the_check(self):
        session, result = self._ready(self._lines(2))
        reply = self._import(session, result, total_amount=999)
        self.assertFalse(reply["success"])
        self.assertIn("30", frappe.as_json(reply))
        self.assertFalse(frappe.db.exists("Sales Invoice", self._number(1)))

    def test_changed_since_the_check_is_refused(self):
        session, result = self._ready(self._lines(2))
        doc = self._doc(session)
        doc.excluded_rows = frappe.as_json([2])
        doc.save()
        reply = self._import(session, result)
        self.assertFalse(reply["success"])
        self.assertIn("dry_run_import", frappe.as_json(reply))

    def test_calling_again_creates_nothing(self):
        session, result = self._ready(self._lines(2))
        self._import(session, result)
        count = frappe.db.count("Sales Invoice", {"customer": self.customer})
        reply = self._import(session, result)
        self.assertTrue(reply["success"], reply)
        self.assertEqual(frappe.db.count("Sales Invoice", {"customer": self.customer}), count)

    def test_one_refused_invoice_does_not_stop_the_rest(self):
        session, result = self._ready(self._lines(3))
        real = opening_invoices.make_invoice

        def refuse_second(invoices, line):
            if line.invoice_number == self._number(2):
                frappe.throw("Refused for the test")
            return real(invoices, line)

        with patch.object(opening_invoices, "make_invoice", side_effect=refuse_second):
            reply = self._import(session, result)
        self.assertTrue(reply["success"], reply)
        doc = self._doc(session)
        self.assertEqual(doc.status, "Partially Completed")
        self.assertTrue(frappe.db.exists("Sales Invoice", self._number(3)))
        self.assertFalse(frappe.db.exists("Sales Invoice", self._number(2)))
        failed = opening_invoices.settings(doc)["failed"]
        self.assertIn("Refused for the test", failed["3"]["message"])

        result = self._check(session)  # the two made are "already imported", by invoice number
        self.assertEqual((result["imported"], result["ready"]), (2, 1), result)
        self.assertTrue(self._import(session, result)["success"])
        self.assertEqual(self._doc(session).status, "Completed")
        self.assertTrue(frappe.db.exists("Sales Invoice", self._number(2)))

    def test_unnumbered_invoices_are_matched_by_party_date_and_amount(self):
        lines = [f"{self.customer},,{self.date},,10", f"{self.customer},,{self.date},,10"]
        session, result = self._ready(lines)
        self.assertEqual(result["ready"], 2)  # the same twice is two invoices
        self._import(session, result)
        result = self._check(session)
        self.assertEqual((result["imported"], result["ready"]), (2, 0), result)

    def test_above_the_limit_runs_as_a_job(self):
        session, result = self._ready(self._lines(3))
        with patch.object(opening_invoices, "REQUEST_LIMIT", 1), patch.object(
            opening_invoices, "COMMIT_EVERY", 2
        ):
            reply = self._import(session, result)
        self.assertTrue(reply["success"], reply)
        doc = self._doc(session)
        self.assertEqual(doc.status, "Completed")
        self.assertEqual(len(opening_invoices.settings(doc)["created"]), 3)
        self.assertEqual(frappe.parse_json(doc.progress)["done"], 3)

    def test_start_import_refuses_an_opening_invoices_session(self):
        session, result = self._ready(self._lines(1))
        reply = self._run(
            "start_import",
            {"session": session, "doctype": "Sales Invoice", "rows": 1, "import_type": "Insert New Records"},
        )
        self.assertFalse(reply["success"])
        self.assertIn("import_opening_invoices", frappe.as_json(reply))

    def _approved_for_a_job(self, n):
        """A session approved for the background job, whose body hasn't run yet."""
        session, result = self._ready(self._lines(n))
        with patch.object(opening_invoices, "REQUEST_LIMIT", 1), patch.object(frappe, "enqueue"):
            self.assertTrue(self._import(session, result)["success"])
        self.assertEqual(self._doc(session).status, "Importing")
        return session

    def test_job_stops_when_rows_dropped_since_the_approval(self):
        session = self._approved_for_a_job(3)
        real = opening_invoices.ready_lines
        with patch.object(opening_invoices, "ready_lines", side_effect=lambda s: real(s)[:-1]):
            opening_invoices.create_job(session, "Administrator")
        for i in (1, 2, 3):
            self.assertFalse(frappe.db.exists("Sales Invoice", self._number(i)))
        doc = self._doc(session)
        self.assertEqual(doc.status, "Failed")
        self.assertIn("check it again", doc.steps[-1].message)

    def test_job_stops_when_the_sheet_changed_since_the_approval(self):
        session = self._approved_for_a_job(2)
        doc = self._doc(session)
        doc.excluded_rows = frappe.as_json([2])
        doc.save()
        opening_invoices.create_job(session, "Administrator")
        self.assertFalse(frappe.db.exists("Sales Invoice", self._number(1)))
        self.assertEqual(self._doc(session).status, "Failed")

    def test_a_job_that_stops_keeps_its_first_batch_and_says_so(self):
        session = self._approved_for_a_job(4)
        calls = {"keep": 0}
        real_keep = opening_invoices._keep

        def keep(*args):
            calls["keep"] += 1
            if calls["keep"] == 2:
                raise RuntimeError("the second batch failed")
            return real_keep(*args)

        events = []
        with (
            patch.object(opening_invoices, "COMMIT_EVERY", 2),
            patch.object(opening_invoices, "_commits", return_value=True),
            patch.object(opening_invoices, "_keep", side_effect=keep),
            patch.object(frappe.db, "commit", side_effect=lambda: events.append("commit")),
            patch.object(frappe.db, "rollback", side_effect=lambda *a, **k: events.append("rollback")),
            patch.object(frappe, "log_error"),
        ):
            opening_invoices.create_job(session, "Administrator")  # does not raise
        self.assertEqual(events, ["commit", "rollback", "commit"])
        doc = self._doc(session)
        self.assertEqual(doc.status, "Partially Completed")
        self.assertIn("stopped", doc.steps[-1].message)
        self.assertEqual(frappe.parse_json(doc.progress)["state"], "stopped")
        self.assertEqual(frappe.parse_json(doc.progress)["done"], 2)
