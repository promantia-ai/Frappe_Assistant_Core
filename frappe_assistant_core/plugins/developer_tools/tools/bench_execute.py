# Copyright (C) 2025 Promantia
# Developer Tools Plugin — bench_execute tool

from typing import Any, Dict

import frappe
from frappe import _

from frappe_assistant_core.core.base_tool import BaseTool
from frappe_assistant_core.plugins.developer_tools.guards import assert_system_manager

from ..services.app_service import AppService
from ..services.fixture_service import FixtureService
from ..services.migration_service import MigrationService
from ..services.site_service import SiteService


class BenchExecute(BaseTool):
    """
    Executes bench operations: list apps, create/install/uninstall/remove apps,
    and export fixtures. Always call bench_help first to know available actions and parameters.
    """

    def __init__(self):
        super().__init__()
        self.name = "bench_execute"
        self.description = (
            "Executes bench operations. Always call bench_help first to know available actions "
            "and parameters. Do NOT ask user for bench paths or site names. "
            "After create_app always call install_app to install it on the current site. "
            "A newly created app must be installed before reports and doctypes are visible. "
            "CREATE_APP VERIFY RULE: After create_app succeeds immediately verify the app imports: "
            "bench --site {site} execute \"import {app_name}; print('ok')\". "
            "If import fails the scaffold is broken — automatically run remove_app then create_app again. "
            "Never proceed with a broken app. "
            "MIGRATE LOCK RULE: Before running migrate, check for a stale lock: "
            "run 'ps aux' to see if bench migrate is already running. "
            "If migrate IS running, alert the user and stop — do not run a second migrate. "
            "If migrate is NOT running but sites/{site}/locks/bench_migrate.lock exists, "
            "delete the lock file automatically then proceed with migrate. "
            "MIGRATE RULE: After ALL write_file calls are complete for a Script Report or DocType, "
            "you MUST call bench_execute with action='migrate' automatically. "
            "Never tell user to run bench migrate manually. "
            "Always run it yourself as the final step. "
            "After creating a Script Report with write_file, ask user: "
            "'Should I run bench migrate to make the report visible? (Yes/No)' "
            "INSTALL_APP RULE: Always call list_sites first. "
            "Then tell user: 'I found these sites: [list]. I will install [app] on [site]. Should I proceed?' "
            "Always wait for user confirmation before installing. "
            "Never skip confirmation even if only one site exists. "
            "INSTALL_APP VERIFY RULE: After installing an app always verify the Python import works: "
            "bench --site {site} execute \"import {app_name}; print('ok')\". "
            "If import fails immediately alert the user and do not proceed further. "
            "REMOVE_APP: Permanently deletes an app from the bench. Requires confirm=true — "
            "without it, remove_app returns a dry-run-style warning describing what would be "
            "deleted, without deleting anything. Never remove frappe, erpnext, hrms, payments, "
            "frappe_assistant_core, or any app that other apps depend on. "
            "Runs the site uninstall, DB deregistration, and pip-uninstall synchronously and "
            "confirms all of them before returning; the app's directory is then deleted in the "
            "background (result includes directory_cleanup='in_progress') so this call's own "
            "response never touches apps/ before it's sent — no follow-up status call exists or "
            "is needed. If remove_app is called again for the same app_name while that background "
            "cleanup is still running, it returns removal_in_progress=True instead of proceeding "
            "— wait a few seconds and retry. create_app on the same app_name during that same "
            "window will wait briefly then return the same removal_in_progress=True if cleanup "
            "hasn't finished. "
            "MIGRATE VERIFY RULE: After migrate completes verify by checking: "
            "frappe.db.get_all('DocType', {'module': '{app_module}'}). "
            "If new doctypes are missing alert the user. "
            "MULTI-SITE RULE: If list_sites returns more than one site, you MUST show the list to "
            "the user and ask: 'I found these sites: [list all sites]. Which site should I use?' "
            "NEVER pick a site automatically when multiple sites exist. "
            "NEVER proceed without the user choosing a site. "
            "MIGRATE STATUS RULE: After migrate returns background=True, immediately call "
            "migrate_status in the same response turn. "
            "If still_running=True, wait 15 seconds and call migrate_status again. "
            "Keep calling until still_running=False. "
            "Do NOT wait for the user to ask — poll automatically. "
            "Only tell the user migrate is complete when still_running=False. "
            "CONNECTION DROP RECOVERY RULE: create_app and install_app write files under apps/ "
            "synchronously before returning, and this bench's dev server auto-restarts on file "
            "changes — occasionally that restart happens mid-request and the tool call comes "
            "back as a raw/generic error (not a normal success/failure JSON result) instead of "
            "a clean response, even though the operation actually completed on the server. If "
            "one of these two actions returns a raw execution error, do NOT immediately tell the "
            "user it failed — first call list_apps and check whether the app appears in "
            "bench_apps/site_apps as expected. Only report a real failure to the user if "
            "list_apps confirms the operation did not actually happen. (remove_app and "
            "export_fixtures no longer need this — their own responses return before the "
            "watched-path file write happens; see REMOVE_APP above and EXPORT_FIXTURES below.) "
            "EXPORT_FIXTURES: Exports records as a fixture and registers them in the target "
            "app's hooks.py. Computes the records_exported/hooks_updated/hooks_update_warning "
            "result synchronously, then writes fixtures/<doctype>.json and (if changed) hooks.py "
            "in the background (result includes file_write='in_progress') so this call's own "
            "response never touches apps/ before it's sent. If export_fixtures is called again "
            "for the same app_name while that background write is still running, it returns "
            "export_in_progress=True instead of proceeding — wait a few seconds and retry."
        )
        self.source_app = "frappe_assistant_core"

        self.inputSchema = {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Operation to perform.",
                    "enum": [
                        "list_apps",
                        "list_sites",
                        "create_app",
                        "install_app",
                        "uninstall_app",
                        "remove_app",
                        "export_fixtures",
                        "migrate",
                        "migrate_status",
                    ],
                },
                "app_name": {
                    "type": "string",
                    "description": "Snake-case app name (required for all actions except list_apps).",
                },
                "confirm": {
                    "type": "boolean",
                    "description": "Must be explicitly set to true to actually delete the app. If false/missing, the action will only report what would be deleted, without deleting anything.",
                },
                "restart": {
                    "type": "boolean",
                    "description": "Run bench restart after migrate to reload workers (default true). Only used by migrate.",
                },
                "build_assets": {
                    "type": "boolean",
                    "description": "Run bench build --app {app_name} after migrate to rebuild JS/CSS assets (default false). Requires app_name. Only used by migrate.",
                },
                "doctype": {
                    "type": "string",
                    "description": "DocType to export as a fixture (only for export_fixtures).",
                },
                "filters": {
                    "type": "object",
                    "description": "Filters to specify which records to export (only for export_fixtures).",
                },
            },
            "required": ["action"],
        }

    def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        assert_system_manager()

        action = arguments.get("action")

        if action == "list_apps":
            return AppService.list_apps(arguments)

        elif action == "list_sites":
            return SiteService.list_sites(arguments)

        elif action == "create_app":
            return AppService.create_app(arguments)

        elif action == "install_app":
            return AppService.install_app(arguments)

        elif action == "migrate":
            return MigrationService.migrate(arguments)

        elif action == "uninstall_app":
            return AppService.uninstall_app(arguments)

        elif action == "remove_app":
            return AppService.remove_app(arguments)

        elif action == "export_fixtures":
            return FixtureService.export_fixtures(arguments)

        elif action == "migrate_status":
            return MigrationService.migrate_status(arguments)

        else:
            frappe.throw(
                _("Unknown action '{0}'. Call bench_help to see available operations.").format(action),
                frappe.ValidationError,
            )


bench_execute = BenchExecute
