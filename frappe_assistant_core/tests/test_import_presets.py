# frappe_assistant_core/tests/test_import_presets.py
"""
Import presets (#34000): recognising a source system's export by its headers and cleaning it.
This file covers the pure module: parsers, loading, matching and cleaning. The tool is tested in
test_apply_import_preset.py.
"""

import json
import os
import shutil
import tempfile
from unittest.mock import patch

import frappe

from frappe_assistant_core.plugins.core import import_presets as presets
from frappe_assistant_core.tests.base_test import BaseAssistantTest


class TestNormalise(BaseAssistantTest):
    def test_dashes_dots_case_and_spaces_are_one_form(self):
        self.assertEqual(presets.normalise("Ledger – Opening Balance"), "ledger opening balance")
        self.assertEqual(presets.normalise("  Ledger - Opening  Balance "), "ledger opening balance")
        self.assertEqual(presets.normalise("Ref. No."), "ref no")
        self.assertEqual(presets.normalise("Party's Name"), presets.normalise("Party’s Name"))
        self.assertEqual(presets.normalise(None), "")


class TestParse(BaseAssistantTest):
    def p(self, value, kind, **spec):
        return presets.parse(value, {"parse": kind, **spec})

    def test_amounts(self):
        cases = {
            "₹ 1,23,456.00": "123456",
            '"2,306,233.08"'.strip('"'): "2306233.08",
            "-$175,804.25": "-175804.25",
            "(1,250)": "-1250",
            "(-)1,250": "-1250",
            "INR 100.00": "100",
            "8,45,000 Dr": "845000",
            "1,25,000.00 Cr": "-125000",
            "1000Cr": "-1000",
            "596.25": "596.25",
            "0.00": "0",
        }
        for value, expected in cases.items():
            self.assertEqual(self.p(value, "amount"), expected, value)

    def test_amount_blank_stays_blank_and_text_stays_text(self):
        self.assertEqual(self.p("", "amount"), "")
        for text in ("n/a", "-", "Nil", "12abc"):
            self.assertEqual(self.p(text, "amount"), text)

    def test_credit_positive_flips_the_sign(self):
        self.assertEqual(self.p("5,000 Cr", "amount", sign="credit_positive"), "5000")
        self.assertEqual(self.p("5,000 Dr", "amount", sign="credit_positive"), "-5000")

    def test_debit_and_credit_split_one_dr_cr_column(self):
        self.assertEqual(self.p("8,45,000 Dr", "debit"), "845000")
        self.assertEqual(self.p("8,45,000 Dr", "credit"), "")
        self.assertEqual(self.p("1,250 Cr", "debit"), "")
        self.assertEqual(self.p("1,250 Cr", "credit"), "1250")

    def test_quantity_unit_and_rate(self):
        self.assertEqual(self.p("10 Nos", "quantity"), "10")
        self.assertEqual(self.p("1,250.000 Kgs", "quantity"), "1250")
        self.assertEqual(self.p("(-)10 Nos", "quantity"), "-10")
        self.assertEqual(self.p("10 Nos", "unit"), "Nos")
        self.assertEqual(self.p("1,250.000 Kgs", "unit"), "Kgs")
        self.assertEqual(self.p("12", "unit"), "")
        self.assertEqual(self.p("100.00/Nos", "rate"), "100")
        self.assertEqual(self.p("lots", "quantity"), "lots")

    def test_quantity_and_unit_keep_text_that_is_not_a_quantity(self):
        for text in ("1.2.3", "2024-04-01", "10-20"):
            self.assertEqual(self.p(text, "quantity"), text, text)
            self.assertEqual(self.p(text, "unit"), text, text)

    def test_amount_edge_cases(self):
        self.assertEqual(self.p("Dr", "amount"), "Dr")
        self.assertEqual(self.p("(  )", "amount"), "(  )")
        self.assertEqual(self.p("1.2.3", "amount"), "1.2.3")
        self.assertEqual(self.p("-", "amount"), "-")
        self.assertEqual(self.p("1,000.50 Dr.", "amount"), "1000.5")

    def test_dates(self):
        formats = ["d-MMM-yy", "d-MMM-yyyy", "dd/MM/yyyy"]
        self.assertEqual(self.p("1-Apr-24", "date", formats=formats), "2024-04-01")
        self.assertEqual(self.p("31-Mar-2025", "date", formats=formats), "2025-03-31")
        self.assertEqual(self.p("05/04/2024", "date", formats=formats), "2024-04-05")
        self.assertEqual(self.p("04/05/2024", "date", formats=["MM/dd/yyyy"]), "2024-04-05")
        self.assertEqual(self.p("2024-04-01", "date", formats=formats), "2024-04-01")
        self.assertEqual(self.p("someday", "date", formats=formats), "someday")

    def test_percent_and_segments(self):
        self.assertEqual(self.p("18%", "percent"), "18")
        self.assertEqual(self.p("GST 18%", "percent"), "18")
        self.assertEqual(self.p("IGST12.5", "percent"), "12.5")
        self.assertEqual(self.p("Truck:Depreciation", "last_segment"), "Depreciation")
        self.assertEqual(self.p("A:B:C", "parent_segment"), "B")
        self.assertEqual(self.p("Checking", "parent_segment"), "")
        self.assertEqual(self.p("1000 · Checking", "strip_number"), "Checking")
        self.assertEqual(self.p("1000 · Checking", "strip_number", keep="number"), "1000")
        self.assertEqual(self.p("Checking", "strip_number", keep="number"), "")

    def test_map_and_default(self):
        spec = {"values": {"Active": "0", "Inactive": "1"}}
        self.assertEqual(self.p("inactive", "map", **spec), "1")
        self.assertEqual(self.p("Archived", "map", **spec), "Archived")
        self.assertEqual(self.p("Archived", "map", default="0", **spec), "0")
        self.assertEqual(presets.parse("", {"default": "Nos"}), "Nos")
        self.assertEqual(presets.parse(" Kgs ", {"default": "Nos"}), "Kgs")

    def test_a_list_of_parsers_runs_in_order(self):
        spec = {"parse": ["last_segment", "strip_number"]}
        self.assertEqual(presets.parse("1000 · Assets:1010 · Checking", spec), "Checking")


