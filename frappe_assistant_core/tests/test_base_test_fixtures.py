# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""The shared test fixtures hold on every site the suite runs on (#34430): Frappe v15 and v16,
with or without India Compliance. A fixture that only works on one of them fails here first."""

import unittest
from unittest.mock import patch

import frappe
from frappe.model.meta import Meta

from frappe_assistant_core.tests.base_test import NO_RIGHTS_ROLE, BaseAssistantTest, item_hsn_fields

# Doctypes the "someone else" users of the import tests must not touch.
GUARDED = ("Customer", "Supplier", "Customer Group", "Item", "Role", "Data Import")


class TestTheThrowawayUser(BaseAssistantTest):
    def test_the_throwaway_role_exists_on_this_site(self):
        self.make_desk_user("role")

        self.assertEqual(frappe.db.get_value("Role", NO_RIGHTS_ROLE, "desk_access"), 1)
        # it grants nothing: no permission rule anywhere names it
        self.assertFalse(frappe.get_all("DocPerm", filters={"role": NO_RIGHTS_ROLE}, limit=1))
        self.assertFalse(frappe.get_all("Custom DocPerm", filters={"role": NO_RIGHTS_ROLE}, limit=1))

    def test_a_throwaway_user_is_a_desk_user(self):
        user = self.make_desk_user("desk")

        self.assertEqual(frappe.db.get_value("User", user, "user_type"), "System User")
        own_roles = frappe.get_all("Has Role", filters={"parent": user, "parenttype": "User"}, pluck="role")
        self.assertEqual(own_roles, [NO_RIGHTS_ROLE])

    def test_a_throwaway_user_cannot_read_or_create_import_doctypes(self):
        user = self.make_desk_user("nothing")

        for doctype in (d for d in GUARDED if frappe.db.exists("DocType", d)):
            for ptype in ("read", "create", "write"):
                with self.subTest(doctype=doctype, ptype=ptype):
                    self.assert_cannot(user, ptype, doctype)
        # every desk user may keep Contacts of their own (the All rule), but none may import them
        self.assert_cannot(user, "import", "Contact")


@unittest.skipUnless("erpnext" in frappe.get_installed_apps(), "needs ERPNext's Item")
class TestTheItemFixture(BaseAssistantTest):
    def _item(self):
        return frappe.get_doc(
            {
                "doctype": "Item",
                "item_code": f"FIXTURE-{frappe.generate_hash(length=6)}",
                "item_group": frappe.db.get_value("Item Group", {"is_group": 0}),
                "stock_uom": "Nos",
                **item_hsn_fields(),
            }
        ).insert()

    def test_an_item_fixture_inserts_on_this_site(self):
        item = self._item()

        self.assertTrue(frappe.db.exists("Item", item.name))
        self.assertEqual(item.get("gst_hsn_code"), item_hsn_fields().get("gst_hsn_code"))

    def test_an_item_fixture_has_a_valid_hsn_when_india_compliance_is_installed(self):
        fields = item_hsn_fields()

        if frappe.get_meta("Item").has_field("gst_hsn_code"):
            from india_compliance.gst_india.utils import get_hsn_settings

            code = fields["gst_hsn_code"]
            self.assertTrue(frappe.db.exists("GST HSN Code", code))
            self.assertIn(len(code), get_hsn_settings()[1])
        else:
            self.assertEqual(fields, {})

    def test_an_item_fixture_sets_no_hsn_without_india_compliance(self):
        # Item has gst_hsn_code only where India Compliance added it; on such a site, report it
        # absent as a site without India Compliance would (fac.localhost has none to begin with).
        real = Meta.has_field

        def has_field(meta, fieldname):
            return fieldname != "gst_hsn_code" and real(meta, fieldname)

        with patch.object(Meta, "has_field", autospec=True, side_effect=has_field):
            self.assertEqual(item_hsn_fields(), {})
