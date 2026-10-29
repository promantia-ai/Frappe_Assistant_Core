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

from frappe_assistant_core.tests.migration_recipe_case import RECIPE, SESSION, DeltaRecipeCase, step
from frappe_assistant_core.utils import import_dry_run as dry_run
from frappe_assistant_core.utils import (
    import_opening_balances,
    import_opening_invoices,
    import_opening_stock,
    import_plan,
)

CUT_OFF = "2026-03-31"


@unittest.skipUnless("erpnext" in frappe.get_installed_apps(), "needs ERPNext's invoices and entries")
class TestDeltaRun(DeltaRecipeCase):
    def setUp(self):
        super().setUp()
        self.recipe = self._delta_recipe()

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
        self.assertEqual(ran["result"]["applied"][0]["left_out"], {"before": 2, "opening": 0, "again": 0})

    def test_day_first_dates_are_read_as_the_import_reads_them(self):
        # 11 March is before the cut-off and 1 April after it; month first they swap sides
        invoices = self._invoices("I1,11-03-2026,C", "I2,01-04-2026,C")

        self._delta(invoices)

        self.assertEqual(self._excluded(invoices), [2])

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

    def test_a_master_already_in_erpnext_is_left_out_not_asked_about(self):
        old = frappe.get_doc(
            {
                "doctype": "Item Group",
                "item_group_name": f"Old {self.tag}",
                "parent_item_group": "All Item Groups",
            }
        ).insert()
        groups = self._csv(
            "Groups", "Name,Parent", f"{old.name},All Item Groups", f"New {self.tag},All Item Groups"
        )

        ran = self._delta(groups, self._invoices("I1,2026-04-01,C"), masters=[1])["result"]

        self.assertEqual(self._excluded(groups), [2])
        self.assertIn("Already in ERPNext", self._reasons(groups)["2"])
        entry = next(a for a in ran["applied"] if a["session"] == groups)
        self.assertEqual(entry["left_out"], {"existing": 1})
        checked = self._run("dry_run_import", {"session": groups})["result"]
        self.assertFalse(checked.get("groups"), checked)

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
        plan = self._run(
            "migration_recipe", {"action": "run", "recipe": self.recipe.name, "sessions": [invoices]}
        )["result"]["plan"]

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

    # -- the same delta run twice ---------------------------------------------------------------

    def test_the_same_delta_run_twice_posts_each_entry_once(self):
        company = frappe.get_all("Company", pluck="name", limit=1)[0]
        cash, other = frappe.get_all(
            "Account",
            filters={"company": company, "is_group": 0, "account_type": ["in", ["Cash", "Bank"]]},
            pluck="name",
            limit=2,
        )
        journals = frappe.get_doc(
            {
                "doctype": RECIPE,
                "title": f"Journals {self.tag}",
                "steps": [
                    step(
                        1,
                        "Journals",
                        "Journal Entry",
                        ["Voucher", "Booked", "Company", "Account", "Debit", "Credit"],
                        {
                            "0": "bill_no",
                            "1": "posting_date",
                            "2": "company",
                            "3": "accounts.account",
                            "4": "accounts.debit_in_account_currency",
                            "5": "accounts.credit_in_account_currency",
                        },
                        is_transaction_step=1,
                    )
                ],
            }
        ).insert()
        lines = (
            "Voucher,Booked,Company,Account,Debit,Credit\n"
            f"JV {self.tag},2026-04-02,{company},{cash},10,0\n"
            f",,,{other},0,10\n"
        ).encode()

        def delta():
            session = self._opened("Journals.csv", lines)
            ran = self._run(
                "migration_recipe",
                {"action": "run", "recipe": journals.name, "sessions": [session], "since": CUT_OFF},
            )
            self.assertTrue(ran["success"], ran)
            return session, ran["result"]

        first, ran = delta()
        answer = [{"id": "plan:failure_rule", "rule": "stop"}]
        checked = self._run("dry_run_import", {"plan": ran["plan"], "answers": answer})
        self.assertEqual(import_plan.get_plan(ran["plan"]).status, "Ready to Run", checked)
        self._approve(ran["plan"])
        self.assertEqual(frappe.db.count("Journal Entry", {"bill_no": f"JV {self.tag}"}), 1)

        again, ran = delta()

        self.assertEqual(ran["applied"][0]["left_out"]["again"], 2)
        self.assertEqual(self._excluded(again), [2, 3])
        self.assertIn("earlier delta run", self._reasons(again)["2"])
