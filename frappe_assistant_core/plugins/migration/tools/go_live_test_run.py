# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Go-Live Test Run Tool for Core Plugin (#34424).

Runs the client's buying and selling flows end to end on the client's own setup, as the user, and
rolls every document back: it reports which step fails and why, and keeps nothing.
"""

from typing import Any, Dict

import frappe
from frappe import _

from frappe_assistant_core.core.base_tool import BaseTool, exception_message
from frappe_assistant_core.utils import go_live_test_run as test_run


class GoLiveTestRun(BaseTool):
    """Run the buying and selling flows once, roll them back, and say which step failed."""

    def __init__(self):
        super().__init__()
        self.name = "go_live_test_run"
        self.description = (
            "Before go-live, run the client's main flows end to end on their own setup and report "
            "each step that fails, with ERPNext's reason. Buying: Material Request, Purchase Order, "
            "Purchase Receipt, Purchase Invoice, Payment Entry; then selling: Quotation, Sales "
            "Order, Delivery Note, Sales Invoice, Payment Entry. Each document is made from the one "
            "before and submitted as the user, so their accounts, taxes, numbering, permissions and "
            "workflows are what is tested. NOTHING IS KEPT: the whole run is rolled back, so there "
            "is nothing to delete afterwards; never offer to clean up. Without item, customer, "
            "supplier or warehouse it takes the site's defaults or its first ones: say which it "
            "used. A step after a failed one is 'not run'. Use it with the Go-Live Readiness report "
            "(get_skill('site-health-check'))."
        )
        self.category = "Builder checks"
        self.requires_permission = None  # each document checks the user's own permission
        # ERPNext's messages quote the client's record names
        self.untrusted_fields = ("steps[].message", "notes", "error")

        self.inputSchema = {
            "type": "object",
            "properties": {
                "company": {
                    "type": "string",
                    "description": "The company to test. Default: the user's default company.",
                },
                "item": {
                    "type": "string",
                    "description": "Item code to buy and sell. Default: the first stock item that "
                    "is both bought and sold, without batches or serial numbers.",
                },
                "customer": {"type": "string", "description": "Customer to sell to."},
                "supplier": {"type": "string", "description": "Supplier to buy from."},
                "warehouse": {"type": "string", "description": "Warehouse to receive into and deliver from."},
                "qty": {"type": "number", "default": 1, "description": "Quantity, default 1."},
                "rate": {
                    "type": "number",
                    "description": "Rate to use. Default: the price list's; 100 when it has none.",
                },
                "flows": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(test_run.FLOWS)},
                    "description": "Which flows to run: buy, sell, or both (the default).",
                },
            },
        }

    def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if "erpnext" not in frappe.get_installed_apps():
            return {"success": False, "error": _("The test run needs ERPNext.")}
        try:
            used = test_run.setup(arguments or {})
        except test_run.TestRunProblem as e:
            return {"success": False, "error": str(e)}
        try:
            return {"success": True, **test_run.run(used)}
        except Exception as e:
            frappe.log_error(title=_("Go-Live Test Run Error"), message=frappe.get_traceback())
            return {"success": False, "error": exception_message(e)}
