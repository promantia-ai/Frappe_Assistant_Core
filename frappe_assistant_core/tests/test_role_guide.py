# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Role guides (#34425): a guide is a FAC Skill of type Role Guide; saving it records the setup it was
written from, and opening it after the setup changed puts an out-of-date note on top. The setup is
changed the way a consultant changes it, with Customize Form's property setters.
"""

import frappe

from frappe_assistant_core.api.handlers.resources import SkillManager
from frappe_assistant_core.core.tool_registry import get_tool_registry
from frappe_assistant_core.tests.base_test import BaseAssistantTest
from frappe_assistant_core.utils import role_guide

DOCTYPE = "ToDo"
FIELD = "date"
STALE = "This guide is out of date."
SAVEPOINT = "fac_role_guide_test"


class TestRoleGuide(BaseAssistantTest):
    def setUp(self):
        super().setUp()
        frappe.set_user("Administrator")
        self.tag = frappe.generate_hash(length=6)
        self.skill_id = f"role-guide-test-{self.tag}"
        self.label = frappe.get_meta(DOCTYPE).get_label(FIELD)
        # The class rolls back only at its end: each test undoes its own setup changes
        frappe.db.savepoint(SAVEPOINT)

    def tearDown(self):
        frappe.db.rollback(save_point=SAVEPOINT)
        frappe.clear_cache(doctype=DOCTYPE)
        super().tearDown()

    def _property(self, property, value, field=FIELD, property_type="Check"):
        # What Customize Form saves when the consultant marks a field mandatory or sets a default
        frappe.make_property_setter(
            {
                "doctype": DOCTYPE,
                "doctype_or_field": "DocField" if field else "DocType",
                "fieldname": field,
                "property": property,
                "value": value,
                "property_type": property_type,
            },
            validate_fields_for_doctype=False,
        )
        frappe.clear_cache(doctype=DOCTYPE)

    def _guide(self, doctypes=DOCTYPE, content="1. Open a new ToDo.\n2. Fill in the description."):
        return frappe.get_doc(
            {
                "doctype": "FAC Skill",
                "skill_id": self.skill_id,
                "title": "Test role guide",
                "description": "Guide for System Managers: to-dos",
                "skill_type": role_guide.ROLE_GUIDE,
                "guide_doctypes": doctypes,
                "content": content,
                "status": "Published",
                "visibility": "Shared",
                "shared_with_roles": [{"role": "System Manager"}],
            }
        ).insert()

    def _read(self):
        return SkillManager().read_skill_content(self.skill_id)

    def test_get_doctype_info_with_setup_reads_this_sites_mandatory_fields(self):
        self._property("reqd", "1")
        reply = (
            get_tool_registry()
            .get_tool("get_doctype_info")
            ._safe_execute({"doctype": DOCTYPE, "setup": True})
        )
        self.assertTrue(reply["success"], reply)
        setup = reply["result"]["setup"]
        self.assertIn(FIELD, [f["fieldname"] for f in setup["mandatory"]])
        for key in ("tables", "naming", "workflow", "default_print_format", "print_formats"):
            self.assertIn(key, setup)

    def test_without_setup_get_doctype_info_is_unchanged(self):
        reply = get_tool_registry().get_tool("get_doctype_info")._safe_execute({"doctype": DOCTYPE})
        self.assertNotIn("setup", reply["result"])

    def test_a_guide_opened_on_an_unchanged_setup_has_no_note(self):
        guide = self._guide()
        self.assertTrue(guide.setup_snapshot)
        self.assertEqual(self._read(), guide.content)

    def test_a_new_mandatory_field_marks_the_guide_out_of_date(self):
        guide = self._guide()
        self._property("reqd", "1")
        content = self._read()
        self.assertTrue(content.startswith(f"> **{STALE}**"), content[:300])
        self.assertIn(f"new mandatory field {self.label}", content)
        self.assertTrue(content.endswith(guide.content))

    def test_a_new_default_print_format_marks_the_guide_out_of_date(self):
        self._guide()
        self._property("default_print_format", f"Client Format {self.tag}", field=None, property_type="Data")
        self.assertIn(f"default print format is now Client Format {self.tag}", self._read())

    def test_updating_the_guide_records_the_new_setup(self):
        guide = self._guide()
        self._property("reqd", "1")
        guide.content += f"\n3. Fill in {self.label}."
        guide.save()
        self.assertEqual(self._read(), guide.content)

    def test_changing_only_who_it_is_shared_with_keeps_the_note(self):
        guide = self._guide()
        self._property("reqd", "1")
        guide.append("shared_with_roles", {"role": "Desk User"})
        guide.save()
        self.assertIn(STALE, self._read())

    def test_a_guide_must_name_doctypes_that_exist(self):
        with self.assertRaises(frappe.ValidationError):
            self._guide(doctypes=f"No Such DocType {self.tag}")

    def test_the_doctypes_are_read_one_per_line_or_comma_separated(self):
        self.assertEqual(
            role_guide.doctypes_of("Sales Invoice, Quotation\n\nSales Invoice\n ToDo "),
            ["Sales Invoice", "Quotation", "ToDo"],
        )

    def test_other_skills_never_get_a_note(self):
        skill = frappe._dict(skill_type="Workflow", setup_snapshot='{"ToDo": null}')
        self.assertEqual(role_guide.stale_note(skill), "")
