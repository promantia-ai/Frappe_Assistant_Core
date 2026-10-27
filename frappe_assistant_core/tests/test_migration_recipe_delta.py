# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""A delta run: the transactions made after the cut-off (#34420 P2.4, RC-5, M-D4 to M-D6)."""

import json
import unittest

import frappe

from frappe_assistant_core.tests.migration_recipe_case import RECIPE, SESSION, RecipeCase
from frappe_assistant_core.utils import import_dry_run as dry_run
from frappe_assistant_core.utils import (
    import_opening_balances,
    import_opening_invoices,
    import_opening_stock,
    import_plan,
)

CUT_OFF = "2026-03-31"


def _step(position, sheet, target, headers, mapping, **extra):
    return {
        "position": position,
        "sheet_name": sheet,
        "original_file_name": f"{sheet}.csv",
        "target_doctype": target,
        "import_type": "Insert New Records",
        "header_signature": json.dumps(headers),
        "column_map": json.dumps(mapping),
        **extra,
    }


@unittest.skipUnless("erpnext" in frappe.get_installed_apps(), "needs ERPNext's invoices and entries")
class TestDeltaRun(RecipeCase):
    def setUp(self):
        super().setUp()
        self.recipe = frappe.get_doc(
            {
                "doctype": RECIPE,
                "title": "Cut-over",
                "steps": [
                    _step(
                        1,
                        "Groups",
                        "Item Group",
                        ["Name", "Parent"],
                        {"0": "item_group_name", "1": "parent_item_group"},
                    ),
                    _step(
                        2,
                        "Invoices",
                        "Sales Invoice",
                        ["Invoice", "Date", "Customer"],
                        {"0": "po_no", "1": "posting_date", "2": "customer"},
                        is_transaction_step=1,
                    ),
                    _step(
                        3,
                        "Stock",
                        "Stock Entry",
                        ["Ref", "Moved", "Note"],
                        {"0": "remarks", "1": "posting_date"},
                        is_transaction_step=1,
                    ),
                    _step(
                        4,
                        "Journals",
                        "Journal Entry",
                        ["Voucher", "Booked", "Opening", "Account"],
                        {"0": "user_remark", "1": "posting_date", "2": "is_opening", "3": "accounts.account"},
                        is_transaction_step=1,
                    ),
                    _step(
                        5,
                        "Opening stock",
                        None,
                        ["Item", "Qty"],
                        {},
                        opening_engine="stock",
                        engine_settings=json.dumps({"warehouse": "Main"}),
                    ),
                ],
            }
        ).insert()

    def _csv(self, name, header, *rows):
        return self._opened(f"{name}.csv", ("\n".join([header, *rows]) + "\n").encode())

    def _invoices(self, *rows):
        return self._csv("Invoices", "Invoice,Date,Customer", *rows)

    def _delta(self, *sessions, since=CUT_OFF, **arguments):
        return self._run(
            "run_migration_recipe",
            {"recipe": self.recipe.name, "sessions": list(sessions), "since": since, **arguments},
        )

    def _excluded(self, session):
        return sorted(frappe.parse_json(frappe.db.get_value(SESSION, session, "excluded_rows") or "[]"))

    def _reasons(self, session):
        rules = frappe.parse_json(frappe.db.get_value(SESSION, session, "transformation_rules") or "{}")
        return rules.get("left_out") or {}

    # -- the cut-off filter ---------------------------------------------------------------------

    def test_rows_on_or_before_the_cut_off_are_left_out_with_the_reason(self):
        invoices = self._invoices("I1,2026-03-30,C", "I2,2026-03-31,C", "I3,2026-04-01,C", "I4,2026-06-30,C")

        ran = self._delta(invoices)

        self.assertTrue(ran["success"], ran)
        self.assertEqual(self._excluded(invoices), [2, 3])  # row 1 is the header
        self.assertIn("Before the cut-off", self._reasons(invoices)["2"])
        self.assertEqual(ran["result"]["applied"][0]["left_out"], {"before": 2, "opening": 0})

    def test_the_rows_under_a_parent_row_follow_its_date(self):
        journals = self._csv(
            "Journals",
            "Voucher,Booked,Opening,Account",
            "J1,2026-03-01,No,Cash",
            ",,,Debtors",
            "J2,2026-04-05,No,Cash",
            ",,,Creditors",
        )

        self._delta(journals)

        self.assertEqual(self._excluded(journals), [2, 3])

    def test_an_opening_journal_entry_is_left_out_whatever_its_date(self):
        journals = self._csv(
            "Journals",
            "Voucher,Booked,Opening,Account",
            "J1,2026-05-01,Yes,Cash",
            ",,,Debtors",
            "J2,2026-05-02,No,Cash",
        )

        ran = self._delta(journals)

        self.assertEqual(self._excluded(journals), [2, 3])
        self.assertIn("opening", self._reasons(journals)["2"].lower())
        self.assertEqual(ran["result"]["applied"][0]["left_out"]["opening"], 2)

    def test_a_date_nobody_can_read_is_not_left_out_for_the_dry_run_to_report(self):
        invoices = self._invoices("I1,not a date,C", "I2,2026-01-01,C")

        self._delta(invoices)

        self.assertEqual(self._excluded(invoices), [3])

    def test_a_file_with_no_posting_date_column_mapped_is_refused(self):
        self.recipe.steps[1].column_map = json.dumps({"0": "po_no", "2": "customer"})
        self.recipe.save()
        invoices = self._invoices("I1,2026-04-01,C")

        ran = self._delta(invoices)

        self.assertFalse(ran["success"])
        self.assertIn("Posting Date", ran["error"])

    def test_a_cut_off_that_is_not_a_date_is_refused(self):
        invoices = self._invoices("I1,2026-04-01,C")

        ran = self._delta(invoices, since="after the audit")

        self.assertFalse(ran["success"])
        self.assertIsNone(frappe.db.get_value(SESSION, invoices, "migration_plan"))

    # -- which steps run ------------------------------------------------------------------------

    def test_only_the_transaction_steps_run_and_the_rest_are_listed_as_skipped(self):
        groups = self._csv("Groups", "Name,Parent", "G,All Item Groups")
        invoices = self._invoices("I1,2026-04-01,C")

        ran = self._delta(groups, invoices)["result"]

        self.assertEqual([m["session"] for m in ran["matched"]], [invoices])
        self.assertEqual(ran["unmatched_sessions"], [groups])
        skipped = {entry["step"]: entry["reason"] for entry in ran["skipped_steps"]}
        self.assertEqual(sorted(skipped), [1, 5])
        self.assertIn("opening", skipped[5])

    def test_a_master_the_user_ticked_comes_in_insert_only(self):
        groups = self._csv("Groups", "Name,Parent", "G,All Item Groups")
        invoices = self._invoices("I1,2026-04-01,C")

        ran = self._delta(groups, invoices, masters=[1])["result"]

        self.assertEqual(sorted(m["session"] for m in ran["matched"]), sorted([groups, invoices]))
        self.assertEqual(frappe.db.get_value(SESSION, groups, "import_type"), "Insert New Records")
        self.assertFalse(frappe.db.get_value(SESSION, groups, "excluded_rows"))  # nothing is filtered by date

    def test_an_opening_step_is_never_run_in_a_delta_run(self):
        opening = self._csv("Opening stock", "Item,Qty", "A,1")

        ran = self._delta(opening)

        self.assertFalse(ran["success"])
        self.assertIsNone(frappe.db.get_value(SESSION, opening, "migration_plan"))

    def test_every_delta_document_is_submitted(self):
        invoices = self._invoices("I1,2026-04-01,C")

        self._delta(invoices)

        self.assertEqual(frappe.db.get_value(SESSION, invoices, "submit_after_import"), 1)

    # -- the order ------------------------------------------------------------------------------

    def test_the_files_go_in_the_order_of_a_delta_run(self):
        journals = self._csv("Journals", "Voucher,Booked,Opening,Account", "J1,2026-05-01,No,Cash")
        invoices = self._invoices("I1,2026-04-01,C")
        stock = self._csv("Stock", "Ref,Moved,Note", "S1,2026-04-02,x")
        groups = self._csv("Groups", "Name,Parent", "G,All Item Groups")

        plan = self._delta(journals, invoices, stock, groups, masters=[1])["result"]["plan"]

        placed = [entry["session"] for entry in import_plan.describe(import_plan.get_plan(plan))["files"]]
        self.assertEqual(placed, [groups, stock, invoices, journals])

    def test_the_plan_knows_it_is_a_delta_run(self):
        invoices = self._invoices("I1,2026-04-01,C")

        plan = self._delta(invoices)["result"]["plan"]

        self.assertEqual(import_plan.delta_since(frappe.get_doc(SESSION, invoices)), CUT_OFF)
        self.assertEqual(frappe.parse_json(import_plan.get_plan(plan).progress)["delta_since"], CUT_OFF)

    def test_a_plan_that_is_not_a_delta_run_is_not_one(self):
        invoices = self._invoices("I1,2026-04-01,C")
        plan = self._run("run_migration_recipe", {"recipe": self.recipe.name, "sessions": [invoices]})[
            "result"
        ]["plan"]

        self.assertIsNone(import_plan.delta_since(frappe.get_doc(SESSION, invoices)))
        self.assertNotIn("delta_since", import_plan.get_plan(plan).progress or "")

    # -- no opening entry -----------------------------------------------------------------------

    def test_the_opening_engines_refuse_a_file_of_a_delta_run(self):
        invoices = self._invoices("I1,2026-04-01,C")
        self._delta(invoices)
        session = dry_run.engine_session(invoices)

        for engine in (import_opening_stock, import_opening_invoices, import_opening_balances):
            with self.assertRaises(dry_run.DryRunProblem) as caught:
                engine.check_can_run(session)
            self.assertIn("delta run", str(caught.exception))