MINIMAL = {
    "id": "test.customers",
    "system": "Test System",
    "export": "Customers",
    "signature": {"required": [["Client"], ["Client Code"]]},
    "columns": {"Client": {"from": ["Client"]}, "Code": {"from": ["Client Code"]}},
    "targets": [{"name": "Customers", "doctype": "Customer", "mapping": {"Client": "customer_name"}}],
}


def preset(**changes):
    out = json.loads(json.dumps(MINIMAL))
    out.update(changes)
    return out


class TestValidate(BaseAssistantTest):
    def test_a_minimal_preset_is_valid(self):
        self.assertIsNone(presets.validate_preset(preset()))

    def test_problems_are_named(self):
        cases = [
            (preset(id=""), "'id'"),
            (preset(signature={"required": []}), "signature.required"),
            (preset(header={"rows": 3}), "header.rows"),
            (preset(columns={"Client": {"from": ["Client"], "parse": "magic"}}), "unknown parse 'magic'"),
            (preset(targets=[]), "targets"),
            (
                preset(targets=[{"name": "X", "doctype": "Customer", "engine": "opening_stock"}]),
                "exactly one",
            ),
            (preset(targets=[{"name": "X", "engine": "teleport", "columns": {}}]), "unknown engine"),
            (
                preset(targets=[{"name": "X", "engine": "opening_invoices", "columns": {"Client": "party"}}]),
                "invoice_type",
            ),
            (
                preset(targets=[{"name": "X", "engine": "opening_stock", "columns": {"Client": "colour"}}]),
                "'colour'",
            ),
            (
                preset(targets=[{"name": "X", "doctype": "Customer", "mapping": {"Nope": "customer_name"}}]),
                "unknown column 'Nope'",
            ),
            (
                preset(
                    targets=[
                        {"name": "X", "doctype": "Customer", "when": {"column": "Client"}, "mapping": {}}
                    ]
                ),
                "when",
            ),
            (preset(groups={"method": "telepathy", "column": "Client"}), "groups.method"),
            (preset(distinct_by=["Missing"]), "distinct_by"),
        ]
        for bad, text in cases:
            problem = presets.validate_preset(bad)
            self.assertIsNotNone(problem, bad)
            self.assertIn(text, problem)

    def test_target_mapping_adds_constants_and_inputs(self):
        target = {
            "name": "Accounts",
            "doctype": "Account",
            "mapping": {"Client": "account_name"},
            "constants": [{"column": "Kind", "field": "account_type", "value": "Bank"}],
            "inputs": [{"name": "company", "column": "Company", "field": "company", "ask": "Which company?"}],
        }
        self.assertEqual(
            presets.target_mapping(target),
            {"Client": "account_name", "Kind": "account_type", "Company": "company"},
        )

    def test_engine_doctype(self):
        self.assertEqual(presets.engine_doctype({"engine": "opening_stock"}), "Stock Reconciliation")
        self.assertEqual(presets.engine_doctype({"engine": "opening_balances"}), "Journal Entry")
        self.assertEqual(
            presets.engine_doctype({"engine": "opening_invoices", "invoice_type": "Purchase"}),
            "Purchase Invoice",
        )


