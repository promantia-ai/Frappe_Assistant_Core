# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""The setup wizard's answers (#34419): v16 asks who the site is set up for and what the client uses
today, and ERPNext only sends that to its telemetry. FAC keeps it on FAC Go-Live, so the go-live banner
knows a consultant is setting up a client. The hook must never fail the wizard."""

from unittest.mock import create_autospec, patch

import frappe

from frappe_assistant_core.tests.go_live_case import GoLiveCase

HOOK = "frappe_assistant_core.onboarding.wizard.after_wizard"


class TestGoLiveWizard(GoLiveCase):
    def setUp(self):
        super().setUp()
        from frappe_assistant_core.utils.plugin_manager import PluginManager

        # never toggle a plugin for real in a test: the plugin manager commits (AGENTS.md)
        self.manager = create_autospec(PluginManager, instance=True)
        self.manager.get_enabled_plugins.return_value = {"core"}
        patcher = patch(
            "frappe_assistant_core.onboarding.entry.get_plugin_manager",
            autospec=True,
            return_value=self.manager,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # commits are blocked in tests, so callbacks queued for after the commit pile up: start clean
        frappe.db.after_commit.reset()
        self.addCleanup(frappe.db.after_commit.reset)

    def run_hook(self, **answers):
        from frappe_assistant_core.onboarding.wizard import after_wizard

        # the args Frappe passes: the wizard's whole form, company_name among them
        return after_wizard({"company_name": self.company, "country": "India", **answers})

    def record(self):
        return frappe.get_doc("FAC Go-Live", {"company": self.company})

    def test_v16_consultant_answers(self):
        self.run_hook(persona_implementing_for="A client I'm consulting for", persona_current_system="Tally")
        doc = self.record()
        self.assertEqual(doc.intent, "Consultant for a client")
        self.assertEqual(doc.current_system, "Tally")
        self.assertEqual(doc.intent_source, "Setup wizard")

    def test_mapping(self):
        cases = {
            "A client I'm consulting for": "Consultant for a client",
            "My own business": "Own business",
            "A company I work for": "Company I work for",
            "": "Unknown",
            None: "Unknown",  # v15's wizard has no such question
        }
        for answer, intent in cases.items():
            with self.subTest(answer=answer):
                frappe.db.delete("FAC Go-Live")
                answers = {} if answer is None else {"persona_implementing_for": answer}
                self.run_hook(**answers)
                self.assertEqual(self.record().intent, intent)
                self.assertEqual(self.record().intent_source, "Setup wizard")

    def test_consultant_turns_tools_on(self):
        self.run_hook(persona_implementing_for="A client I'm consulting for")
        # not inside the wizard: enable_plugin commits, and the wizard must be able to roll back
        self.manager.enable_plugin.assert_not_called()
        frappe.db.after_commit.run()
        self.manager.enable_plugin.assert_called_once_with("migration")

    def test_consultant_tools_already_on(self):
        self.manager.get_enabled_plugins.return_value = {"core", "migration"}
        self.run_hook(persona_implementing_for="A client I'm consulting for")
        self.manager.enable_plugin.assert_not_called()

    def test_own_business_leaves_tools(self):
        self.run_hook(persona_implementing_for="My own business")
        self.manager.enable_plugin.assert_not_called()

    def test_company_from_the_site_when_args_have_none(self):
        from frappe_assistant_core.onboarding import entry
        from frappe_assistant_core.onboarding.wizard import after_wizard

        with patch.object(entry, "_companies", autospec=True, return_value=[self.company]):
            after_wizard({"persona_implementing_for": "My own business"})
        self.assertEqual(self.record().intent, "Own business")

    def test_wizard_hook_never_raises(self):
        from frappe_assistant_core.onboarding import entry, wizard

        before = frappe.db.count("Error Log", {"method": "FAC go-live wizard hook"})
        # no company in the args and none to fall back on: nothing to write, no error
        with patch.object(entry, "_companies", autospec=True, return_value=[]):
            self.assertIsNone(wizard.after_wizard({}))
        # anything failing inside is logged, and the wizard goes on
        with patch.object(wizard, "get_or_create", autospec=True, side_effect=RuntimeError("boom")):
            self.assertIsNone(self.run_hook(persona_implementing_for="My own business"))
        self.assertIsNone(wizard.after_wizard(None))
        self.assertEqual(frappe.db.count("Error Log", {"method": "FAC go-live wizard hook"}), before + 1)

    def test_hook_registered(self):
        self.assertIn(HOOK, frappe.get_hooks("setup_wizard_complete"))
