# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Hypercare (#34426): the weekly summary of what is going wrong after go-live, the report FACO runs with
generate_report, and the daily job that sends it to the consultant and ends the period on its own.
"""

import random
import string
import unittest
from unittest.mock import patch

import frappe
from frappe.utils import add_days, get_datetime, getdate, now_datetime, nowdate

from frappe_assistant_core.core.tool_registry import get_tool_registry
from frappe_assistant_core.tests.base_test import BaseAssistantTest
from frappe_assistant_core.utils import hypercare

REPORT = "Hypercare Summary"


def _letters(k=8):
    # Summaries group messages with their numbers taken out, so a tag here is letters only
    return "".join(random.choices(string.ascii_lowercase, k=k))


class TestHypercare(BaseAssistantTest):
    def setUp(self):
        super().setUp()
        frappe.set_user("Administrator")
        self.tag = _letters()
        self.today = getdate(nowdate())
        self.consultant = self.make_throwaway_user("consultant", roles=("System Manager",))
        if not frappe.db.exists("Report", REPORT):
            # bench migrate makes the report from its file; a test site may not be migrated yet
            from frappe.modules.import_file import import_file_by_path

            path = frappe.get_app_path(
                "frappe_assistant_core",
                "assistant_core",
                "report",
                "hypercare_summary",
                "hypercare_summary.json",
            )
            import_file_by_path(path, force=True)

    def _hypercare(self, start, days=30, **extra):
        return frappe.get_doc(
            {
                "doctype": "FAC Hypercare",
                "consultant": self.consultant,
                "start_date": start,
                "days": days,
                **extra,
            }
        ).insert()

    def _rows(self, area, from_date=None, to_date=None):
        rows = hypercare.summary(from_date or add_days(self.today, -6), to_date or self.today)
        return [r for r in rows if r["area"] == hypercare.label(area)]

    def _age(self, doctype, name, days):
        # The record was made, and last touched, ``days`` ago
        when = get_datetime(add_days(now_datetime(), -days))
        frappe.db.set_value(doctype, name, {"creation": when, "modified": when}, update_modified=False)

    # -- FAC Hypercare -------------------------------------------------------------------------

    def test_a_hypercare_runs_for_its_days_from_the_start_date(self):
        record = self._hypercare(self.today)
        self.assertEqual(getdate(record.end_date), getdate(add_days(self.today, 29)))
        self.assertEqual(record.status, "Active")

    def test_a_hypercare_of_no_days_or_for_a_website_user_is_refused(self):
        with self.assertRaises(frappe.ValidationError):
            self._hypercare(self.today, days=0)
        self.consultant = self.make_throwaway_user("website")
        with self.assertRaises(frappe.ValidationError):
            self._hypercare(self.today)

    # -- each area of the summary ---------------------------------------------------------------

    def test_the_same_error_again_and_again_is_one_row_with_a_fix(self):
        title = f"Fiscal Year missing for {self.tag}"
        for n in range(3):
            frappe.get_doc({"doctype": "Error Log", "method": title, "error": f"trace {n}"}).insert(
                ignore_permissions=True
            )
        # the summary shows the ten biggest; a test site has many more errors than that
        with patch.object(hypercare, "SHOWN", 100000):
            row = next(r for r in self._rows(hypercare.ERRORS) if self.tag in r["item"])
        self.assertEqual(row["count"], 3)
        self.assertIn("Fiscal Year", row["fix"])

    def test_the_same_failure_from_several_users_is_a_training_point(self):
        users = [self.make_throwaway_user(f"clerk{n}") for n in range(2)]
        for n, user in enumerate(users):
            frappe.get_doc(
                {
                    "doctype": "Assistant Audit Log",
                    "action": "tool_call",
                    "tool_name": "create_document",
                    "user": user,
                    "status": "Error",
                    "timestamp": now_datetime(),
                    # The same failure on different records: the names must not split the group
                    "error_message": f"Price List 'Retail {self.tag} {n}' is disabled for {self.tag}",
                }
            ).insert(ignore_permissions=True)
        row = next(r for r in self._rows(hypercare.FAILURES) if self.tag in r["item"])
        self.assertEqual((row["count"], row["users"]), (2, 2))
        self.assertTrue(row["fix"].startswith("2 users hit this."), row["fix"])
        self.assertIn("price list", row["fix"])
        self.assertNotIn("Retail", row["item"])

    def test_a_draft_left_for_days_is_listed(self):
        if "erpnext" not in frappe.get_installed_apps():
            self.skipTest("needs ERPNext")
        company = frappe.get_all("Company", pluck="name", order_by="creation", limit=1)[0]
        parent = frappe.db.get_value(
            "Account", {"company": company, "root_type": "Asset", "is_group": 1}, order_by="lft"
        )
        accounts = [
            frappe.get_doc(
                {
                    "doctype": "Account",
                    "account_name": f"{side} {self.tag}",
                    "company": company,
                    "parent_account": parent,
                }
            )
            .insert()
            .name
            for side in ("Cash", "Bank")
        ]
        before = sum(
            r["count"]
            for r in self._rows(hypercare.DRAFTS, add_days(self.today, -10))
            if r["item"] == "Journal Entry"
        )
        je = frappe.get_doc(
            {
                "doctype": "Journal Entry",
                "company": company,
                "posting_date": nowdate(),
                "accounts": [
                    {"account": accounts[0], "debit_in_account_currency": 10},
                    {"account": accounts[1], "credit_in_account_currency": 10},
                ],
            }
        ).insert()
        self._age("Journal Entry", je.name, 5)
        after = sum(
            r["count"]
            for r in self._rows(hypercare.DRAFTS, add_days(self.today, -10))
            if r["item"] == "Journal Entry"
        )
        self.assertEqual(after, before + 1)

    def test_an_approval_waiting_longer_than_the_limit_is_listed(self):
        todo = frappe.get_doc({"doctype": "ToDo", "description": f"approve {self.tag}"}).insert()
        state = f"Pending {self.tag}"
        action = frappe.get_doc(
            {
                "doctype": "Workflow Action",
                "reference_doctype": "ToDo",
                "reference_name": todo.name,
                "workflow_state": state,
                "status": "Open",
            }
        ).insert(ignore_permissions=True)
        self.assertFalse([r for r in hypercare.approvals(3) if state in r["item"]])
        # A workflow made it when the document reached the state; nobody acted for 5 days
        self._age("Workflow Action", action.name, 5)
        row = next(r for r in hypercare.approvals(3) if state in r["item"])
        self.assertEqual(row["count"], 1)

    def test_a_user_who_never_logged_in_is_counted(self):
        user = self.make_throwaway_user("idle", roles=("System Manager",))

        def idle():
            rows = self._rows(hypercare.LOGINS)
            return rows[0]["count"] if rows else 0

        before = idle()
        frappe.db.set_value("User", user, "last_login", now_datetime())
        self.assertEqual(idle(), before - 1)

    def test_failed_jobs_are_grouped_by_job(self):
        job = frappe.get_all("Scheduled Job Type", pluck="name", limit=1)[0]
        before = next((r["count"] for r in self._rows(hypercare.JOBS) if r["item"] == job), 0)
        for _n in range(2):
            frappe.get_doc(
                {"doctype": "Scheduled Job Log", "scheduled_job_type": job, "status": "Failed"}
            ).insert(ignore_permissions=True)
        self.assertEqual(next(r["count"] for r in self._rows(hypercare.JOBS) if r["item"] == job), before + 2)

    # -- the report ---------------------------------------------------------------------------

    def test_generate_report_runs_it_with_a_headline(self):
        reply = (
            get_tool_registry()
            .get_tool("generate_report")
            ._safe_execute({"report_name": REPORT, "filters": {"from_date": str(add_days(self.today, -6))}})
        )
        self.assertTrue(reply["success"], reply)
        self.assertIn(str(self.today), str(reply["result"].get("message")))

    def test_with_document_the_message_is_a_table_for_the_pdf(self):
        frappe.get_doc({"doctype": "Error Log", "method": f"Broken {self.tag}", "error": "x"}).insert(
            ignore_permissions=True
        )
        with patch.object(hypercare, "SHOWN", 100000):
            message = hypercare.execute({"document": 1})[2]
        self.assertIn("| Area | What | How many | Users | Suggested fix |", message)
        self.assertIn(self.tag, message)

    def test_the_summary_is_only_for_system_managers(self):
        with self.assertRaises(frappe.PermissionError):
            hypercare.check_access(self.make_desk_user("outsider"))
        hypercare.check_access(self.consultant)

    def test_a_consultant_who_is_not_a_system_manager_is_refused(self):
        # the weekly email's links open a System Manager report: anyone else would be refused there
        self.consultant = self.make_desk_user("consultant")
        with self.assertRaises(frappe.ValidationError):
            self._hypercare(self.today)

    # -- the weekly job -----------------------------------------------------------------------

    def _send(self):
        with patch("frappe.sendmail", autospec=True) as sendmail:
            hypercare.send_due_summaries()
        return sendmail

    def test_a_week_after_the_start_the_consultant_gets_the_summary(self):
        record = self._hypercare(add_days(self.today, -7))
        sendmail = self._send()
        mine = [c for c in sendmail.call_args_list if c.kwargs.get("reference_name") == record.name]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0].kwargs["recipients"], [self.consultant])
        self.assertIn("/copilot/chat?prompt=", mine[0].kwargs["message"])
        self.assertTrue(
            frappe.db.exists(
                "Notification Log",
                {"for_user": self.consultant, "document_name": record.name, "type": "Alert"},
            )
        )
        self.assertEqual(
            getdate(frappe.db.get_value("FAC Hypercare", record.name, "last_summary_on")),
            add_days(self.today, -1),
        )

        # the next day: the week isn't over, nothing more is sent
        sendmail = self._send()
        self.assertFalse(
            [c for c in sendmail.call_args_list if c.kwargs.get("reference_name") == record.name]
        )

    def test_a_site_that_cannot_send_email_still_gets_the_bell_and_the_hypercare_ends(self):
        record = self._hypercare(add_days(self.today, -30), days=30, last_summary_on=add_days(self.today, -4))
        record.db_set("status", "Active")
        # what frappe.sendmail raises on a site with no outgoing email account (Frappe 16)
        refused = frappe.OutgoingEmailError("Please setup default outgoing Email Account")
        with patch("frappe.sendmail", autospec=True, side_effect=refused):
            hypercare.send_due_summaries()

        self.assertTrue(
            frappe.db.exists(
                "Notification Log",
                {"for_user": self.consultant, "document_name": record.name, "type": "Alert"},
            )
        )
        self.assertEqual(
            getdate(frappe.db.get_value("FAC Hypercare", record.name, "last_summary_on")),
            add_days(self.today, -1),
        )
        self.assertEqual(frappe.db.get_value("FAC Hypercare", record.name, "status"), "Ended")

    def test_less_than_a_week_in_nothing_is_sent(self):
        record = self._hypercare(add_days(self.today, -3))
        sendmail = self._send()
        self.assertFalse(
            [c for c in sendmail.call_args_list if c.kwargs.get("reference_name") == record.name]
        )
        self.assertEqual(frappe.db.get_value("FAC Hypercare", record.name, "status"), "Active")

    def test_when_the_period_is_over_the_last_days_are_sent_and_it_ends(self):
        record = self._hypercare(add_days(self.today, -30), days=30, last_summary_on=add_days(self.today, -4))
        # Saved while it ran; the job sees it Active the day after its last day
        record.db_set("status", "Active")
        sendmail = self._send()
        mine = [c for c in sendmail.call_args_list if c.kwargs.get("reference_name") == record.name]
        self.assertEqual(len(mine), 1)
        self.assertIn(str(add_days(self.today, -3)), mine[0].kwargs["subject"])
        self.assertEqual(frappe.db.get_value("FAC Hypercare", record.name, "status"), "Ended")
