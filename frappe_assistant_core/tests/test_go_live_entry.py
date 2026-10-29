# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""The go-live entry point (#34419): the FAC Go-Live record, the six steps, the stage and the check
that decides whether a System Manager is offered to take the company live."""

import frappe

from frappe_assistant_core.tests.go_live_case import GoLiveCase


class TestGoLiveDocType(GoLiveCase):
    def test_one_record_per_company(self):
        frappe.get_doc({"doctype": "FAC Go-Live", "company": self.company}).insert(ignore_permissions=True)
        with self.assertRaises(frappe.DuplicateEntryError):
            frappe.get_doc({"doctype": "FAC Go-Live", "company": self.company}).insert(
                ignore_permissions=True
            )

    def test_get_or_create_is_idempotent(self):
        from frappe_assistant_core.assistant_core.doctype.fac_go_live.fac_go_live import get_or_create

        first = get_or_create(self.company)
        second = get_or_create(self.company)
        self.assertEqual(first.name, second.name)
        self.assertEqual(first.intent, "Unknown")
        self.assertEqual(first.intent_source, "Manual")
        self.assertEqual(frappe.db.count("FAC Go-Live", {"company": self.company}), 1)

    def test_only_system_manager_can_read(self):
        sales = self.make_throwaway_user("sales", roles=("Sales User",))
        admin = self.make_throwaway_user("sysmgr", roles=("System Manager",))
        self.assertFalse(frappe.has_permission("FAC Go-Live", "read", user=sales))
        self.assertTrue(frappe.has_permission("FAC Go-Live", "read", user=admin))


class TestSteps(GoLiveCase):
    def _steps(self):
        from frappe_assistant_core.onboarding import entry

        return {s["key"]: s for s in entry.steps(self.company)}

    def _ready(self, ready=True, company=None):
        return self.audit({"ready": ready, "used": {"company": company or self.company}, "steps": []})

    def test_six_steps_in_order(self):
        from frappe_assistant_core.onboarding import entry

        self.assertEqual(
            [s["key"] for s in entry.steps(self.company)],
            ["discovery", "setup_plan", "chart_of_accounts", "data_migration", "go_live_test_run", "hypercare"],
        )

    def test_discovery_done_with_any_plan(self):
        self.plan("Completed")
        latest = self.plan("Draft")
        step = self._steps()["discovery"]
        self.assertEqual(step["state"], "done")
        self.assertEqual(step["record"], {"doctype": "FAC Setup Plan", "name": latest.name})

    def test_plan_of_another_company_does_not_count(self):
        other = frappe.get_all("Company", filters={"name": ("!=", self.company)}, pluck="name", limit=1)
        if not other:
            self.skipTest("needs a second company")
        self.plan("Completed", company=other[0])
        self.assertNotEqual(self._steps()["discovery"]["state"], "done")

    def test_discovery_not_skippable(self):
        doc = self.go_live()
        # the API refuses it; a row written by hand is ignored all the same
        doc.append("skipped_steps", {"step": "setup_plan"})
        doc.save(ignore_permissions=True)
        frappe.db.set_value("FAC Go-Live Skipped Step", doc.skipped_steps[0].name, "step", "discovery")
        self.assertEqual(self._steps()["discovery"]["state"], "next")

    def test_setup_plan_completed_done(self):
        plan = self.plan("Completed")
        step = self._steps()["setup_plan"]
        self.assertEqual(step["state"], "done")
        self.assertEqual(step["record"], {"doctype": "FAC Setup Plan", "name": plan.name})

    def test_setup_plan_needs_attention(self):
        for status in ("Partially Completed", "Failed", "Stopped"):
            with self.subTest(status=status):
                frappe.db.delete("FAC Setup Plan")
                self.plan(status)
                self.assertEqual(self._steps()["setup_plan"]["state"], "needs_attention")

    def test_setup_plan_record_includes_setup_record(self):
        self.plan("Completed")
        record = frappe.get_doc(
            {
                "doctype": "FAC Onboarding Document",
                "title": "Setup Record",
                "document_type": "Setup Record",
                "company": self.company,
                "content": "x",
            }
        ).insert(ignore_permissions=True)
        step = self._steps()["setup_plan"]
        self.assertEqual(step["setup_record"], {"doctype": "FAC Onboarding Document", "name": record.name})

    def test_chart_done_from_go_live_field(self):
        session = frappe.get_all("FAC Import Session", pluck="name", limit=1)
        self.go_live(chart_imported_on=frappe.utils.now_datetime(), chart_import_session=(session or [None])[0])
        step = self._steps()["chart_of_accounts"]
        self.assertEqual(step["state"], "done")
        if session:
            self.assertEqual(step["record"], {"doctype": "FAC Import Session", "name": session[0]})

    def test_migration_done_from_go_live_field(self):
        self.go_live(migration_completed_on=frappe.utils.now_datetime())
        self.assertEqual(self._steps()["data_migration"]["state"], "done")

    def test_step5_ready_true_done(self):
        entry = self._ready(True)
        step = self._steps()["go_live_test_run"]
        self.assertEqual(step["state"], "done")
        self.assertEqual(step["record"], {"doctype": "Assistant Audit Log", "name": entry.name})

    def test_step5_other_company_ignored(self):
        self._ready(True, company="Some Other Company")
        self.assertNotEqual(self._steps()["go_live_test_run"]["state"], "done")

    def test_step5_failed_call_ignored(self):
        self.audit({"ready": True, "used": {"company": self.company}}, status="Error")
        self.assertNotEqual(self._steps()["go_live_test_run"]["state"], "done")

    def test_step5_latest_wins(self):
        older = self._ready(True)
        frappe.db.set_value("Assistant Audit Log", older.name, "creation", frappe.utils.add_days(None, -1))
        self._ready(False)
        self.assertNotEqual(self._steps()["go_live_test_run"]["state"], "done")

    def test_step5_truncated_or_bad_json_not_done(self):
        older = self._ready(True)
        frappe.db.set_value("Assistant Audit Log", older.name, "creation", frappe.utils.add_days(None, -1))
        # a truncated entry for this company (named in the input) is the latest run: not done
        self.audit('{"ready": true, "used": {"comp', truncated=1, input_data={"company": self.company})
        self.assertNotEqual(self._steps()["go_live_test_run"]["state"], "done")
        frappe.db.delete("Assistant Audit Log", {"tool_name": "go_live_test_run"})
        self.audit("{bad")
        self.assertNotEqual(self._steps()["go_live_test_run"]["state"], "done")

    def test_hypercare_active_or_ended_done(self):
        care = self.hypercare("Active")
        step = self._steps()["hypercare"]
        self.assertEqual(step["state"], "done")
        self.assertEqual(step["record"], {"doctype": "FAC Hypercare", "name": care.name})
        care.db_set("status", "Ended")
        self.assertEqual(self._steps()["hypercare"]["state"], "done")

    def test_skipped_step(self):
        doc = self.go_live()
        doc.append(
            "skipped_steps",
            {"step": "chart_of_accounts", "skipped_by": "Administrator", "reason": "keep ERPNext's chart"},
        )
        doc.save(ignore_permissions=True)
        step = self._steps()["chart_of_accounts"]
        self.assertEqual(step["state"], "skipped")
        self.assertEqual(step["skipped"]["by"], "Administrator")
        self.assertEqual(step["skipped"]["reason"], "keep ERPNext's chart")

    def test_next_is_first_not_done_not_skipped(self):
        from frappe_assistant_core.onboarding import entry

        self.plan("Draft")
        doc = self.go_live()
        doc.append("skipped_steps", {"step": "setup_plan"})
        doc.save(ignore_permissions=True)
        steps = entry.steps(self.company)
        by_key = {s["key"]: s for s in steps}
        self.assertEqual(by_key["chart_of_accounts"]["state"], "next")
        self.assertEqual(by_key["data_migration"]["state"], "not_started")
        self.assertEqual(entry.next_step(steps), "chart_of_accounts")

    def test_next_keeps_needs_attention(self):
        from frappe_assistant_core.onboarding import entry

        self.plan("Failed")
        steps = entry.steps(self.company)
        self.assertEqual(steps[1]["state"], "needs_attention")
        self.assertEqual(entry.next_step(steps), "setup_plan")

    def test_all_done_no_next(self):
        from frappe_assistant_core.onboarding import entry

        self.plan("Completed")
        self.go_live(chart_imported_on=frappe.utils.now_datetime(), migration_completed_on=frappe.utils.now_datetime())
        self._ready(True)
        self.hypercare("Active")
        steps = entry.steps(self.company)
        self.assertEqual({s["state"] for s in steps}, {"done"})
        self.assertIsNone(entry.next_step(steps))
