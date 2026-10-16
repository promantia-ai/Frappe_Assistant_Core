# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Update existing records from a file (#34005): rows are matched to records on any column, no-match
and many-match rows are reported, the check shows old and new values, and the update runs through
Data Import's Update Existing Records, so MIG-09 handles what fails.
"""

import unittest
import unittest.mock

import frappe

from frappe_assistant_core.plugins.core import import_update

# The module, not its class: a test class imported here would run again with this module's tests.
from frappe_assistant_core.tests import test_import_run as run_tests

UPDATE = "Update Existing Records"
KEYED = {"Tax ID": "tax_id", "Customer Group": "customer_group", "Website": "website"}


@unittest.skipUnless("erpnext" in frappe.get_installed_apps(), "needs ERPNext's Customer")
class UpdateCase(run_tests.ImportRunCase):
    def setUp(self):
        super().setUp()
        self.customers = {}
        for key in ("a", "b"):
            doc = frappe.get_doc(
                {
                    "doctype": "Customer",
                    "customer_name": f"Cust {key.upper()} {self.tag}",
                    "customer_group": self.group,
                    "customer_type": "Company",
                    "tax_id": f"TAX-{key.upper()}-{self.tag}",
                    "website": f"https://{key}.example",
                }
            ).insert()
            self.customers[key] = doc.name

    def _csv(self, lines, header=None):
        header = header or ",".join(KEYED)
        return self._file("update.csv", ("\n".join([header, *lines]) + "\n").encode())

    def _session(self, lines, mapping=None, match_on=("tax_id",), header=None):
        file_doc = self._csv(lines, header)
        session = self._run("start_import_session", {"file_id": file_doc.name, "import_type": UPDATE})
        session = session["result"]["session_id"]
        mapped = self._run(
            "set_column_mapping",
            {
                "session": session,
                "mapping": mapping or KEYED,
                "doctype": "Customer",
                "match_on": list(match_on),
            },
        )
        return session, mapped


class TestUpdateMapping(UpdateCase):
    def test_the_mapping_saves_the_match_fields(self):
        session, mapped = self._session([f"TAX-A-{self.tag},{self.group},https://new.example"])

        self.assertTrue(mapped["success"], mapped)
        doc = self._doc(session)
        self.assertEqual((doc.import_type, doc.match_key), (UPDATE, "tax_id"))
        self.assertEqual(import_update.match_fields(doc), ["tax_id"])

    def test_an_update_needs_to_know_which_column_finds_the_record(self):
        _session, mapped = self._session([f"TAX-A-{self.tag},{self.group},x"], match_on=())

        self.assertFalse(mapped["success"], mapped)
        self.assertIn("match_on", str(mapped))

    def test_a_match_field_must_be_a_mapped_column(self):
        _session, mapped = self._session([f"TAX-A-{self.tag},{self.group},x"], match_on=("customer_name",))

        self.assertFalse(mapped["success"], mapped)

    def test_child_table_columns_are_refused_in_update_mode(self):
        header = "Tax ID,Credit Limit"
        _session, mapped = self._session(
            [f"TAX-A-{self.tag},5000"],
            mapping={"Tax ID": "tax_id", "Credit Limit": "credit_limits.credit_limit"},
            header=header,
        )

        self.assertFalse(mapped["success"], mapped)
        self.assertIn("child table", str(mapped).lower())

    def test_a_column_mapped_to_the_id_is_the_match(self):
        header = "ID,Website"
        session, mapped = self._session(
            [f"{self.customers['a']},https://new.example"],
            mapping={"ID": "name", "Website": "website"},
            match_on=(),
            header=header,
        )

        self.assertTrue(mapped["success"], mapped)
        self.assertEqual(self._doc(session).match_key, "name")

    def test_changing_the_match_fields_makes_the_last_check_stale(self):
        session, _mapped = self._session([f"TAX-A-{self.tag},{self.group},https://new.example"])
        doc = self._doc(session)
        from frappe_assistant_core.plugins.core import import_dry_run as dry_run

        before = dry_run.fingerprint(doc, frappe.get_doc("File", doc.source_file))
        doc.match_key = "customer_group"

        self.assertNotEqual(before, dry_run.fingerprint(doc, frappe.get_doc("File", doc.source_file)))


class TestUpdatePermission(UpdateCase):
    def test_updating_needs_write_not_create(self):
        from frappe_assistant_core.plugins.core.import_session import import_target_problem

        def has_permission(doctype, ptype="read", *args, **kwargs):
            return ptype != "create"  # can write and import, can't create

        with unittest.mock.patch.object(frappe, "has_permission", side_effect=has_permission):
            self.assertIsNone(import_target_problem("Customer", UPDATE))
            self.assertIsNotNone(import_target_problem("Customer"))

    def test_updating_without_write_is_refused_before_any_work(self):
        from frappe_assistant_core.plugins.core.import_session import import_target_problem

        def has_permission(doctype, ptype="read", *args, **kwargs):
            return ptype != "write"

        with unittest.mock.patch.object(frappe, "has_permission", side_effect=has_permission):
            self.assertIn("update", import_target_problem("Customer", UPDATE))
