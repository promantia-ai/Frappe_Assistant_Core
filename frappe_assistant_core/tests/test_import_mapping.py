# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""
get_import_schema and set_column_mapping (#33989).

The schema has to stay short (get_doctype_info's full metadata is cut off before the model
reads it, which is how a live test missed Delivery Note's po_no), and every mapping saved
has to be one Frappe's Data Import accepts as it stands, since #33993 copies it across.
"""

import json
import unittest
from unittest.mock import patch

import frappe
from frappe.core.doctype.data_import.importer import get_df_for_column_header

from frappe_assistant_core.core.tool_registry import get_tool_registry
from frappe_assistant_core.plugins.core.tools import import_mapping
from frappe_assistant_core.plugins.core.tools.get_doctype_info import GetDoctypeInfo
from frappe_assistant_core.plugins.core.tools.get_import_schema import GetImportSchema
from frappe_assistant_core.plugins.core.tools.set_column_mapping import SetColumnMapping
from frappe_assistant_core.tests.base_test import BaseAssistantTest

FIELDS = import_mapping.SESSION_FIELDS
CONTACT_COLUMNS = ["First Name", "Last Name", "E-mail", "Mobile", "Company", "Notes"]


class FakeSession(frappe._dict):
    """A stand-in for a FAC Import Session row, with the fields #33988's design names."""

    def set(self, key, value):
        self[key] = value

    def save(self):
        self.saves = self.get("saves", 0) + 1

    @property
    def column_to_field_map(self):
        return json.loads(self[FIELDS["mapping"]] or "{}").get("column_to_field_map", {})


def _session(target="Contact", columns=CONTACT_COLUMNS, mapping=None):
    return FakeSession(
        {
            "name": "IMP-TEST-00001",
            FIELDS["target"]: target,
            FIELDS["sheet"]: "Contacts",
            FIELDS["sheets"]: json.dumps([{"name": "Contacts", "headers": list(columns), "row_count": 3}]),
            FIELDS["mapping"]: json.dumps({"column_to_field_map": mapping or {}}),
        }
    )


class TestGetImportSchema(BaseAssistantTest):
    def setUp(self):
        super().setUp()
        self.addCleanup(frappe.set_user, frappe.session.user)

    def _schema(self, doctype):
        return GetImportSchema().execute({"doctype": doctype})

    def test_lists_importable_fields_and_child_tables_apart(self):
        schema = self._schema("Contact")

        self.assertTrue(schema["success"], schema.get("error"))
        self.assertIn("first_name", [f["fieldname"] for f in schema["fields"]])
        tables = {t["fieldname"]: t for t in schema["child_tables"]}
        self.assertEqual(tables["email_ids"]["doctype"], "Contact Email")
        email_id = next(f for f in tables["email_ids"]["fields"] if f["fieldname"] == "email_id")
        self.assertTrue(email_id["required"])

    def test_leaves_out_fields_an_import_does_not_set(self):
        meta = frappe.get_meta("Contact")
        for field in self._schema("Contact")["fields"]:
            df = meta.get_field(field["fieldname"])
            self.assertFalse(df.hidden or df.read_only, field["fieldname"])
            self.assertNotIn(df.fieldtype, ("Section Break", "Column Break", "Tab Break", "HTML", "Table"))

    @unittest.skipUnless("erpnext" in frappe.get_installed_apps(), "needs ERPNext")
    def test_delivery_note_includes_po_no_and_stays_short(self):
        schema = self._schema("Delivery Note")

        self.assertIn("po_no", [f["fieldname"] for f in schema["fields"]])
        size = len(json.dumps(schema, default=str))
        full = len(json.dumps(GetDoctypeInfo().execute({"doctype": "Delivery Note"}), default=str))
        self.assertLess(size, 20000)
        self.assertLess(size, full / 3)

    def test_child_table_is_refused_and_points_to_its_parent(self):
        result = self._schema("Contact Email")
        self.assertFalse(result["success"])
        self.assertIn("child table", result["error"])
        self.assertIn("Contact", result["error"])

    def test_doctypes_data_import_refuses_are_refused(self):
        for doctype, reason in (
            ("System Settings", "single record"),
            ("DocType", "not allowed"),
            ("Error Log", "Allow Import"),
        ):
            with self.subTest(doctype=doctype):
                result = self._schema(doctype)
                self.assertFalse(result["success"])
                self.assertIn(reason, result["error"])

    def test_unknown_doctype_suggests_the_closest(self):
        result = self._schema("Contct")
        self.assertFalse(result["success"])
        self.assertIn("Contact", result["suggestions"])

    def test_user_without_import_permission_is_told_before_any_work(self):
        frappe.set_user("Guest")
        result = self._schema("Contact")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_type"], "permission_error")
        self.assertIn("Contact", result["error"])


class TestSetColumnMapping(BaseAssistantTest):
    # FAC Import Session arrives with #33988. Until then load_session, the one function
    # that reads it from the database, returns a stand-in with the fields its design names.
    def setUp(self):
        super().setUp()
        self.addCleanup(frappe.set_user, frappe.session.user)

    def _map(self, doc, mapping, **arguments):
        with patch.object(import_mapping, "load_session", autospec=True, return_value=doc):
            return SetColumnMapping().execute({"session": doc.name, "mapping": mapping, **arguments})

    def test_saves_the_mapping_in_data_import_shape(self):
        doc = _session()
        result = self._map(
            doc,
            {
                "First Name": "first_name",
                "Last Name": "Last Name",  # a label works too
                "E-mail": "email_ids.email_id",
                "3": "phone_nos.phone",  # so does a position
                "Notes": "Don't Import",
            },
        )

        self.assertTrue(result["success"], result)
        self.assertEqual(
            doc.column_to_field_map,
            {
                "0": "first_name",
                "1": "last_name",
                "2": "email_ids.email_id",
                "3": "phone_nos.phone",
                "5": "Don't Import",
            },
        )
        # Data Import resolves every saved value as it stands.
        for field in doc.column_to_field_map.values():
            if field != "Don't Import":
                self.assertIsNotNone(get_df_for_column_header("Contact", field), field)

    def test_child_table_fields_are_shown_apart(self):
        result = self._map(_session(), {"First Name": "first_name", "E-mail": "email_ids.email_id"})

        self.assertEqual([row["field"] for row in result["mapping"]], ["first_name"])
        self.assertEqual(
            result["child_tables"],
            {
                "Email IDs (email_ids)": [
                    {"column": "E-mail", "position": 2, "field": "email_ids.email_id", "label": "Email ID"}
                ]
            },
        )

    def test_unmapped_columns_and_required_fields_without_a_column_are_marked(self):
        doc = _session(target="Address", columns=["Street", "Town", "Remarks"])
        result = self._map(doc, {"Street": "address_line1", "Town": "city"})

        self.assertTrue(result["success"], result)
        self.assertEqual(result["unmapped_columns"], ["Remarks"])
        self.assertEqual(sorted(m["field"] for m in result["missing_required"]), ["address_type", "country"])

    def test_a_child_tables_required_fields_count_once_it_is_used(self):
        result = self._map(_session(), {"Mobile": "phone_nos.is_primary_mobile_no"})

        self.assertEqual(
            result["missing_required"],
            [{"field": "phone_nos.phone", "label": "Number", "table": "Contact Numbers (phone_nos)"}],
        )

    def test_unknown_field_is_rejected_with_a_suggestion_and_nothing_is_saved(self):
        doc = _session()
        result = self._map(doc, {"First Name": "frist_name", "Last Name": "last_name"})

        self.assertFalse(result["success"])
        self.assertIn("'first_name'", result["problems"][0])
        self.assertEqual(doc.column_to_field_map, {})
        self.assertNotIn("saves", doc)

    def test_read_only_field_is_explained(self):
        result = self._map(_session(), {"E-mail": "email_id"})
        self.assertFalse(result["success"])
        self.assertIn("read-only", result["problems"][0])

    def test_unknown_column_lists_the_real_ones(self):
        result = self._map(_session(), {"Fax": "first_name"})
        self.assertFalse(result["success"])
        self.assertIn("First Name", result["problems"][0])

    def test_two_columns_on_one_field_are_rejected(self):
        result = self._map(_session(), {"First Name": "first_name", "Last Name": "first_name"})
        self.assertFalse(result["success"])
        self.assertIn("first_name", result["problems"][0])

    def test_a_correction_changes_that_column_and_keeps_the_rest(self):
        doc = _session()
        self._map(doc, {"First Name": "first_name", "Company": "company_name"})

        result = self._map(doc, {"Company": "designation"})

        self.assertTrue(result["success"], result)
        self.assertEqual(doc.column_to_field_map, {"0": "first_name", "4": "designation"})

    def test_changing_the_target_clears_the_old_mapping(self):
        doc = _session(columns=["Street", "Town"], mapping={"0": "first_name"})
        result = self._map(doc, {"Town": "city"}, doctype="Address")

        self.assertTrue(result["success"], result)
        self.assertEqual(doc[FIELDS["target"]], "Address")
        self.assertEqual(doc.column_to_field_map, {"1": "city"})

    def test_a_session_without_a_target_asks_for_one(self):
        result = self._map(_session(target=None), {"First Name": "first_name"})
        self.assertFalse(result["success"])
        self.assertIn("doctype", result["error"])

    def test_target_permission_is_checked_before_anything_is_saved(self):
        doc = _session()
        frappe.set_user("Guest")
        result = self._map(doc, {"First Name": "first_name"})

        self.assertEqual(result["error_type"], "permission_error")
        self.assertNotIn("saves", doc)

    def test_says_so_while_import_sessions_are_not_installed(self):
        if frappe.db.exists("DocType", import_mapping.SESSION_DOCTYPE):
            self.skipTest("FAC Import Session is installed")
        result = SetColumnMapping().execute({"session": "IMP-2026-00001", "mapping": {"A": "first_name"}})
        self.assertFalse(result["success"])
        self.assertIn("FAC Import Session", result["error"])


class TestImportToolsAreListed(BaseAssistantTest):
    def test_both_tools_are_available(self):
        names = [tool["name"] for tool in get_tool_registry().get_available_tools()]
        self.assertIn("get_import_schema", names)
        self.assertIn("set_column_mapping", names)
