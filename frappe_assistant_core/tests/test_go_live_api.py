# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""The go-live entry point's endpoints (#34419): the only writes it makes are the user's own clicks,
each checked again on the server; the cache follows the records; the FAC Chat banner steps aside."""

from unittest.mock import create_autospec, patch

import frappe

from frappe_assistant_core.tests.go_live_case import GoLiveCase


class TestGoLiveApi(GoLiveCase):
    def setUp(self):
        super().setUp()
        from frappe_assistant_core.utils.plugin_manager import PluginManager

        frappe.db.delete("GL Entry", {"company": self.company})
        frappe.db.set_single_value("Assistant Core Settings", "server_enabled", 1)
        # the setup wizard names the company, so the check is about this one
        self.go_live(intent_source="Setup wizard")
        self.user = self.make_throwaway_user("sysmgr", roles=("System Manager",))
        # never toggle a plugin for real in a test: the plugin manager commits (AGENTS.md)
        self.manager = create_autospec(PluginManager, instance=True)
        self.manager.get_enabled_plugins.return_value = {"core", "migration"}
        for target in (
            "frappe_assistant_core.onboarding.entry.get_plugin_manager",
            "frappe_assistant_core.utils.plugin_manager.get_plugin_manager",
        ):
            patcher = patch(target, autospec=True, return_value=self.manager)
            patcher.start()
            self.addCleanup(patcher.stop)
        frappe.set_user(self.user)

    def tearDown(self):
        frappe.set_user("Administrator")
        super().tearDown()

    def api(self):
        from frappe_assistant_core.onboarding import api

        return api

    def record(self):
        return frappe.get_doc("FAC Go-Live", {"company": self.company})

    def test_state_shows_for_system_manager(self):
        self.assertTrue(self.api().get_entry_state()["show"])

    def test_sales_user_gets_show_false(self):
        frappe.set_user(self.make_throwaway_user("sales", roles=("Sales User",)))
        self.assertEqual(self.api().get_entry_state(), {"show": False})
        self.assertEqual(self.api().get_entry_state(view="checklist"), {"show": False})

    def test_sales_user_post_refused(self):
        frappe.set_user(self.make_throwaway_user("sales", roles=("Sales User",)))
        api = self.api()
        calls = (
            lambda: api.snooze(self.company),
            lambda: api.mark_live(self.company),
            lambda: api.unmark_live(self.company),
            lambda: api.skip_step(self.company, "chart_of_accounts"),
            lambda: api.unskip_step(self.company, "chart_of_accounts"),
            lambda: api.enable_tools(),
        )
        for call in calls:
            with self.assertRaises(frappe.PermissionError):
                call()

    def test_snooze_7_days_creates_record(self):
        frappe.set_user("Administrator")
        frappe.db.delete("FAC Go-Live")
        frappe.set_user(self.user)
        self.api().snooze(self.company)
        rows = self.record().snoozes
        self.assertEqual([r.user for r in rows], [self.user])
        days = frappe.utils.time_diff_in_seconds(rows[0].until, frappe.utils.now_datetime()) / 86400
        self.assertAlmostEqual(days, 7, delta=0.01)
        # the record made here names no wizard, so ask about the company itself
        self.assertEqual(self.api().get_entry_state(company=self.company), {"show": False})

    def test_snooze_twice_keeps_one_row(self):
        self.api().snooze(self.company)
        self.api().snooze(self.company)
        self.assertEqual(len(self.record().snoozes), 1)

    def test_mark_live_hides_for_everyone_and_undo(self):
        other = self.make_throwaway_user("other", roles=("System Manager",))
        self.api().mark_live(self.company)
        doc = self.record()
        self.assertTrue(doc.marked_live)
        self.assertEqual(doc.marked_live_by, self.user)
        self.assertTrue(doc.marked_live_on)
        frappe.set_user(other)
        self.assertEqual(self.api().get_entry_state(), {"show": False})
        self.api().unmark_live(self.company)
        self.assertFalse(self.record().marked_live)
        self.assertTrue(self.api().get_entry_state()["show"])

    def test_skip_discovery_refused(self):
        with self.assertRaises(frappe.ValidationError):
            self.api().skip_step(self.company, "discovery")

    def test_skip_and_unskip(self):
        state = self.api().skip_step(self.company, "chart_of_accounts", reason="keep ERPNext's chart")
        steps = {s["key"]: s for s in state["steps"]}
        self.assertEqual(steps["chart_of_accounts"]["state"], "skipped")
        self.assertEqual(steps["chart_of_accounts"]["skipped"]["by"], self.user)
        # skipping twice keeps one row
        self.api().skip_step(self.company, "chart_of_accounts")
        self.assertEqual(len(self.record().skipped_steps), 1)
        state = self.api().unskip_step(self.company, "chart_of_accounts")
        steps = {s["key"]: s for s in state["steps"]}
        self.assertNotEqual(steps["chart_of_accounts"]["state"], "skipped")

    def test_enable_tools_needs_settings_write(self):
        real = frappe.has_permission

        def no_settings_write(doctype, ptype="read", *args, **kwargs):
            if doctype == "Assistant Core Settings" and ptype == "write":
                return False
            return real(doctype, ptype, *args, **kwargs)

        with patch("frappe.has_permission", side_effect=no_settings_write):
            with self.assertRaises(frappe.PermissionError):
                self.api().enable_tools()
        self.manager.enable_plugin.assert_not_called()

    def test_enable_tools_turns_plugin_on_and_clears_switch_off(self):
        frappe.db.set_value("FAC Go-Live", {"company": self.company}, "tools_disabled_by", "Administrator")
        self.api().enable_tools()
        self.manager.enable_plugin.assert_called_once_with("migration")
        self.assertFalse(self.record().tools_disabled_by)

    def test_admin_switch_off_recorded_and_cleared(self):
        from frappe_assistant_core.api.admin.plugins import toggle_plugin

        frappe.set_user("Administrator")
        self.assertTrue(toggle_plugin("migration", False)["success"])
        doc = self.record()
        self.assertEqual(doc.tools_disabled_by, "Administrator")
        self.assertTrue(doc.tools_disabled_on)
        self.assertTrue(toggle_plugin("migration", True)["success"])
        self.assertFalse(self.record().tools_disabled_by)

    def test_other_plugin_switch_off_not_recorded(self):
        from frappe_assistant_core.api.admin.plugins import toggle_plugin

        frappe.set_user("Administrator")
        toggle_plugin("visualization", False)
        self.assertFalse(self.record().tools_disabled_by)

    def test_cache_cleared_on_setup_plan_insert(self):
        api = self.api()
        self.assertEqual(api.get_entry_state()["stage"], "not_started")
        frappe.set_user("Administrator")
        self.plan("Draft")
        frappe.set_user(self.user)
        self.assertEqual(api.get_entry_state()["stage"], "in_progress")

    def test_cache_cleared_on_test_run_audit_entry(self):
        api = self.api()
        self.plan("Completed")
        self.assertEqual(api.get_entry_state()["stage"], "in_progress")
        self.audit({"ready": True, "used": {"company": self.company}})
        self.assertEqual(api.get_entry_state()["stage"], "ready_to_go_live")

    def test_chat_banner_hidden_while_go_live_shows(self):
        from frappe_assistant_core.chat.api.discovery import should_show_banner
        from frappe_assistant_core.chat.gate import clear_chat_gate_cache

        frappe.db.set_single_value("Assistant Core Settings", "enable_fac_chat", 0)
        clear_chat_gate_cache()
        self.assertFalse(should_show_banner())
        # an established site: the FAC Chat banner behaves as before
        frappe.db.set_value("FAC Go-Live", {"company": self.company}, "intent", "Own business")
        self.gl_entries(50)
        from frappe_assistant_core.onboarding import entry

        entry.clear_cache()
        self.assertTrue(should_show_banner())
