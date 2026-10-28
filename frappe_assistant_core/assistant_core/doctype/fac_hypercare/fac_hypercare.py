# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
FAC Hypercare (#34426): hypercare switched on for a consultant, from a start date for a number of days.
The daily job in utils/hypercare.py sends each week's summary and ends the period on its own.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_days, cint, getdate, nowdate


class FACHypercare(Document):
    def validate(self):
        if cint(self.days) < 1:
            frappe.throw(_("Hypercare lasts at least one day."))
        if not frappe.db.get_value(
            "User", {"name": self.consultant, "enabled": 1, "user_type": "System User"}
        ):
            frappe.throw(_("The consultant must be an enabled Desk user."))
        if "System Manager" not in frappe.get_roles(self.consultant):
            # the summary is a System Manager report: its email's links would refuse anyone else
            frappe.throw(_("The consultant must be a System Manager, to open the weekly summary."))
        self.end_date = add_days(self.start_date, cint(self.days) - 1)
        # Lengthening an ended period starts it again
        self.status = "Ended" if getdate(self.end_date) < getdate(nowdate()) else "Active"
