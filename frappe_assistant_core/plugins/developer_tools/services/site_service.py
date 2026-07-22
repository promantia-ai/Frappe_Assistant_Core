# Copyright (C) 2025 Promantia
# Developer Tools Plugin — SiteService

import os

import frappe
from frappe import _


class SiteService:
    """
    Business logic for bench_execute's site-related actions.

    Each @staticmethod implements one action and takes the raw arguments
    dict passed to BenchExecute.execute(). bench_execute.py's execute()
    calls straight into these and returns their result directly — no
    try/except here or at the call site, so ValidationError/PermissionError
    propagate uncaught to base_tool.py's error handling.
    """

    @staticmethod
    def list_sites(arguments):
        bench_path = frappe.utils.get_bench_path()
        sites_path = os.path.join(bench_path, "sites")
        try:
            entries = os.listdir(sites_path)
        except OSError as e:
            frappe.throw(
                _("Cannot list sites directory '{0}': {1}").format(sites_path, str(e)),
                frappe.ValidationError,
            )
        available_sites = []
        for item in entries:
            site_config = os.path.join(sites_path, item, "site_config.json")
            if os.path.isfile(site_config):
                available_sites.append(item)
        return {
            "success": True,
            "sites": available_sites,
            "count": len(available_sites),
            "message": "These are the available sites on this bench.",
        }
