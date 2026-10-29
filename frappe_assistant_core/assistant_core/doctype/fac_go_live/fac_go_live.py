# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""One record per company being taken live (#34419): what the setup wizard said, "Already live",
skipped steps, "Not now", what the chart import and the migration plan finished, and who switched the
onboarding tools off. Read by the go-live entry check in onboarding/entry.py."""

import frappe
from frappe.model.document import Document


class FACGoLive(Document):
    pass


def get_or_create(company: str) -> Document:
    """The company's FAC Go-Live, made on first use (a site set up before #34419 has none)."""
    name = frappe.db.get_value("FAC Go-Live", {"company": company})
    if name:
        return frappe.get_doc("FAC Go-Live", name)
    return frappe.get_doc(
        {"doctype": "FAC Go-Live", "company": company, "intent": "Unknown", "intent_source": "Manual"}
    ).insert(ignore_permissions=True)
