"""
apply_import_preset (#34000): an export recognised by a preset is cleaned into one file per
target, and each opens an ordinary import session with the mapping applied. These tests use a
preset patched in; the shipped presets are tested at the end of this file.
"""

import io
import json
from unittest.mock import patch

import frappe
from openpyxl import Workbook

from frappe_assistant_core.core.tool_registry import get_tool_registry
from frappe_assistant_core.plugins.core import import_presets as presets
from frappe_assistant_core.plugins.core.import_session import import_target_problem
from frappe_assistant_core.plugins.core.tools import apply_import_preset as tool_module
from frappe_assistant_core.tests.base_test import BaseAssistantTest
from frappe_assistant_core.tests.test_import_presets import LEDGERS, TB


def xlsx(rows, sheet="Sheet1") -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = sheet
    for row in rows:
        worksheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


LEDGER_ROWS = [
    ["Acme Pvt Ltd"],
    ["Ledgers"],
    [],
    ["Name", "Under", "GSTIN", "Opening Balance"],
    ["Ravi Traders {tag}", "Sundry Debtors", "29ABCDE1234F1Z5", "1,000 Dr"],
    ["Mehta Supplies {tag}", "Sundry Creditors", "", "500 Cr"],
    ["HDFC Bank {tag}", "Bank Accounts", "", ""],
    ["Grand Total", "", "", "500"],
]


class PresetToolCase(BaseAssistantTest):
    def setUp(self):
        super().setUp()
        frappe.set_user("Administrator")
        self.registry = get_tool_registry()
        self.tag = frappe.generate_hash(length=6)
        self.files_before = set(frappe.get_all("File", pluck="name"))
        presets.clear_cache()

    def tearDown(self):
        frappe.set_user("Administrator")
        presets.clear_cache()
        for name in set(frappe.get_all("File", pluck="name")) - self.files_before:
            frappe.delete_doc("File", name, force=True, ignore_permissions=True)
        super().tearDown()

    def _upload(self, file_name, content):
        return frappe.get_doc(
            {"doctype": "File", "file_name": file_name, "content": content, "is_private": 1}
        ).insert()

    def _rows(self, rows):
        return [[str(c).format(tag=self.tag) for c in row] for row in rows]

    def _run(self, arguments, use=(LEDGERS,)):
        with patch.object(presets, "load_presets", return_value=list(use)):
            return self.registry.get_tool("apply_import_preset")._safe_execute(arguments)

    def _apply(self, arguments, use=(LEDGERS,)):
        reply = self._run(arguments, use)
        self.assertTrue(reply["success"], reply)
        return reply["result"]


