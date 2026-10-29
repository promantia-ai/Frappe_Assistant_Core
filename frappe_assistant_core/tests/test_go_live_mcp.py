# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""The go-live offer over MCP (#34419): Claude, ChatGPT and other clients are told in the initialize
result that the company isn't live yet, and get a go-live prompt, from the same rules as the banner."""

from unittest.mock import create_autospec, patch

import frappe

from frappe_assistant_core.tests.go_live_case import GoLiveCase


class TestGoLiveMcp(GoLiveCase):
    def setUp(self):
        super().setUp()
        from frappe_assistant_core.utils.plugin_manager import PluginManager

        frappe.db.delete("GL Entry", {"company": self.company})
        frappe.db.set_single_value("Assistant Core Settings", "server_enabled", 1)
        # the setup wizard names the company, so the check is about this one
        self.go_live(intent="Consultant for a client", intent_source="Setup wizard")
        # never toggle a plugin for real in a test: the plugin manager commits (AGENTS.md)
        manager = create_autospec(PluginManager, instance=True)
        manager.get_enabled_plugins.return_value = {"core", "migration"}
        patcher = patch(
            "frappe_assistant_core.onboarding.entry.get_plugin_manager", autospec=True, return_value=manager
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        frappe.set_user("Administrator")
        super().tearDown()

    def initialize(self):
        from frappe_assistant_core.mcp.server import MCPServer

        return MCPServer("test")._handle_initialize({})

    def test_initialize_has_instructions_in_onboarding(self):
        frappe.set_user(self.make_throwaway_user("sysmgr", roles=("System Manager",)))
        self.plan("Draft")
        text = self.initialize()["instructions"]
        self.assertIn(self.company, text)
        self.assertIn("Setup plan", text)
        self.assertIn("go-live", text)

    def test_initialize_silent_when_established(self):
        frappe.set_user(self.make_throwaway_user("sysmgr", roles=("System Manager",)))
        frappe.db.set_value("FAC Go-Live", {"company": self.company}, "intent", "Own business")
        self.gl_entries(50)
        self.assertNotIn("instructions", self.initialize())

    def test_initialize_silent_in_hypercare(self):
        frappe.set_user(self.make_throwaway_user("sysmgr", roles=("System Manager",)))
        self.hypercare("Active")
        self.assertNotIn("instructions", self.initialize())

    def test_initialize_silent_for_sales_user(self):
        frappe.set_user(self.make_throwaway_user("sales", roles=("Sales User",)))
        self.assertNotIn("instructions", self.initialize())

    def test_initialize_survives_a_broken_check(self):
        from frappe_assistant_core.onboarding import entry

        with patch.object(entry, "mcp_instructions", autospec=True, side_effect=RuntimeError("boom")):
            result = self.initialize()
        self.assertIn("protocolVersion", result)
        self.assertNotIn("instructions", result)

    def test_go_live_prompt_installed(self):
        from frappe_assistant_core.utils.migration_hooks import _install_system_prompt_templates

        _install_system_prompt_templates()
        name = frappe.db.get_value("Prompt Template", {"prompt_id": "go-live", "is_system": 1})
        self.assertTrue(name)
        doc = frappe.get_doc("Prompt Template", name)
        self.assertEqual(doc.status, "Published")
        self.assertIn("setup_plan", doc.template_content)
        self.assertEqual([a.argument_name for a in doc.arguments], ["company", "step"])
        # without a step it tells the client how to find where the company stopped
        self.assertIn("setup_plan with action discover", frappe.render_template(doc.template_content, {}))
