# Copyright (C) 2025 Promantia
# Developer Tools Plugin — AppService

import os
import shutil
import sys

import frappe
import frappe.installer
from frappe import _

from frappe_assistant_core.plugins.developer_tools.guards import (
    assert_app_dir_exists,
    assert_not_protected_app,
    assert_required,
    assert_valid_app_name,
    build_failure,
    get_app_path,
    get_apps_path,
)
from frappe_assistant_core.plugins.developer_tools.services.app_registry import AppRegistry


class AppService:
    """
    Business logic for bench_execute's app-lifecycle actions.

    Each @staticmethod implements one action and takes the raw arguments
    dict passed to BenchExecute.execute(). bench_execute.py's execute()
    calls straight into these and returns their result directly — no
    try/except here or at the call site, so ValidationError/PermissionError
    propagate uncaught to base_tool.py's error handling.
    """

    @staticmethod
    def list_apps(arguments):
        apps_path = get_apps_path()

        try:
            all_entries = os.listdir(apps_path)
        except OSError as e:
            frappe.throw(
                _("Cannot list apps directory '{0}': {1}").format(apps_path, str(e)),
                frappe.ValidationError,
            )
        bench_apps = [
            entry
            for entry in all_entries
            if not entry.startswith(".") and os.path.isdir(os.path.join(apps_path, entry))
        ]

        all_installed = frappe.get_installed_apps()
        site_apps = list(all_installed)

        # Only auto-clean ghost entries for non-protected apps; never silently
        # remove protected app entries even if their directory appears missing.
        ghosts = AppRegistry().remove_ghost_apps(apps_path)
        if ghosts:
            site_apps = [app for app in site_apps if app not in ghosts]

        return {
            "success": True,
            "bench_apps": bench_apps,
            "bench_apps_count": len(bench_apps),
            "site_apps": site_apps,
            "site_apps_count": len(site_apps),
            "message": "bench_apps are all apps on disk. site_apps are apps installed on current site.",
        }

    @staticmethod
    def install_app(arguments):
        import subprocess

        app_name = arguments.get("app_name")
        assert_required(app_name, _("app_name is required for install_app."))

        site_name = frappe.local.site
        bench_path = frappe.utils.get_bench_path()
        frappe.installer.install_app(app_name)

        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-e", f"apps/{app_name}", "--no-deps"],
            capture_output=True,
            cwd=bench_path,
        )

        verify_result = subprocess.run(
            [sys.executable, "-c", f"import {app_name}; print('ok')"],
            capture_output=True,
            text=True,
            cwd=bench_path,
        )
        if verify_result.returncode != 0:
            return build_failure(
                "App installed but import failed. hooks.py may be missing.",
                app_name=app_name,
                site_name=site_name,
            )

        return {
            "success": True,
            "app_name": app_name,
            "site_name": site_name,
            "message": f"App '{app_name}' installed on site '{site_name}' successfully.",
        }

    @staticmethod
    def uninstall_app(arguments):
        app_name = arguments.get("app_name")
        assert_required(app_name, _("app_name is required for uninstall_app."))

        installed = frappe.get_installed_apps()
        if app_name not in installed:
            return {
                "success": True,
                "already_uninstalled": True,
                "app_name": app_name,
                "message": f"App '{app_name}' is already not installed on the current site.",
            }

        frappe.installer.remove_app(app_name, yes=True, no_backup=True)

        AppRegistry().remove_from_installed_apps(app_name)

        return {
            "success": True,
            "app_name": app_name,
            "message": f"App '{app_name}' uninstalled from current site successfully.",
        }

    @staticmethod
    def create_app(arguments):
        app_name = arguments.get("app_name")
        assert_required(app_name, _("app_name is required for create_app."))

        assert_valid_app_name(app_name)

        assert_not_protected_app(
            app_name,
            _("Cannot create or overwrite protected app '{0}'.").format(app_name),
        )

        apps_path = get_apps_path()
        app_path = get_app_path(app_name)

        if os.path.isdir(app_path):
            return {
                "success": True,
                "already_existed": True,
                "installed": False,
                "apps_txt_updated": False,
                "app_name": app_name,
                "message": f"App '{app_name}' already exists at {app_path}.",
            }

        app_title = arguments.get("app_title") or app_name.replace("_", " ").title()
        app_description = arguments.get("app_description") or ""

        hooks = frappe._dict(
            app_name=app_name,
            app_title=app_title,
            app_description=app_description,
            app_publisher="Promantia",
            app_email="dev@promantia.com",
            app_license="mit",
            create_github_workflow=False,
        )

        from frappe.utils.boilerplate import _create_app_boilerplate

        _create_app_boilerplate(apps_path, hooks, no_git=True)

        outer_init = os.path.join(apps_path, app_name, "__init__.py")
        if not os.path.exists(outer_init):
            open(  # nosemgrep: frappe-security-file-traversal — path validated by resolve_and_validate_path()
                outer_init, "w"
            ).close()

        pyproject = os.path.join(apps_path, app_name, "pyproject.toml")
        if not os.path.exists(pyproject):
            with open(  # nosemgrep: frappe-security-file-traversal — path validated by resolve_and_validate_path()
                pyproject, "w"
            ) as f:
                f.write(f"""[project]
name = "{app_name}"
version = "0.0.1"

[build-system]
requires = ["flit_core >=3.2,<4"]
build-backend = "flit_core.buildapi"
""")

        from pip._internal.cli.main import main as pip_main

        pip_main(["install", "--quiet", "-e", os.path.join(apps_path, app_name)])

        if app_path not in sys.path:
            sys.path.insert(0, app_path)

        apps_txt_updated = AppRegistry().add_to_apps_txt(app_name)

        return {
            "success": True,
            "already_existed": False,
            "installed": False,
            "apps_txt_updated": apps_txt_updated,
            "app_name": app_name,
            "app_title": app_title,
            "message": f"App '{app_name}' created successfully. Use install_app to install it on the site.",
        }

    @staticmethod
    def remove_app(arguments):
        app_name = arguments.get("app_name")
        assert_required(app_name, _("app_name is required for remove_app."))

        assert_not_protected_app(
            app_name,
            _("Cannot remove protected app '{0}'.").format(app_name),
        )

        app_path = get_app_path(app_name)

        assert_app_dir_exists(
            app_path,
            _("App '{0}' directory does not exist at {1}.").format(app_name, app_path),
        )

        if not arguments.get("confirm"):
            return build_failure(
                f"This will permanently delete '{app_name}' and all its files. "
                f"Call remove_app again with confirm=true to proceed.",
                requires_confirmation=True,
            )

        # Uninstall from site first if installed
        installed_apps = frappe.get_installed_apps()
        if app_name in installed_apps:
            frappe.installer.remove_app(app_name, yes=True, no_backup=True)
        else:
            # Still clean from DB just in case
            new_list = []
            for app in installed_apps:
                if app != app_name:
                    new_list.append(app)
            frappe.db.set_global("installed_apps", frappe.as_json(new_list))
            frappe.db.commit()  # nosemgrep: frappe-manual-commit — required to persist app state before next DB read

        shutil.rmtree(app_path)

        registry_changes = AppRegistry().deregister_app(app_name)

        # Pip uninstall the package
        from pip._internal.cli.main import main as pip_main

        try:
            pip_main(["uninstall", "-y", app_name])
        except Exception:
            pass  # ignore if not pip installed

        return {
            "success": True,
            "app_name": app_name,
            "apps_txt_updated": registry_changes["apps_txt_updated"],
            "message": f"App '{app_name}' directory removed from bench successfully.",
        }
