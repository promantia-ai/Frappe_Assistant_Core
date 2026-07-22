# Copyright (C) 2025 Promantia
# Developer Tools Plugin — AppRegistry

import os

import frappe

from frappe_assistant_core.plugins.developer_tools.guards import PROTECTED_APPS


class AppRegistry:
    """
    Keeps the site's installed_apps DB record and the bench's sites/apps.txt
    file in sync with which apps actually exist on disk.
    """

    def __init__(self):
        self.bench_path = frappe.utils.get_bench_path()
        self.apps_txt_path = os.path.join(self.bench_path, "sites", "apps.txt")

    def remove_from_installed_apps(self, app_name):
        """
        Removes app_name from the site's installed_apps global, if present.
        Idempotent — safe to call even if app_name is not installed.
        Returns True if the DB record was changed, False otherwise.
        """
        installed = frappe.get_installed_apps()
        if app_name not in installed:
            return False

        new_list = [app for app in installed if app != app_name]
        frappe.db.set_global("installed_apps", frappe.as_json(new_list))
        frappe.db.commit()  # nosemgrep: frappe-manual-commit — required to persist app state before next DB read
        return True

    def remove_ghost_apps(self, apps_path):
        """
        Removes ghost entries from installed_apps: apps listed as installed
        but whose directory is missing from apps_path on disk. Never removes
        an entry that is in PROTECTED_APPS, even if its directory is missing.
        Returns the list of ghost app names that were removed.
        """
        installed = frappe.get_installed_apps()
        ghosts = [
            app
            for app in installed
            if app not in PROTECTED_APPS and not os.path.isdir(os.path.join(apps_path, app))
        ]
        if ghosts:
            clean_list = [app for app in installed if app not in ghosts]
            frappe.db.set_global("installed_apps", frappe.as_json(clean_list))
            frappe.db.commit()  # nosemgrep: frappe-manual-commit — required to persist app state before next DB read

        return ghosts

    def add_to_apps_txt(self, app_name):
        """
        Appends app_name to sites/apps.txt if not already present.
        Returns True if the file was changed, False otherwise.
        """
        if os.path.exists(self.apps_txt_path):
            with open(  # nosemgrep: frappe-security-file-traversal — path validated by resolve_and_validate_path()
                self.apps_txt_path
            ) as f:
                existing = [line.strip() for line in f if line.strip()]
        else:
            existing = []

        if app_name in existing:
            return False

        existing.append(app_name)
        with open(  # nosemgrep: frappe-security-file-traversal — path validated by resolve_and_validate_path()
            self.apps_txt_path, "w"
        ) as f:
            f.write("\n".join(existing) + "\n")
        return True

    def remove_from_apps_txt(self, app_name):
        """
        Removes app_name from sites/apps.txt if present.
        Returns True if the file was changed, False otherwise.
        """
        if not os.path.exists(self.apps_txt_path):
            return False

        with open(  # nosemgrep: frappe-security-file-traversal — path validated by resolve_and_validate_path()
            self.apps_txt_path
        ) as f:
            existing = [line.strip() for line in f if line.strip()]

        if app_name not in existing:
            return False

        existing.remove(app_name)
        with open(  # nosemgrep: frappe-security-file-traversal — path validated by resolve_and_validate_path()
            self.apps_txt_path, "w"
        ) as f:
            f.write("\n".join(existing) + "\n")
        return True

    def deregister_app(self, app_name):
        """
        Removes app_name from both the installed_apps DB global and
        sites/apps.txt. Returns a dict showing what was actually changed
        in each: {"installed_apps_updated": bool, "apps_txt_updated": bool}.
        """
        installed_apps_updated = self.remove_from_installed_apps(app_name)
        apps_txt_updated = self.remove_from_apps_txt(app_name)
        return {
            "installed_apps_updated": installed_apps_updated,
            "apps_txt_updated": apps_txt_updated,
        }
