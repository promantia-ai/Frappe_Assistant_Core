# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
The go-live readiness check (#34424), the ERPNext report "Go-Live Readiness": each item Pass, Warn or
Fail with the fix, failures first, and the checklist as a document with a sign-off block for the PDF.
FACO runs it with generate_report. The gaps are made the way a user makes them on a real site.
"""

import os
import tempfile
import unittest
from unittest.mock import patch

import frappe
from frappe.modules.import_file import import_file_by_path
from frappe.utils import add_days, get_datetime, nowdate

from frappe_assistant_core.core.tool_registry import get_tool_registry
from frappe_assistant_core.tests.base_test import BaseAssistantTest
from frappe_assistant_core.tests.test_import_opening_stock import refuse_sql_functions
from frappe_assistant_core.utils import go_live_check

REPORT = "Go-Live Readiness"


@unittest.skipUnless("erpnext" in frappe.get_installed_apps(), "needs ERPNext")
class TestGoLiveCheck(BaseAssistantTest):
    def setUp(self):
        super().setUp()
        frappe.set_user("Administrator")
        self.tag = frappe.generate_hash(length=6)
        self.company = frappe.get_all("Company", pluck="name", order_by="creation", limit=1)[0]
        if not frappe.db.exists("Report", REPORT):
            # bench migrate makes the report from this file; a test site may not be migrated yet
            path = os.path.join(
                os.path.dirname(go_live_check.__file__),
                "..",
                "assistant_core",
                "report",
                "go_live_readiness",
                "go_live_readiness.json",
            )
            import_file_by_path(os.path.abspath(path), force=True)

    def _rows(self, **filters):
        return go_live_check.execute({"company": self.company, **filters})[1]

    def _row(self, check, **filters):
        found = [r for r in self._rows(**filters) if r["check"] == check]
        self.assertEqual(len(found), 1, [r["check"] for r in self._rows(**filters)])
        return found[0]

    def _account(self, name, **extra):
        parent = frappe.db.get_value(
            "Account", {"company": self.company, "root_type": "Asset", "is_group": 1}, order_by="lft"
        )
        return (
            frappe.get_doc(
                {
                    "doctype": "Account",
                    "account_name": f"{name} {self.tag}",
                    "company": self.company,
                    "parent_account": parent,
                    **extra,
                }
            )
            .insert()
            .name
        )

    # -- through FACO -------------------------------------------------------------------------

    def test_generate_report_runs_it_with_pass_warn_or_fail_and_a_fix_for_each_gap(self):
        reply = (
            get_tool_registry()
            .get_tool("generate_report")
            ._safe_execute({"report_name": REPORT, "filters": {"company": self.company}})
        )
        self.assertTrue(reply["success"], reply)
        result = reply["result"]
        data = result.get("data") or result.get("result")
        self.assertTrue(data)
        for row in data:
            self.assertIn(row["status"], go_live_check.STATUSES)
            if row["status"] != go_live_check.PASS:
                self.assertTrue(row["fix"], row)
        self.assertIn("pass", str(result.get("message")))

    def test_failures_come_first_then_warnings(self):
        order = [go_live_check.STATUSES.index(r["status"]) for r in self._rows()]
        self.assertEqual(order, sorted(order))

    def test_with_document_the_message_is_the_checklist_with_a_place_to_sign(self):
        message = go_live_check.execute({"company": self.company, "document": 1})[2]
        self.assertIn("| Status | Area | Check | Detail | Fix |", message)
        self.assertIn("## Sign-off", message)
        self.assertIn("| Client | | | |", message)
        self.assertEqual(
            message.count("\n| Pass |") + message.count("\n| Warn |") + message.count("\n| Fail |"),
            len(self._rows()),
        )

    def test_no_query_asks_get_all_for_a_sql_function(self):
        with patch.object(frappe, "get_all", refuse_sql_functions(frappe.get_all)):
            self.assertTrue(self._rows())

    # -- each check ---------------------------------------------------------------------------

    def test_a_disabled_default_receivable_account_fails(self):
        # A user disables the account the company still names as its default
        account = self._account("Debtors Old", account_type="Receivable")
        frappe.db.set_value("Account", account, "disabled", 1)
        frappe.db.set_value("Company", self.company, "default_receivable_account", account)
        row = self._row("Receivable and payable accounts")
        self.assertEqual(row["status"], go_live_check.FAIL)
        self.assertIn("is disabled", row["detail"])

    def test_no_fiscal_year_for_today_fails(self):
        with patch.object(go_live_check, "nowdate", return_value="2099-06-01"):
            row = self._row("Fiscal year covers today")
        self.assertEqual(row["status"], go_live_check.FAIL)

    def test_the_next_number_already_taken_by_an_imported_document_fails(self):
        # An import that kept the old system's numbers in ERPNext's series leaves the counter behind
        upcoming = go_live_check._next_name(go_live_check._default_series("Material Request"))
        item = frappe.get_all("Item", filters={"disabled": 0, "is_stock_item": 1}, pluck="name", limit=1)
        warehouse = frappe.db.get_value("Warehouse", {"company": self.company, "is_group": 0})
        if not item or not warehouse:
            self.skipTest("needs a stock item and a warehouse")
        frappe.get_doc(
            {
                "doctype": "Material Request",
                "material_request_type": "Purchase",
                "company": self.company,
                "schedule_date": nowdate(),
                "items": [
                    {"item_code": item[0], "qty": 1, "warehouse": warehouse, "schedule_date": nowdate()}
                ],
            }
        ).insert(set_name=upcoming)
        row = self._row("Next numbers are free")
        self.assertEqual(row["status"], go_live_check.FAIL)
        self.assertIn(upcoming, row["detail"])

    def test_a_draft_opening_journal_entry_fails(self):
        account, other = self._account("Opening Cash"), self._account("Opening Bank")
        frappe.get_doc(
            {
                "doctype": "Journal Entry",
                "company": self.company,
                "posting_date": nowdate(),
                "is_opening": "Yes",
                "accounts": [
                    {"account": account, "debit_in_account_currency": 10},
                    {"account": other, "credit_in_account_currency": 10},
                ],
            }
        ).insert()
        row = self._row("Opening entries posted")
        self.assertEqual(row["status"], go_live_check.FAIL)
        self.assertIn("1 Journal Entry", row["detail"])

    def test_a_user_with_no_role_is_a_warning(self):
        user = self.make_throwaway_user("golive")
        row = self._row("Every user has a role")
        self.assertEqual(row["status"], go_live_check.WARN)
        self.assertIn(user, row["detail"])

    def test_a_user_with_a_role_is_not_listed(self):
        user = self.make_throwaway_user("golive", roles=("Accounts User",))
        self.assertNotIn(user, self._row("Every user has a role")["detail"])

    def test_a_tax_category_no_template_uses_is_a_warning(self):
        category = frappe.get_doc({"doctype": "Tax Category", "title": f"Export {self.tag}"}).insert().name
        row = self._row("Tax categories")
        self.assertEqual(row["status"], go_live_check.WARN)
        self.assertIn(category, row["detail"])

    def test_negative_stock_allowed_is_a_warning(self):
        if not go_live_check._uses_stock():
            self.skipTest("the site has no stock item")
        frappe.db.set_single_value("Stock Settings", "allow_negative_stock", 1)
        frappe.clear_document_cache("Stock Settings", "Stock Settings")
        self.assertEqual(self._row("Negative stock not allowed")["status"], go_live_check.WARN)

    def test_backups_older_than_two_days_or_none_are_a_warning(self):
        with tempfile.TemporaryDirectory() as folder:
            backup = os.path.join(folder, "20260101_000000-site-database.sql.gz")
            open(backup, "w").close()
            old = get_datetime(add_days(nowdate(), -5)).timestamp()
            os.utime(backup, (old, old))
            with patch.object(go_live_check, "backup_files", autospec=True, return_value=[backup]):
                self.assertIn("5 days old", self._row("Recent backup")["detail"])
            os.utime(backup, None)
            with patch.object(go_live_check, "backup_files", autospec=True, return_value=[backup]):
                self.assertEqual(self._row("Recent backup")["status"], go_live_check.PASS)
        with patch.object(go_live_check, "backup_files", autospec=True, return_value=[]):
            self.assertEqual(self._row("Recent backup")["status"], go_live_check.WARN)

    def test_the_scheduler_off_fails(self):
        with patch("frappe.utils.scheduler.is_scheduler_inactive", autospec=True, return_value=True):
            self.assertEqual(self._row("Scheduler on")["status"], go_live_check.FAIL)
        with patch("frappe.utils.scheduler.is_scheduler_inactive", autospec=True, return_value=False):
            self.assertEqual(self._row("Scheduler on")["status"], go_live_check.PASS)

    def test_an_unknown_company_is_refused(self):
        with self.assertRaises(frappe.ValidationError):
            go_live_check.execute({"company": f"No Such Company {self.tag}"})
