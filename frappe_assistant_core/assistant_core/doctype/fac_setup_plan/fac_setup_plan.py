# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""A setup plan (#34421): what the discovery interview decided, as the records and settings it becomes.

Drafted and changed in chat without writing to the site; approved once; applied by a background job
(utils/setup_plan.py). It outlives the chat: it is the setup record the client is handed."""

import frappe
from frappe import _
from frappe.model.document import Document

#: Only a Draft plan's items and answers change: after the approval they are what was approved.
FROZEN = ("Approved", "Running", "Completed", "Partially Completed", "Stopped", "Failed")


def _what_is_planned(doc):
    """The items as the approval covers them: not their state or message, which the run writes."""
    return [
        (
            i.area,
            i.decision,
            i.target_doctype,
            i.target_name,
            i.values,
            i.depends_on,
            i.is_customisation,
            i.created_record,
        )
        for i in doc.items
    ]


class FACSetupPlan(Document):
    def validate(self):
        before = self.get_doc_before_save()
        if before and before.status in FROZEN and _what_is_planned(before) != _what_is_planned(self):
            frappe.throw(_("Plan {0} is {1}, so its items can't change.").format(self.name, _(before.status)))
