# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Delete the stored settings of the onboarding tools merged into fewer (#34419).

The recipe tools are one migration_recipe; the setup interview's three are one setup_plan, and its
users and roles run with run_setup_plan instead of set_up_staff; the chart check is
import_chart_of_accounts mode check; where old IDs are kept is set_column_mapping's old_id_storage.
The removed tools' FAC Tool Configuration rows are deleted, so a site's tool list and its Desk
settings never show a tool that isn't there. Safe to run twice.
"""

from frappe_assistant_core.patches.v3_2.remove_import_undo_tool_configurations import (
    delete_configurations,
)

MERGED = (
    "choose_old_id_storage",
    "save_migration_recipe",
    "run_migration_recipe",
    "export_migration_recipe",
    "import_migration_recipe",
    "start_setup_discovery",
    "save_setup_plan",
    "update_setup_plan",
    "check_chart_of_accounts",
    "set_up_staff",
)


def execute():
    delete_configurations(MERGED)
