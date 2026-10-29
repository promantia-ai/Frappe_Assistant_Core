# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""A new site starts with core and the migration plugin on (#34602).

The plugin manager reads the plugin list in Assistant Core Settings. On a new site it is empty, and
the upgrade patch that switches the migration plugin on never runs there, so the import and
onboarding tools did not exist at all.
"""

import inspect
import json

import frappe

from frappe_assistant_core.tests.base_test import BaseAssistantTest
from frappe_assistant_core.utils import migration_hooks
from frappe_assistant_core.utils.plugin_manager import PluginPersistence

SETTINGS = "Assistant Core Settings"


class TestDefaultPlugins(BaseAssistantTest):
    def _plugins(self, value):
        frappe.db.set_single_value(SETTINGS, "enabled_plugins_list", value)

    def test_a_new_site_has_the_migration_tools(self):
        # A new site: install leaves the list empty (base_test fills it, so it is cleared here)
        self._plugins(None)

        migration_hooks._enable_default_plugins()

        self.assertEqual({"core", "migration"}, PluginPersistence().load_enabled_plugins())

    def test_install_switches_them_on(self):
        self.assertIn("_enable_default_plugins()", inspect.getsource(migration_hooks.after_install))

    def test_a_list_that_names_plugins_is_kept(self):
        self._plugins(json.dumps(["core", "visualization"]))

        migration_hooks._enable_default_plugins()

        self.assertEqual({"core", "visualization"}, PluginPersistence().load_enabled_plugins())
