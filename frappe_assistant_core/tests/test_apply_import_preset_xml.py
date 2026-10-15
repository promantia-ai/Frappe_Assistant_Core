"""
apply_import_preset on a Tally XML export (#34001): listing its parts (AC1), and opening one
import session per target for the parts the user chooses, with the Tally presets (AC2).
"""

import frappe

from frappe_assistant_core.core.tool_registry import get_tool_registry
from frappe_assistant_core.plugins.core import import_presets as presets
from frappe_assistant_core.tests.test_import_tally_xml import (
    GSTIN_NEW,
    VOUCHER,
    XmlCase,
    export,
    group,
    ledger,
    stock_item,
)

MASTERS = (
    group("Sundry Debtors"),
    group("Sundry Creditors"),
    ledger(
        "Ravi Traders {tag}",
        "Sundry Debtors",
        EMAIL="ravi@example.com",
        OPENINGBALANCE="-1000.00",
        **{"LEDGSTREGDETAILS.LIST": GSTIN_NEW},
    ),
    ledger("Mehta Supplies {tag}", "Sundry Creditors", OPENINGBALANCE="500.00"),
    ledger("HDFC Bank {tag}", "Bank Accounts"),
    stock_item("Steel Rod {tag}", "Raw Material", OPENINGBALANCE="10 Nos", OPENINGRATE="100.00/Nos"),
    VOUCHER,
)


class TallyXmlToolCase(XmlCase):
    def setUp(self):
        super().setUp()
        self.registry = get_tool_registry()
        presets.clear_cache()

    def tearDown(self):
        presets.clear_cache()
        super().tearDown()

    def _file(self, *records):
        records = records or MASTERS
        return self._upload(export(*[r.replace("{tag}", self.tag) for r in records]))

    def _run(self, arguments):
        return self.registry.get_tool("apply_import_preset")._safe_execute(arguments)

    def _ok(self, arguments):
        reply = self._run(arguments)
        self.assertTrue(reply["success"], reply)
        return reply["result"]

    def _counts(self):
        return frappe.db.count("FAC Import Session"), frappe.db.count("File")


class TestListing(TallyXmlToolCase):
    def test_parts_are_listed_with_counts_and_nothing_is_written(self):
        doc = self._file()
        before = self._counts()
        result = self._ok({"file_id": doc.name})
        self.assertEqual(self._counts(), before)
        self.assertEqual(result["kind"], "tally_xml")
        self.assertEqual(result["source"]["company"], "Acme Traders")
        parts = {p["part"]: p for p in result["parts"]}
        self.assertEqual(parts["ledgers"]["count"], 3)
        self.assertTrue(parts["ledgers"]["importable"])
        self.assertEqual(parts["ledgers"]["preset"], "tally_prime.ledgers")
        self.assertEqual(parts["ledgers"]["by_parent"]["Sundry Debtors"], 1)
        self.assertEqual(parts["stock_items"]["count"], 1)
        self.assertEqual(parts["groups"]["count"], 2)
        self.assertFalse(parts["groups"]["importable"])
        self.assertIn("not imported", parts["groups"]["note"])
        self.assertEqual(result["other_records"], {"VOUCHER": 1})
        self.assertIn("3 ledgers", result["message"])

    def test_a_part_with_no_records_is_left_out(self):
        result = self._ok({"file_id": self._file(ledger("A {tag}", "Sundry Debtors")).name})
        self.assertEqual([p["part"] for p in result["parts"]], ["ledgers"])