class TestApply(PresetToolCase):
    def test_a_recognised_file_opens_one_mapped_session_per_target(self):
        file_doc = self._upload(f"ledgers-{self.tag}.xlsx", xlsx(self._rows(LEDGER_ROWS)))
        result = self._apply({"file_id": file_doc.name})
        self.assertTrue(result["matched"])
        self.assertEqual(result["preset"]["id"], "test.ledgers")
        self.assertIn("Test", result["message"])
        self.assertEqual(result["source"]["header_row"], 4)
        self.assertEqual(result["dropped"], {"title": 3, "totals": 1})
        by_target = {s["target"]: s for s in result["sessions"]}
        self.assertEqual(set(by_target), {"Customers", "Suppliers"})
        self.assertEqual(
            result["needs"],
            [{"target": "Accounts", "rows": 1, "name": "company", "ask": "Which company?"}],
        )

        customers = by_target["Customers"]
        session = frappe.get_doc("FAC Import Session", customers["session_id"])
        self.assertEqual(session.status, "Mapped")
        self.assertEqual(session.target_doctype, "Customer")
        options = json.loads(session.template_options)
        self.assertEqual(options["column_to_field_map"]["0"], "customer_name")
        self.assertEqual(options["column_to_field_map"]["4"], "customer_type")  # the constant column
        cleaned = frappe.get_doc("File", session.source_file)
        self.assertEqual(cleaned.attached_to_doctype, "FAC Import Session")
        self.assertEqual(cleaned.attached_to_name, session.name)
        content = cleaned.get_content()
        self.assertEqual(
            content.decode() if isinstance(content, bytes) else content,
            f"Name,Group,GSTIN,Opening,Customer Type\nRavi Traders {self.tag},Sundry Debtors,29ABCDE1234F1Z5,1000,Company\n",
        )
        self.assertEqual([s.step for s in session.steps], ["Read File", "Apply Preset", "Map Columns"])
        self.assertEqual(customers["sample_rows"][0][0], f"Ravi Traders {self.tag}")
        self.assertEqual(customers["next"], "dry_run_import")

    def test_a_second_call_with_the_input_opens_only_the_named_target(self):
        file_doc = self._upload(f"ledgers-{self.tag}.xlsx", xlsx(self._rows(LEDGER_ROWS)))
        self._apply({"file_id": file_doc.name})
        before = frappe.db.count("FAC Import Session")
        company = frappe.get_all("Company", pluck="name", limit=1)[0]
        result = self._apply(
            {"file_id": file_doc.name, "values": {"company": company}, "targets": ["Accounts"]}
        )
        if import_target_problem("Account"):
            # Account can't be imported on this site: the target is skipped with the reason.
            self.assertEqual(result["sessions"], [])
            self.assertEqual([s["target"] for s in result["skipped_targets"]], ["Accounts"])
            self.assertEqual(frappe.db.count("FAC Import Session"), before)
        else:
            self.assertEqual([s["target"] for s in result["sessions"]], ["Accounts"])
            self.assertEqual(frappe.db.count("FAC Import Session"), before + 1)
        self.assertNotIn("needs", result)

    def test_gstin_goes_to_the_first_field_this_site_has(self):
        file_doc = self._upload(f"ledgers-{self.tag}.xlsx", xlsx(self._rows(LEDGER_ROWS)))
        preset = json.loads(json.dumps(LEDGERS))
        preset["targets"][0]["mapping"]["GSTIN"] = ["gstin_not_here", "tax_id"]
        preset["targets"][0]["mapping"]["Opening"] = ["pan_not_here"]
        result = self._apply({"file_id": file_doc.name}, use=(preset,))
        customers = next(s for s in result["sessions"] if s["target"] == "Customers")
        session = frappe.get_doc("FAC Import Session", customers["session_id"])
        options = json.loads(session.template_options)
        self.assertEqual(options["column_to_field_map"]["2"], "tax_id")
        self.assertEqual(options["column_to_field_map"]["3"], "Don't Import")
        self.assertIn("Opening (pan_not_here)", result["not_on_this_site"])

    def test_an_unverified_preset_carries_a_warning(self):
        file_doc = self._upload(f"ledgers-{self.tag}.xlsx", xlsx(self._rows(LEDGER_ROWS)))
        result = self._apply({"file_id": file_doc.name})
        self.assertFalse(result["preset"]["verified"])
        self.assertTrue(any("not checked against a real" in w for w in result["warnings"]))

    def test_an_engine_target_returns_columns_for_its_setup_tool(self):
        tb = json.loads(json.dumps(TB))
        rows = [
            ["Acme"],
            ["Trial Balance"],
            ["Particulars", "Debit", "Credit"],
            ["Cash", "100", ""],
            ["Capital", "", "100"],
            ["Grand Total", "100", "100"],
        ]
        file_doc = self._upload(f"tb-{self.tag}.csv", "\n".join(",".join(r) for r in rows).encode())
        result = self._apply({"file_id": file_doc.name}, use=(tb,))
        session = result["sessions"][0]
        self.assertEqual(session["engine"], "opening_balances")
        self.assertEqual(session["columns"], {"Account": "account", "Debit": "debit", "Credit": "credit"})
        self.assertEqual(session["next"], "set_opening_balances")
        doc = frappe.get_doc("FAC Import Session", session["session_id"])
        self.assertEqual(doc.status, "File Read")
        self.assertIsNone(doc.target_doctype)

    def test_no_match_writes_nothing(self):
        file_doc = self._upload(f"plain-{self.tag}.csv", b"Customer Name,Customer Group\nA,Retail\n")
        before = frappe.db.count("FAC Import Session"), frappe.db.count("File")
        result = self._apply({"file_id": file_doc.name})
        self.assertFalse(result["matched"])
        self.assertIn("start_import_session", result["message"])
        self.assertEqual((frappe.db.count("FAC Import Session"), frappe.db.count("File")), before)

    def test_a_named_preset_that_does_not_fit_says_which_headers_are_missing(self):
        file_doc = self._upload(f"plain-{self.tag}.csv", b"Name,GSTIN\nA,B\n")
        result = self._apply({"file_id": file_doc.name, "preset": "test.ledgers"})
        self.assertFalse(result["matched"])
        self.assertEqual(result["missing_headers"], [["Under", "Group Name"]])

    def test_an_unknown_preset_is_refused_with_the_list(self):
        file_doc = self._upload(f"plain-{self.tag}.csv", b"Name,Under\nA,B\n")
        reply = self._run({"file_id": file_doc.name, "preset": "test.nothing"})
        self.assertFalse(reply["success"])
        self.assertIn("test.ledgers", reply["error"])

    def test_only_title_and_totals_left_is_an_error(self):
        rows = [["Title"], ["Name", "Under"], ["Grand Total", ""]]
        file_doc = self._upload(f"empty-{self.tag}.csv", "\n".join(",".join(r) for r in rows).encode())
        reply = self._run({"file_id": file_doc.name})
        self.assertFalse(reply["success"])
        self.assertIn("no data rows", reply["error"])

    def test_a_user_who_cannot_read_the_file_gets_the_usual_refusal(self):
        file_doc = self._upload(f"ledgers-{self.tag}.xlsx", xlsx(self._rows(LEDGER_ROWS)))
        frappe.set_user(self.make_throwaway_user("outsider", roles=("Blogger",)))
        before = frappe.db.count("FAC Import Session")
        reply = self._run({"file_id": file_doc.name})
        self.assertFalse(reply["success"])
        self.assertIn("File not found, or you don't have access to it.", reply["error"])
        frappe.set_user("Administrator")
        self.assertEqual(frappe.db.count("FAC Import Session"), before)

    def test_a_wrong_file_type_gets_the_usual_refusal(self):
        file_doc = self._upload(f"notes-{self.tag}.txt", b"Name,Under\n")
        reply = self._run({"file_id": file_doc.name})
        self.assertFalse(reply["success"])
        self.assertIn("Only .csv, .xlsx or .xls", reply["error"])

    def test_a_target_the_user_cannot_import_is_skipped_with_the_reason(self):
        file_doc = self._upload(f"ledgers-{self.tag}.xlsx", xlsx(self._rows(LEDGER_ROWS)))

        def refuse_supplier(doctype):
            return "no Supplier for you" if doctype == "Supplier" else None

        with patch.object(tool_module, "import_target_problem", side_effect=refuse_supplier):
            result = self._apply({"file_id": file_doc.name})
        self.assertEqual([s["target"] for s in result["sessions"]], ["Customers"])
        self.assertEqual(
            result["skipped_targets"],
            [{"target": "Suppliers", "rows": 1, "reason": "no Supplier for you"}],
        )

    def test_set_column_mapping_still_changes_a_column_afterwards(self):
        file_doc = self._upload(f"ledgers-{self.tag}.xlsx", xlsx(self._rows(LEDGER_ROWS)))
        session = next(
            s for s in self._apply({"file_id": file_doc.name})["sessions"] if s["target"] == "Customers"
        )
        reply = self.registry.get_tool("set_column_mapping")._safe_execute(
            {"session": session["session_id"], "mapping": {"Group": "customer_group"}}
        )
        self.assertTrue(reply["success"], reply)
        options = json.loads(frappe.get_doc("FAC Import Session", session["session_id"]).template_options)
        self.assertEqual(options["column_to_field_map"]["1"], "customer_group")
        self.assertEqual(options["column_to_field_map"]["0"], "customer_name")

    def test_two_columns_with_the_same_fallback_do_not_take_the_same_field(self):
        file_doc = self._upload(f"ledgers-{self.tag}.xlsx", xlsx(self._rows(LEDGER_ROWS)))
        preset = json.loads(json.dumps(LEDGERS))
        preset["targets"][0]["mapping"]["GSTIN"] = ["gstin_not_here", "tax_id"]
        preset["targets"][0]["mapping"]["Opening"] = ["gstin_not_here", "tax_id"]
        result = self._apply({"file_id": file_doc.name}, use=(preset,))
        customers = next(s for s in result["sessions"] if s["target"] == "Customers")
        self.assertNotIn("mapping_problems", customers)
        self.assertEqual(customers["next"], "dry_run_import")
        options = json.loads(frappe.get_doc("FAC Import Session", customers["session_id"]).template_options)
        self.assertEqual(options["column_to_field_map"]["2"], "tax_id")
        self.assertEqual(options["column_to_field_map"]["3"], "Don't Import")
        self.assertIn("Opening (gstin_not_here / tax_id)", result["not_on_this_site"])

    def test_a_mapping_that_cannot_be_saved_points_to_set_column_mapping(self):
        file_doc = self._upload(f"ledgers-{self.tag}.xlsx", xlsx(self._rows(LEDGER_ROWS)))
        with (
            patch.object(tool_module, "resolve_mapping", return_value=({}, ["Column X has no field"])),
            patch.object(tool_module, "duplicate_targets", return_value=[]),
        ):
            result = self._apply({"file_id": file_doc.name})
        customers = next(s for s in result["sessions"] if s["target"] == "Customers")
        self.assertEqual(customers["mapping_problems"], ["Column X has no field"])
        self.assertNotIn("mapping", customers)
        self.assertEqual(customers["next"], "set_column_mapping")

    def test_a_failure_opening_one_target_leaves_the_others_and_no_half_writes(self):
        file_doc = self._upload(f"ledgers-{self.tag}.xlsx", xlsx(self._rows(LEDGER_ROWS)))
        real_save = tool_module.save_mapping

        def save_unless_supplier(session, doctype, *args, **kwargs):
            if doctype == "Supplier":
                raise ValueError("cannot save the Supplier mapping")
            return real_save(session, doctype, *args, **kwargs)

        with patch.object(tool_module, "save_mapping", side_effect=save_unless_supplier):
            result = self._apply({"file_id": file_doc.name})
        self.assertEqual([s["target"] for s in result["sessions"]], ["Customers"])
        self.assertEqual(
            result["skipped_targets"],
            [{"target": "Suppliers", "rows": 1, "reason": "cannot save the Supplier mapping"}],
        )
        self.assertEqual(frappe.db.count("FAC Import Session", {"target_doctype": "Supplier"}), 0)
        self.assertEqual(
            frappe.db.count("File", {"file_name": ["like", f"ledgers-{self.tag} - Suppliers%"]}), 0
        )
        self.assertEqual(
            frappe.db.count("File", {"file_name": ["like", f"ledgers-{self.tag} - Customers%"]}), 1
        )

    def test_an_engine_doctype_missing_from_the_site_is_skipped(self):
        rows = [
            ["Acme"],
            ["Trial Balance"],
            ["Particulars", "Debit", "Credit"],
            ["Cash", "100", ""],
            ["Capital", "", "100"],
        ]
        file_doc = self._upload(f"tb-{self.tag}.csv", "\n".join(",".join(r) for r in rows).encode())
        with patch.object(presets, "engine_doctype", return_value="No Such Doctype"):
            result = self._apply({"file_id": file_doc.name}, use=(TB,))
        self.assertEqual(result["sessions"], [])
        self.assertEqual(
            result["skipped_targets"][0]["reason"], "No Such Doctype isn't available on this site."
        )

    def test_an_unknown_target_name_is_refused_and_writes_nothing(self):
        file_doc = self._upload(f"ledgers-{self.tag}.xlsx", xlsx(self._rows(LEDGER_ROWS)))
        before = frappe.db.count("FAC Import Session"), frappe.db.count("File")
        reply = self._run({"file_id": file_doc.name, "targets": ["Nope"]})
        self.assertFalse(reply["success"])
        self.assertIn(
            "No target 'Nope'. Targets of test.ledgers: Customers, Suppliers, Accounts.", reply["error"]
        )
        self.assertEqual((frappe.db.count("FAC Import Session"), frappe.db.count("File")), before)
