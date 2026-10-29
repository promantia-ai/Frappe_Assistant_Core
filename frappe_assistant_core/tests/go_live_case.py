# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""What the go-live entry tests share (#34419): a company of their own, and a site with no onboarding
records, so the QA site's own setup plans and hypercare don't decide a stage."""

import json
import unittest

import frappe
from frappe.utils import now_datetime

from frappe_assistant_core.tests.base_test import BaseAssistantTest

# The records the stage and the steps are read from. Deleted inside the test transaction, which is
# rolled back, so the site keeps its own.
ONBOARDING_DOCTYPES = ("FAC Go-Live", "FAC Setup Plan", "FAC Hypercare", "FAC Onboarding Document")


class GoLiveCase(BaseAssistantTest):
    company = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if "erpnext" not in frappe.get_installed_apps():
            raise unittest.SkipTest("The go-live entry point needs ERPNext.")
        tag = frappe.generate_hash(length=5).upper()
        cls.company = f"_GoLive {tag}"
        cls.abbr = f"G{tag}"
        frappe.get_doc(
            {
                "doctype": "Company",
                "company_name": cls.company,
                "abbr": cls.abbr,
                "default_currency": "INR",
                "country": "India",
                "create_chart_of_accounts_based_on": "Standard Template",
                "chart_of_accounts": "Standard",
            }
        ).insert(ignore_permissions=True)

    def setUp(self):
        super().setUp()
        for doctype in ONBOARDING_DOCTYPES:
            if frappe.db.table_exists(doctype):
                frappe.db.delete(doctype)
        frappe.db.delete("FAC Import Session", {"plan_status": ("is", "set")})
        frappe.db.delete("Assistant Audit Log", {"tool_name": "go_live_test_run"})
        frappe.cache().delete_keys("fac_go_live_entry")

    # --- records, made the way the tools make them ---

    def plan(self, status="Draft", company=None):
        doc = frappe.get_doc(
            {"doctype": "FAC Setup Plan", "title": "Plan", "company": company or self.company}
        ).insert(ignore_permissions=True)
        if status != "Draft":
            # run_setup_plan moves the status on; the form never sets it
            doc.db_set("status", status)
        return doc

    def audit(self, output, truncated=0, input_data=None, status="Success"):
        # what audit_trail.log_tool_execution writes after a go_live_test_run call
        return frappe.get_doc(
            {
                "doctype": "Assistant Audit Log",
                "action": "go_live_test_run",
                "tool_name": "go_live_test_run",
                "user": "Administrator",
                "status": status,
                "timestamp": now_datetime(),
                "input_data": json.dumps(input_data or {}),
                "output_data": output if isinstance(output, str) else json.dumps(output),
                "output_truncated": truncated,
            }
        ).insert(ignore_permissions=True)

    def hypercare(self, status="Active", start=None, days=30):
        doc = frappe.get_doc(
            {
                "doctype": "FAC Hypercare",
                "consultant": "Administrator",
                "start_date": start or frappe.utils.nowdate(),
                "days": days,
            }
        ).insert(ignore_permissions=True)
        if status != "Active":
            # send_due_summaries ends it when the period is over
            doc.db_set("status", status)
        return doc

    def go_live(self, company=None, **fields):
        from frappe_assistant_core.assistant_core.doctype.fac_go_live.fac_go_live import get_or_create

        doc = get_or_create(company or self.company)
        for key, value in fields.items():
            doc.set(key, value)
        doc.save(ignore_permissions=True)
        return doc
