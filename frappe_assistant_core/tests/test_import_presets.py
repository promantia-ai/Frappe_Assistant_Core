# frappe_assistant_core/tests/test_import_presets.py
"""
Import presets (#34000): recognising a source system's export by its headers and cleaning it.
This file covers the pure module: parsers, loading, matching and cleaning. The tool is tested in
test_apply_import_preset.py.
"""

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
