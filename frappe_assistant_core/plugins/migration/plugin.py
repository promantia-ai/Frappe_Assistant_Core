# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Migration plugin for Frappe Assistant Core.
Brings a client's data into ERPNext and takes the client live: import sessions and the migration plan
(#33986), opening stock, invoices and balances, recipes, the setup plan with its users and roles, the
client's chart of accounts, and the go-live test run (#34419).
"""

from typing import Any, Dict, List, Optional, Tuple

from frappe_assistant_core.plugins.base_plugin import BasePlugin


class MigrationPlugin(BasePlugin):
    """Data migration and onboarding: from the client's old system to go-live."""

    def get_info(self) -> Dict[str, Any]:
        return {
            "name": "migration",
            "display_name": "Migration",
            "description": "Import a client's data from their old system and take them live: import "
            "sessions, migration plans, opening entries, recipes, the setup plan, the chart of "
            "accounts and the go-live test run",
            "version": "1.0.0",
            "dependencies": [],
            "requires_restart": False,
        }

    def get_tools(self) -> List[str]:
        return [
            # One file at a time (#33986)
            "start_import_session",
            "get_import_schema",
            "set_column_mapping",
            "dry_run_import",
            "start_import",
            # Opening entries
            "set_opening_stock",
            "import_opening_stock",
            "set_opening_invoices",
            "import_opening_invoices",
            "set_opening_balances",
            "import_opening_balances",
            # A whole migration on one approval
            "plan_migration",
            "run_migration_plan",
            "stop_migration_plan",
            # Onboarding a client (#34419): recipes, the setup plan (users and roles in it), the chart
            "migration_recipe",
            "setup_plan",
            "run_setup_plan",
            "import_chart_of_accounts",
            "replace_chart_of_accounts",
            # Go-live (#34424)
            "go_live_test_run",
        ]

    def validate_environment(self) -> Tuple[bool, Optional[str]]:
        return True, None