class TestImport(TallyXmlToolCase):
    def test_ledgers_open_customers_and_suppliers_and_ask_for_the_company(self):
        doc = self._file()
        result = self._ok({"file_id": doc.name, "parts": ["ledgers"]})
        self.assertEqual(result["kind"], "tally_xml")
        (ledgers,) = result["results"]
        self.assertEqual(ledgers["part"], "ledgers")
        self.assertEqual(ledgers["preset"]["id"], "tally_prime.ledgers")
        by_target = {s["target"]: s for s in ledgers["sessions"]}
        self.assertEqual(set(by_target), {"Customers", "Suppliers"})
        self.assertEqual(ledgers["needs"][0]["name"], "company")
        customers = by_target["Customers"]
        mapped = {row["column"]: row["field"] for row in customers["mapping"]["mapping"]}
        self.assertEqual(mapped["Name"], "customer_name")
        self.assertEqual(mapped["GSTIN"], "tax_id")
        self.assertEqual(mapped["Email"], "email_id")
        session = frappe.get_doc("FAC Import Session", customers["session_id"])
        self.assertEqual(session.status, "Mapped")
        self.assertIn("ledgers (Tally XML)", session.steps[0].message)
        self.assertTrue(session.file_name.startswith(f"Master-{self.tag} - Customers"))

    def test_a_second_call_opens_only_accounts(self):
        doc = self._file()
        self._ok({"file_id": doc.name, "parts": ["ledgers"]})
        before = frappe.db.count("FAC Import Session")
        company = frappe.get_all("Company", pluck="name", limit=1)[0]
        result = self._ok(
            {
                "file_id": doc.name,
                "parts": ["ledgers"],
                "values": {"company": company},
                "targets": ["Accounts"],
            }
        )
        self.assertEqual([s["target"] for s in result["results"][0]["sessions"]], ["Accounts"])
        self.assertEqual(frappe.db.count("FAC Import Session"), before + 1)

    def test_stock_items_open_items(self):
        result = self._ok({"file_id": self._file().name, "parts": ["stock_items"]})
        (items,) = result["results"]
        self.assertEqual(items["preset"]["id"], "tally_prime.stock_items")
        (session,) = items["sessions"]
        self.assertEqual(session["doctype"], "Item")
        mapped = {row["column"]: row["field"] for row in session["mapping"]["mapping"]}
        self.assertEqual(mapped["Units"], "stock_uom")
        self.assertEqual(mapped["Group"], "item_group")

    def test_both_parts_in_one_call(self):
        result = self._ok({"file_id": self._file().name, "parts": ["ledgers", "stock_items"]})
        self.assertEqual([r["part"] for r in result["results"]], ["ledgers", "stock_items"])
        self.assertIn("3 import session(s) from 2 part(s)", result["message"])

    def test_targets_for_one_part_skip_the_other(self):
        result = self._ok(
            {"file_id": self._file().name, "parts": ["ledgers", "stock_items"], "targets": ["Items"]}
        )
        by_part = {r["part"]: r for r in result["results"]}
        self.assertIn("skipped", by_part["ledgers"])
        self.assertEqual([s["target"] for s in by_part["stock_items"]["sessions"]], ["Items"])

    def test_the_dry_run_accepts_a_customers_session(self):
        result = self._ok({"file_id": self._file().name, "parts": ["ledgers"]})
        customers = next(s for s in result["results"][0]["sessions"] if s["target"] == "Customers")
        reply = self.registry.get_tool("dry_run_import")._safe_execute({"session": customers["session_id"]})
        self.assertTrue(reply["success"], reply)
        self.assertEqual(reply["result"]["total_rows"], 1)


class TestRefusals(TallyXmlToolCase):
    def _refused(self, arguments, text):
        before = self._counts()
        reply = self._run(arguments)
        self.assertFalse(reply["success"], reply)
        self.assertIn(text, reply["error"])
        self.assertEqual(self._counts(), before)

    def test_a_non_tally_xml_is_refused_and_writes_nothing(self):
        doc = self._upload(b"<customers><customer><name>A</name></customer></customers>")
        self._refused({"file_id": doc.name}, "not a Tally export")

    def test_vouchers_only_is_refused(self):
        self._refused({"file_id": self._upload(export(VOUCHER)).name}, "no ledgers, groups or stock items")

    def test_bad_parts_are_refused(self):
        doc = self._file(ledger("A {tag}", "Sundry Debtors"), group("Sundry Debtors"))
        self._refused({"file_id": doc.name, "parts": ["groups"]}, "not imported")
        self._refused({"file_id": doc.name, "parts": ["vouchers"]}, "Importable parts")
        self._refused({"file_id": doc.name, "parts": ["stock_items"]}, "no stock items")
        self._refused({"file_id": doc.name, "parts": ["ledgers"], "targets": ["Nope"]}, "No target 'Nope'")

    def test_parts_must_be_a_list(self):
        # The framework's schema check answers a string first; the tool's own guard is called directly.
        doc = self._file(ledger("A {tag}", "Sundry Debtors"))
        before = self._counts()
        reply = self.registry.get_tool("apply_import_preset").execute(
            {"file_id": doc.name, "parts": "ledgers"}
        )
        self.assertFalse(reply["success"], reply)
        self.assertIn("parts must be a list", reply["error"])
        self.assertEqual(self._counts(), before)

    def test_preset_and_sheet_are_refused_for_xml(self):
        doc = self._file()
        self._refused({"file_id": doc.name, "preset": "tally_prime.ledgers"}, "not used for Tally XML")
        self._refused({"file_id": doc.name, "sheet": "Sheet1"}, "not used for Tally XML")

    def test_parts_on_a_spreadsheet_is_refused(self):
        doc = self._upload(b"Name,Under\nA,Sundry Debtors\n", file_name=f"ledgers-{self.tag}.csv")
        self._refused({"file_id": doc.name, "parts": ["ledgers"]}, "only for Tally XML")

    def test_a_user_who_cannot_read_the_file_gets_the_usual_refusal(self):
        doc = self._file()
        frappe.set_user(self.make_throwaway_user("outsider", roles=("Blogger",)))
        reply = self._run({"file_id": doc.name})
        self.assertFalse(reply["success"])
        self.assertIn("File not found, or you don't have access to it.", reply["error"])

    def test_a_csv_still_goes_the_spreadsheet_way(self):
        doc = self._upload(
            b"Name,Under,GSTIN/UIN\nA,Sundry Debtors,29ABCDE1234F1Z5\n", file_name=f"ledgers-{self.tag}.csv"
        )
        result = self._ok({"file_id": doc.name})
        self.assertNotIn("kind", result)
        self.assertEqual(result["preset"]["id"], "tally_prime.ledgers")
