# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""The go-live entry point (#34419): the FAC Go-Live record, the six steps, the stage and the check
that decides whether a System Manager is offered to take the company live."""

from unittest.mock import create_autospec, patch

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


class TestStage(GoLiveCase):
    def setUp(self):
        super().setUp()
        # the class shares one company and one transaction: each test starts with no postings
        frappe.db.delete("GL Entry", {"company": self.company})

    def _stage(self):
        from frappe_assistant_core.onboarding import entry

        return entry.stage(self.company)

    def migration_plan(self):
        # run_migration_plan's plan is an import session with plan_status set
        doc = frappe.new_doc("FAC Import Session")
        doc.update(
            {
                "name": frappe.generate_hash(length=10),
                "file_name": "plan",
                "status": "File Read",
                "user": "Administrator",
                "plan_status": "Planning",
            }
        )
        doc.db_insert()
        return doc

    def test_r1_active_hypercare(self):
        self.plan("Completed")
        self.audit({"ready": True, "used": {"company": self.company}})
        self.hypercare("Active")
        self.assertEqual(self._stage(), "live_hypercare")

    def test_r2_ended_hypercare(self):
        self.hypercare("Ended")
        self.assertEqual(self._stage(), "done")

    def test_r3_ready_test_run_no_hypercare(self):
        self.plan("Completed")
        self.audit({"ready": True, "used": {"company": self.company}})
        self.assertEqual(self._stage(), "ready_to_go_live")

    def test_r4_setup_plan_exists(self):
        self.plan("Draft")
        self.assertEqual(self._stage(), "in_progress")

    def test_r4_migration_plan_exists(self):
        self.migration_plan()
        self.assertEqual(self._stage(), "in_progress")

    def test_r4_before_r6_qa_site_case(self):
        # the v16 QA site: 26 GL entries and two completed setup plans
        self.gl_entries(26)
        self.plan("Completed")
        self.plan("Completed")
        self.assertEqual(self._stage(), "in_progress")

    def test_r5_consultant_intent(self):
        self.go_live(intent="Consultant for a client", intent_source="Setup wizard")
        self.gl_entries(60, days_old=200)
        self.assertEqual(self._stage(), "not_started")

    def test_r6_new_site_no_intent(self):
        self.assertEqual(self._stage(), "not_started_soft")

    def test_r6_own_business_new_site(self):
        self.go_live(intent="Own business", intent_source="Setup wizard")
        self.gl_entries(10, days_old=5)
        self.assertEqual(self._stage(), "not_started_soft")

    def test_r7_many_gl_entries(self):
        self.gl_entries(50)
        self.assertEqual(self._stage(), "established")

    def test_r7_old_gl_entry(self):
        self.gl_entries(4)
        self.gl_entries(1, days_old=61)
        self.assertEqual(self._stage(), "established")

    def test_cancelled_gl_entries_not_counted(self):
        self.gl_entries(10)
        frappe.db.set_value("GL Entry", {"company": self.company}, "is_cancelled", 1)
        self.gl_entries(1, days_old=61)
        frappe.db.set_value(
            "GL Entry", {"company": self.company, "is_cancelled": 0}, "is_cancelled", 1
        )
        self.assertEqual(self._stage(), "not_started_soft")

    def test_pick_company_fallbacks(self):
        from frappe_assistant_core.onboarding import entry

        others = frappe.get_all("Company", filters={"name": ("!=", self.company)}, pluck="name", limit=1)
        if not others:
            self.skipTest("needs a second company")
        other = others[0]
        user = self.make_throwaway_user("pick", roles=("System Manager",))

        self.plan("Draft")
        self.assertEqual(entry.pick_company(user), self.company)

        frappe.db.delete("FAC Setup Plan")
        self.go_live(company=other, intent_source="Setup wizard")
        self.assertEqual(entry.pick_company(user), other)

        frappe.db.delete("FAC Go-Live")
        # what ERPNext stores when a user picks a default company
        frappe.defaults.set_user_default("company", self.company, user)
        self.assertEqual(entry.pick_company(user), self.company)

        frappe.defaults.clear_user_default("company", user)
        with patch("frappe.defaults.get_user_default", autospec=True, return_value=None):
            self.assertIsNone(entry.pick_company(user))

    def test_pick_company_single_company(self):
        from frappe_assistant_core.onboarding import entry

        with patch.object(entry, "_companies", autospec=True, return_value=["Only Co"]):
            self.assertEqual(entry.pick_company("Administrator"), "Only Co")


class TestEntryCheck(GoLiveCase):
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
        patcher = patch(
            "frappe_assistant_core.onboarding.entry.get_plugin_manager",
            autospec=True,
            return_value=self.manager,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def state(self, user=None, view="banner"):
        from frappe_assistant_core.onboarding import entry

        return entry.get_state(user or self.user, view)

    def assert_hidden_before_stage(self, user=None):
        from frappe_assistant_core.onboarding import entry

        with patch.object(entry, "stage", autospec=True) as stage:
            self.assertEqual(self.state(user), {"show": False})
            stage.assert_not_called()

    def test_new_site_shows_checklist(self):
        state = self.state()
        self.assertTrue(state["show"])
        self.assertEqual(state["variant"], "checklist")
        self.assertEqual(state["stage"], "not_started")
        self.assertTrue(state["soft"])
        self.assertEqual(state["company"], self.company)
        self.assertEqual(len(state["steps"]), 6)
        self.assertEqual(state["next_step"], "discovery")

    def test_s1_server_disabled_hides(self):
        frappe.db.set_single_value("Assistant Core Settings", "server_enabled", 0)
        self.assert_hidden_before_stage()

    def test_s2_no_erpnext_hides(self):
        from frappe_assistant_core.onboarding import entry

        # patched at FAC's own check: Frappe reads the installed apps too, and needs ERPNext's hooks
        with patch.object(entry, "_erpnext_installed", autospec=True, return_value=False):
            self.assert_hidden_before_stage()

    def test_s3_wizard_not_finished_hides(self):
        with patch("frappe.is_setup_complete", return_value=False):
            self.assert_hidden_before_stage()

    def test_s4_no_company_hides(self):
        from frappe_assistant_core.onboarding import entry

        with patch.object(entry, "_companies", autospec=True, return_value=[]):
            self.assert_hidden_before_stage()

    def test_s5_marked_live_hides(self):
        self.go_live(marked_live=1)
        self.assert_hidden_before_stage()

    def test_u1_guest_hides(self):
        self.assert_hidden_before_stage("Guest")

    def test_u1_website_user_hides(self):
        self.assert_hidden_before_stage(self.make_throwaway_user("web"))

    def test_u2_not_system_manager_hides(self):
        self.assert_hidden_before_stage(self.make_throwaway_user("sales", roles=("Sales User",)))

    def test_u3_no_read_hides(self):
        real = frappe.has_permission

        def no_hypercare(doctype, *args, **kwargs):
            return False if doctype == "FAC Hypercare" else real(doctype, *args, **kwargs)

        with patch("frappe.has_permission", side_effect=no_hypercare):
            self.assert_hidden_before_stage()

    def test_u4_snoozed_hides(self):
        self.go_live(snoozes=[{"user": self.user, "until": frappe.utils.add_days(None, 1)}])
        self.assertEqual(self.state(), {"show": False})
        # for this user only
        self.assertTrue(self.state(self.make_throwaway_user("other", roles=("System Manager",)))["show"])

    def test_u4_expired_snooze_shows(self):
        self.go_live(snoozes=[{"user": self.user, "until": frappe.utils.add_days(None, -1)}])
        self.assertTrue(self.state()["show"])

    def test_hidden_stages(self):
        from frappe_assistant_core.onboarding import entry

        for stage in ("done", "established"):
            with self.subTest(stage=stage), patch.object(entry, "stage", autospec=True, return_value=stage):
                entry.clear_cache()
                self.assertEqual(self.state(), {"show": False})

    def test_shown_stages_variant(self):
        from frappe_assistant_core.onboarding import entry

        expected = {
            "not_started": "checklist",
            "in_progress": "checklist",
            "ready_to_go_live": "checklist",
            "live_hypercare": "hypercare",
        }
        self.hypercare("Active")
        for stage, variant in expected.items():
            with self.subTest(stage=stage), patch.object(entry, "stage", autospec=True, return_value=stage):
                entry.clear_cache()
                state = self.state()
                self.assertTrue(state["show"])
                self.assertEqual(state["variant"], variant)
                self.assertEqual(state["stage"], stage)

    def test_s6_auto_enables_when_nobody_said_no(self):
        self.manager.get_enabled_plugins.return_value = {"core"}
        state = self.state()
        self.manager.enable_plugin.assert_called_once_with("migration")
        self.assertEqual(state["variant"], "checklist")
        self.assertTrue(
            frappe.db.exists(
                "Assistant Audit Log", {"action": "enable plugin", "source_app": "go-live entry check"}
            )
        )

    def test_s6_respects_admin_switch_off(self):
        self.manager.get_enabled_plugins.return_value = {"core"}
        self.go_live(tools_disabled_by="Administrator", tools_disabled_on=frappe.utils.now_datetime())
        state = self.state()
        self.manager.enable_plugin.assert_not_called()
        self.assertEqual(state["variant"], "enable_tools")
        self.assertTrue(state["can_enable_tools"])
        self.assertEqual(state["primary"], "Turn on onboarding tools")
        self.assertEqual(
            state["text"], f"FAC can help take {self.company} live, but its onboarding tools are off."
        )

    def test_u5_without_settings_write(self):
        self.manager.get_enabled_plugins.return_value = {"core"}
        self.go_live(tools_disabled_by="Administrator")
        real = frappe.has_permission

        def no_settings_write(doctype, ptype="read", *args, **kwargs):
            if doctype == "Assistant Core Settings" and ptype == "write":
                return False
            return real(doctype, ptype, *args, **kwargs)

        with patch("frappe.has_permission", side_effect=no_settings_write):
            state = self.state()
        self.assertEqual(state["variant"], "enable_tools")
        self.assertFalse(state["can_enable_tools"])
        self.assertIsNone(state["primary"])
        self.assertEqual(state["buttons"], ["not_now"])
        self.assertEqual(
            state["text"],
            f"FAC can help take {self.company} live. Ask an administrator to turn on its onboarding tools.",
        )

    def test_established_with_plugin_off_sees_nothing(self):
        self.manager.get_enabled_plugins.return_value = {"core"}
        frappe.db.set_value("FAC Go-Live", {"company": self.company}, "intent", "Own business")
        self.gl_entries(50)
        self.assertEqual(self.state(), {"show": False})
        self.manager.enable_plugin.assert_not_called()

    def test_checklist_view_ignores_snooze_and_marked_live(self):
        self.go_live(
            marked_live=1,
            marked_live_by="Administrator",
            snoozes=[{"user": self.user, "until": frappe.utils.add_days(None, 1)}],
        )
        state = self.state(view="checklist")
        self.assertTrue(state["show"])
        self.assertEqual(state["marked_live"]["by"], "Administrator")
        self.assertIn(self.company, state["companies"])

    def test_checklist_view_shows_established(self):
        frappe.db.set_value("FAC Go-Live", {"company": self.company}, "intent", "Own business")
        self.gl_entries(50)
        state = self.state(view="checklist")
        self.assertTrue(state["show"])
        self.assertEqual(state["stage"], "established")

    def test_checklist_view_still_needs_system_manager(self):
        sales = self.make_throwaway_user("sales", roles=("Sales User",))
        self.assertEqual(self.state(sales, view="checklist"), {"show": False})

    def test_fail_closed(self):
        from frappe_assistant_core.onboarding import entry

        before = frappe.db.count("Error Log", {"method": "FAC go-live entry check"})
        with patch.object(entry, "steps", autospec=True, side_effect=RuntimeError("boom")):
            entry.clear_cache()
            self.assertEqual(self.state(), {"show": False})
        self.assertEqual(frappe.db.count("Error Log", {"method": "FAC go-live entry check"}), before + 1)

    def test_texts_match_spec(self):
        from frappe_assistant_core.onboarding import entry

        c = self.company
        cases = {
            "not_started": (f"{c} isn't live yet. Want FACO to help take it live?", "Start"),
            "not_started_soft": (f"Is {c} a new implementation? FACO can help take it live.", "Start"),
            "ready_to_go_live": (
                f"{c} passed its go-live test run. Switch on hypercare after go-live.",
                "Continue",
            ),
        }
        for stage, (text, primary) in cases.items():
            with self.subTest(stage=stage), patch.object(entry, "stage", autospec=True, return_value=stage):
                entry.clear_cache()
                state = self.state()
                self.assertEqual(state["text"], text)
                self.assertEqual(state["primary"], primary)
                self.assertEqual(state["buttons"], ["not_now", "already_live"])

        self.plan("Completed")
        entry.clear_cache()
        state = self.state()
        self.assertEqual(
            state["text"], f"{c} isn't live yet: 2 of 6 go-live steps done. Next: Chart of accounts."
        )
        self.assertEqual(state["primary"], "Continue")

    def test_hypercare_text_weeks(self):
        self.hypercare("Active", start=frappe.utils.add_days(None, -8), days=30)
        state = self.state()
        self.assertEqual(state["variant"], "hypercare")
        self.assertEqual(state["text"], f"Hypercare for {self.company}: week 2 of 5.")
        self.assertEqual(state["primary"], "Open hypercare summary")
        self.assertEqual(state["buttons"], ["not_now"])

    def test_continue_external_when_chat_off(self):
        from frappe_assistant_core.chat.gate import clear_chat_gate_cache

        frappe.db.set_single_value("Assistant Core Settings", "enable_fac_chat", 0)
        clear_chat_gate_cache()
        self.addCleanup(clear_chat_gate_cache)
        state = self.state()
        self.assertEqual(state["continue"]["mode"], "external")
        self.assertTrue(state["mcp_endpoint"])

    def test_continue_in_fac_chat_when_it_is_on_even_if_not_ready(self):
        # not connected / waitlisted: FAC Chat shows its own setup screen, the dialog isn't needed
        with patch(
            "frappe_assistant_core.chat.gate.is_chat_enabled", autospec=True, return_value=True
        ), patch(
            "frappe_assistant_core.chat.api.settings.access.can_use_faco",
            autospec=True,
            return_value={"can_use": False, "reason": "FACO needs to be connected to FAC Cloud"},
        ):
            go = self.state()["continue"]
        self.assertEqual(go["mode"], "fac_chat")
        self.assertIn("discover", go["prompt"])

    # Start and Continue hand the AI the go-live template, filled in for the company and the step to
    # do next: the user types nothing, and the AI starts where the checklist says (#34419)

    def install_templates(self):
        from frappe_assistant_core.utils.migration_hooks import _install_system_prompt_templates

        _install_system_prompt_templates()

    def test_start_prompt_begins_with_discovery(self):
        self.install_templates()
        prompt = self.state()["continue"]["prompt"]
        self.assertIn(f"Take {self.company} live", prompt)
        self.assertIn("setup_plan with action discover", prompt)
        self.assertNotIn("go_live_test_run", prompt)

    def test_continue_prompt_names_next_step(self):
        self.install_templates()
        self.plan("Draft")
        prompt = self.state()["continue"]["prompt"]
        self.assertIn(self.company, prompt)
        self.assertIn("run_setup_plan", prompt)
        self.assertNotIn("action discover", prompt)

    def test_every_step_has_an_instruction(self):
        from frappe_assistant_core.onboarding import entry

        for key in entry.STEP_KEYS:
            with self.subTest(step=key):
                self.assertTrue(entry.step_instruction(key, "Acme"))

    def test_continue_prompt_comes_from_the_template(self):
        # an administrator may reword the template: the button follows it
        from frappe_assistant_core.onboarding import entry

        self.install_templates()
        name = frappe.db.get_value("Prompt Template", {"prompt_id": "go-live"})
        frappe.db.set_value("Prompt Template", name, "template_content", "GO {{ company }} | {{ step }}")
        prompt = self.state()["continue"]["prompt"]
        self.assertEqual(prompt, f"GO {self.company} | " + entry.step_instruction("discovery", self.company))

    def test_continue_prompt_without_the_template(self):
        frappe.db.delete("Prompt Template", {"prompt_id": "go-live"})
        prompt = self.state()["continue"]["prompt"]
        self.assertIn(self.company, prompt)
        self.assertIn("setup_plan with action discover", prompt)

    def test_a_broken_template_falls_back(self):
        self.install_templates()
        name = frappe.db.get_value("Prompt Template", {"prompt_id": "go-live"})
        frappe.db.set_value("Prompt Template", name, "template_content", "{% if %}")
        state = self.state()
        self.assertTrue(state["show"])
        self.assertIn("setup_plan with action discover", state["continue"]["prompt"])

    # Each card says what its step is for and can be started on its own: steps don't have to be done
    # in order, so a consultant may import the chart before the setup plan is approved

    def test_checklist_steps_say_what_they_are_for(self):
        for step in self.state(view="checklist")["steps"]:
            with self.subTest(step=step["key"]):
                self.assertTrue(step["description"])

    def test_each_open_step_can_be_started_on_its_own(self):
        from frappe_assistant_core.onboarding import entry

        self.install_templates()
        steps = {s["key"]: s for s in self.state(view="checklist")["steps"]}
        # Discovery is next, but the chart of accounts can be started as well
        self.assertIn(entry.step_instruction("chart_of_accounts", self.company), steps["chart_of_accounts"]["prompt"])
        self.assertIn(self.company, steps["chart_of_accounts"]["prompt"])
        self.assertIn("setup_plan with action discover", steps["discovery"]["prompt"])

    def test_done_and_skipped_steps_have_no_prompt(self):
        self.plan("Draft")
        doc = frappe.get_doc("FAC Go-Live", {"company": self.company})
        doc.append("skipped_steps", {"step": "data_migration"})
        doc.save(ignore_permissions=True)
        steps = {s["key"]: s for s in self.state(view="checklist")["steps"]}
        self.assertIsNone(steps["discovery"]["prompt"])
        self.assertIsNone(steps["data_migration"]["prompt"])
        self.assertTrue(steps["setup_plan"]["prompt"])

    def test_banner_view_renders_no_step_prompts(self):
        # the banner shows one line on every Desk page: it renders no template per step
        for step in self.state()["steps"]:
            self.assertNotIn("prompt", step)

    def test_result_has_no_record_contents(self):
        self.plan("Completed")
        state = self.state()
        for step in state["steps"]:
            if step["record"]:
                self.assertEqual(set(step["record"]), {"doctype", "name"})

    # --- found in review ---

    def test_checklist_view_never_enables_tools_on_a_live_site(self):
        self.manager.get_enabled_plugins.return_value = {"core"}
        self.plan("Draft")
        self.go_live(marked_live=1, marked_live_by="Administrator")
        state = self.state(view="checklist")
        self.manager.enable_plugin.assert_not_called()
        self.assertEqual(state["variant"], "enable_tools")

    def test_auto_enable_logs_before_the_plugin_commits(self):
        # enable_plugin commits, and a GET request rolls back what comes after it
        self.manager.get_enabled_plugins.return_value = {"core"}
        logged = []

        def enable(name):
            logged.append(frappe.db.exists("Assistant Audit Log", {"action": "enable plugin"}))

        self.manager.enable_plugin.side_effect = enable
        self.state()
        self.assertEqual(len(logged), 1)
        self.assertTrue(logged[0])

    def test_switch_off_on_another_company_is_respected(self):
        others = frappe.get_all("Company", filters={"name": ("!=", self.company)}, pluck="name", limit=1)
        if not others:
            self.skipTest("needs a second company")
        self.manager.get_enabled_plugins.return_value = {"core"}
        # FAC Admin recorded it on the company the check was about then
        self.go_live(company=others[0], intent_source="Manual", tools_disabled_by="Administrator")
        state = self.state()
        self.manager.enable_plugin.assert_not_called()
        self.assertEqual(state["variant"], "enable_tools")

    def test_checklist_view_on_an_established_site_can_start(self):
        frappe.db.set_value("FAC Go-Live", {"company": self.company}, "intent", "Own business")
        self.gl_entries(50)
        state = self.state(view="checklist")
        self.assertEqual(state["stage"], "established")
        self.assertIn("setup_plan with action discover", state["continue"]["prompt"])

    def test_setup_plan_end_clears_the_cache(self):
        from frappe_assistant_core.utils import setup_plan

        plan = self.plan("Running")
        self.assertNotEqual({s["key"]: s for s in self.state()["steps"]}["setup_plan"]["state"], "done")
        setup_plan._end(plan.name, "Completed", "done")
        self.assertEqual({s["key"]: s for s in self.state()["steps"]}["setup_plan"]["state"], "done")

    def test_hypercare_end_clears_the_cache(self):
        from frappe_assistant_core.utils import hypercare

        care = self.hypercare("Active", days=30)
        # forty days on: the period is over and its last summary was sent
        end = frappe.utils.add_days(None, -11)
        care.db_set(
            {"start_date": frappe.utils.add_days(None, -40), "end_date": end, "last_summary_on": end}
        )
        self.assertEqual(self.state()["variant"], "hypercare")
        with patch.object(hypercare, "send", autospec=True):
            hypercare.send_due_summaries()
        self.assertEqual(frappe.db.get_value("FAC Hypercare", care.name, "status"), "Ended")
        self.assertEqual(self.state(), {"show": False})