class TestLoad(BaseAssistantTest):
    def setUp(self):
        super().setUp()
        self.folder = tempfile.mkdtemp()
        presets.clear_cache()

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)
        presets.clear_cache()
        super().tearDown()

    def _write(self, name, content):
        path = os.path.join(self.folder, name)
        with open(path, "w") as f:
            f.write(content if isinstance(content, str) else json.dumps(content))
        return path

    def _hooks(self, entries):
        real = frappe.get_hooks

        def fake(hook=None, *args, **kwargs):
            if hook == presets.HOOK:
                return entries
            return real(hook, *args, **kwargs)

        return patch("frappe.get_hooks", side_effect=fake), patch(
            "frappe.get_app_path", side_effect=lambda app, *parts: os.path.join(self.folder, *parts)
        )

    def test_shipped_presets_all_validate(self):
        for name in sorted(os.listdir(presets.PRESET_DIR)):
            with open(os.path.join(presets.PRESET_DIR, name)) as f:
                content = json.load(f)
            for item in content:
                self.assertIsNone(presets.validate_preset(item), f"{name}: {item.get('id')}")

    def test_a_hook_folder_adds_presets_and_a_hook_id_replaces_one(self):
        os.mkdir(os.path.join(self.folder, "presets"))
        self._write("presets/a.json", [preset(id="partner.customers")])
        self._write("presets/b.json", [preset(export="Customers, partner edition")])
        # FAC's own folder is replaced by an empty one; MINIMAL is loaded first through the first hook entry.
        self._write("base.json", [preset()])
        hooks, app_path = self._hooks(
            [{"app": "fac_test", "path": "base.json"}, {"app": "partner_app", "path": "presets"}]
        )
        with hooks, app_path, patch.object(presets, "PRESET_DIR", os.path.join(self.folder, "none")):
            loaded = {p["id"]: p for p in presets.load_presets()}
        self.assertIn("partner.customers", loaded)
        self.assertEqual(loaded["test.customers"]["export"], "Customers, partner edition")

    def test_an_invalid_hook_preset_is_skipped_and_logged(self):
        self._write("bad.json", [preset(id="bad.one", targets=[]), preset(id="good.one")])
        hooks, app_path = self._hooks([{"app": "partner_app", "path": "bad.json"}])
        with hooks, app_path, patch.object(presets, "PRESET_DIR", os.path.join(self.folder, "none")), patch(
            "frappe.log_error"
        ) as log:
            ids = [p["id"] for p in presets.load_presets()]
        self.assertEqual(ids, ["good.one"])
        self.assertIn("bad.one", frappe.as_json(log.call_args))

    def test_a_broken_json_file_is_skipped_and_logged(self):
        self._write("broken.json", "{not json")
        hooks, app_path = self._hooks([{"app": "partner_app", "path": "broken.json"}])
        with hooks, app_path, patch.object(presets, "PRESET_DIR", os.path.join(self.folder, "none")), patch(
            "frappe.log_error"
        ) as log:
            self.assertEqual(presets.load_presets(), [])
        self.assertTrue(log.called)

    def test_loading_is_cached_for_the_request(self):
        first = presets.load_presets()
        with patch.object(presets, "_preset_files", side_effect=AssertionError("read again")):
            self.assertIs(presets.load_presets(), first)
