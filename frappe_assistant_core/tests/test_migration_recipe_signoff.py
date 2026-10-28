# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""The sign-off report covers a delta run (#34420 P2.5, RC-6): what came in after the cut-off, and the
books at go-live against the old system's closing totals."""

import unittest

import frappe
from frappe.utils import today

from frappe_assistant_core.tests.migration_recipe_case import SESSION, DeltaRecipeCase
from frappe_assistant_core.utils import import_signoff


def section(data, name):
    return [row for row in data if row.get("section") == name]


@unittest.skipUnless("erpnext" in frappe.get_installed_apps(), "needs ERPNext's invoices and ledgers")
class TestSignOffCoversTheDelta(DeltaRecipeCase):
    def setUp(self):
        super().setUp()
        self.recipe = self._delta_recipe()
        self.company = (
            frappe.defaults.get_global_default("company") or frappe.get_all("Company", pluck="name")[0]
        )

    def _delta_session(self):
        invoices = self._invoices("I1,2026-03-30,C", "I2,2026-03-31,C", "I3,2026-04-01,C")
        self.assertTrue(self._delta(invoices)["success"])
        return frappe.get_doc(SESSION, invoices)

    # -- since the cut-off ----------------------------------------------------------------------

    def test_a_delta_run_has_its_own_section_with_the_rows_before_the_cut_off_counted_apart(self):
        session = self._delta_session()

        report = import_signoff.build([session])

        since = report["since_cut_off"]
        self.assertEqual(since["since"], self.CUT_OFF)
        [entry] = since["doctypes"]
        self.assertEqual((entry["label"], entry["source"], entry["before"]), ("Sales Invoice", 3, 2))
        self.assertEqual(entry["excluded"], 0)  # the two before the cut-off aren't "left out" twice

    def test_the_section_is_in_the_report_rows_and_the_document(self):
        report = import_signoff.build([self._delta_session()])

        [row] = section(import_signoff.rows(report), "Since Cut-off")

        self.assertEqual((row["item"], row["source_rows"]), ("Sales Invoice", 3))
        self.assertIn("2 more rows are dated on or before", row["details"])
        self.assertIn("## Since Cut-off", import_signoff.document(report))

    def test_a_migration_with_no_delta_run_has_no_such_section(self):
        invoices = self._invoices("I1,2026-04-01,C")
        self._run("migration_recipe", {"action": "run", "recipe": self.recipe.name, "sessions": [invoices]})

        report = import_signoff.build([frappe.get_doc(SESSION, invoices)])

        self.assertIsNone(report["since_cut_off"])
        self.assertEqual(section(import_signoff.rows(report), "Since Cut-off"), [])

    def test_the_opening_plan_and_its_delta_plan_are_one_sign_off(self):
        first, _groups = self._finished_plan()
        delta = self._delta_session()

        names = import_signoff.plan_sessions(f"{first}, {delta.migration_plan}")

        self.assertIn(delta.name, names)
        self.assertIn(_groups, names)

    def test_a_plan_that_does_not_exist_is_still_refused(self):
        with self.assertRaises(frappe.DoesNotExistError):
            import_signoff.plan_sessions("no-such-plan")

    # -- at go-live -----------------------------------------------------------------------------

    def _erpnext_figures(self):
        check = import_signoff.go_live_check({"company": self.company, "as_of": today()})
        return {item["key"]: item["erpnext"] for item in check["items"]}

    def test_every_total_is_shown_from_erpnext_even_when_the_old_system_gave_none(self):
        check = import_signoff.go_live_check({"company": self.company, "as_of": today()})

        self.assertEqual(
            [i["key"] for i in check["items"]], ["receivables", "payables", "stock_value", "debit", "credit"]
        )
        self.assertTrue(all(i["old_system"] is None and i["matches"] is None for i in check["items"]))

    def test_totals_that_agree_match(self):
        figures = self._erpnext_figures()

        check = import_signoff.go_live_check({"company": self.company, "as_of": today(), **figures})

        self.assertTrue(all(i["matches"] for i in check["items"]), check)
        self.assertTrue(all(i["difference"] == 0 for i in check["items"]))

    def test_a_total_that_differs_is_an_open_point_naming_both_figures(self):
        figures = self._erpnext_figures()
        closing = {
            "company": self.company,
            "as_of": today(),
            **figures,
            "receivables": figures["receivables"] + 125,
        }
        report = import_signoff.build([], closing)

        item = next(i for i in report["go_live"]["items"] if i["key"] == "receivables")

        self.assertFalse(item["matches"])
        self.assertEqual(item["difference"], -125)
        self.assertTrue(
            any("receivables" in p and "difference" in p for p in report["open_points"]),
            report["open_points"],
        )
        self.assertFalse(report["ready_to_sign"])

    def test_a_total_not_given_is_an_open_point_too(self):
        report = import_signoff.build([], {"company": self.company, "as_of": today(), "receivables": 0})

        self.assertTrue(
            any("payables wasn't given" in p for p in report["open_points"]), report["open_points"]
        )

    def test_the_go_live_rows_are_in_the_report(self):
        figures = self._erpnext_figures()
        report = import_signoff.build([], {"company": self.company, "as_of": today(), **figures})

        rows = section(import_signoff.rows(report), "Go-live Check")

        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["company"], self.company)

    def test_the_filters_ask_for_it_and_without_a_date_there_is_none(self):
        self.assertIsNone(import_signoff.closing_totals(frappe._dict()))
        closing = import_signoff.closing_totals(
            frappe._dict(go_live_date="2026-04-30", company=self.company, closing_payables="10")
        )
        self.assertEqual(
            (closing["company"], closing["as_of"], closing["payables"]), (self.company, "2026-04-30", "10")
        )

    def test_the_report_runs_with_the_go_live_filters(self):
        figures = self._erpnext_figures()
        filters = {
            "go_live_date": today(),
            "company": self.company,
            **{f"closing_{key}": value for key, value in figures.items()},
        }

        _columns, rows, _message, _chart, _cards = import_signoff.execute(filters)

        self.assertEqual(len(section(rows, "Go-live Check")), 5)

    def test_a_user_who_cannot_read_the_ledger_is_told_not_shown_its_figures(self):
        frappe.set_user(self.make_desk_user("noledger"))

        check = import_signoff.go_live_check({"company": self.company, "as_of": today(), "receivables": 1})

        self.assertIn("problem", check)
        self.assertEqual(check["items"], [])
