# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""The go-live entry point (#34419): the FAC Go-Live record, the six steps, the stage and the check
that decides whether a System Manager is offered to take the company live."""

import frappe

from frappe_assistant_core.tests.go_live_case import GoLiveCase


class TestGoLiveDocType(GoLiveCase):
    def test_one_record_per_company(self):
        frappe.get_doc({"doctype": "FAC Go-Live", "company": self.company}).insert(ignore_permissions=True)
        with self.assertRaises(frappe.DuplicateEntryError):
            frappe.get_doc({"doctype": "FAC Go-Live", "company": self.company}).insert(
                ignore_permissions=True
            )

    def test_get_or_create_is_idempotent(self):
        from frappe_assistant_core.assistant_core.doctype.fac_go_live.fac_go_live import get_or_create

        first = get_or_create(self.company)
        second = get_or_create(self.company)
        self.assertEqual(first.name, second.name)
        self.assertEqual(first.intent, "Unknown")
        self.assertEqual(first.intent_source, "Manual")
        self.assertEqual(frappe.db.count("FAC Go-Live", {"company": self.company}), 1)

    def test_only_system_manager_can_read(self):
        sales = self.make_throwaway_user("sales", roles=("Sales User",))
        admin = self.make_throwaway_user("sysmgr", roles=("System Manager",))
        self.assertFalse(frappe.has_permission("FAC Go-Live", "read", user=sales))
        self.assertTrue(frappe.has_permission("FAC Go-Live", "read", user=admin))
