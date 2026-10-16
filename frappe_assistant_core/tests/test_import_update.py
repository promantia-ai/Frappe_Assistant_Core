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


class TestMatching(UpdateCase):
    def _matches(self, lines, match_on=("tax_id",), mapping=None, header=None):
        session, mapped = self._session(lines, mapping=mapping, match_on=match_on, header=header)
        self.assertTrue(mapped["success"], mapped)
        doc = self._doc(session)
        from frappe_assistant_core.plugins.core.import_session import read_sheet_rows, readable_session_file

        rows = read_sheet_rows(readable_session_file(doc), doc.sheet or None)
        return doc, rows, import_update.match_records(doc, rows)

    def test_each_row_finds_its_record_by_another_column(self):
        _doc, _rows, matches = self._matches(
            [f"TAX-A-{self.tag},{self.group},x", f"TAX-B-{self.tag},{self.group},y"]
        )

        self.assertEqual(matches, {2: [self.customers["a"]], 3: [self.customers["b"]]})

    def test_case_and_spaces_dont_stop_a_match(self):
        _doc, _rows, matches = self._matches([f"  tax-a-{self.tag} ,{self.group},x"])

        self.assertEqual(matches[2], [self.customers["a"]])

    def test_no_match_and_more_than_one_match(self):
        frappe.db.set_value("Customer", self.customers["b"], "tax_id", f"TAX-A-{self.tag}")

        _doc, _rows, matches = self._matches(
            [f"TAX-A-{self.tag},{self.group},x", f"TAX-Z-{self.tag},{self.group},y"]
        )

        self.assertEqual(sorted(matches[2]), sorted(self.customers.values()))
        self.assertEqual(matches[3], [])

    def test_a_key_of_two_columns(self):
        frappe.db.set_value("Customer", self.customers["b"], "tax_id", f"TAX-A-{self.tag}")
        other = f"Other {self.tag}"
        frappe.get_doc(
            {
                "doctype": "Customer Group",
                "customer_group_name": other,
                "parent_customer_group": "All Customer Groups",
            }
        ).insert()
        frappe.db.set_value("Customer", self.customers["b"], "customer_group", other)

        _doc, _rows, matches = self._matches(
            [f"TAX-A-{self.tag},{other},x"], match_on=("tax_id", "customer_group")
        )

        self.assertEqual(matches[2], [self.customers["b"]])

    def test_a_record_the_user_cannot_read_is_not_matched(self):
        def get_list(doctype, *args, **kwargs):
            return []  # what get_list returns to a user with no access to these records

        session, mapped = self._session([f"TAX-A-{self.tag},{self.group},x"])
        doc = self._doc(session)
        from frappe_assistant_core.plugins.core.import_session import read_sheet_rows, readable_session_file

        rows = read_sheet_rows(readable_session_file(doc), doc.sheet or None)
        with unittest.mock.patch.object(frappe, "get_list", side_effect=get_list):
            matches = import_update.match_records(doc, rows)

        self.assertEqual(matches[2], [])

    def test_the_id_column_is_added_and_the_key_is_never_written(self):
        doc, rows, matches = self._matches([f"tax-a-{self.tag},{self.group},https://new.example"])

        out, column_map = import_update.with_ids(doc, rows, {2: matches[2][0]})

        self.assertEqual(out[0][-1], "ID")
        self.assertEqual(out[1][-1], self.customers["a"])
        self.assertEqual(column_map[str(len(rows[0]))], "name")
        self.assertEqual(column_map["0"], "Don't Import")  # Tax ID: only used to find the record
        self.assertEqual(column_map["2"], "website")

    def test_matching_on_the_id_adds_no_column(self):
        doc, rows, matches = self._matches(
            [f"{self.customers['a']},https://new.example"],
            mapping={"ID": "name", "Website": "website"},
            match_on=(),
            header="ID,Website",
        )

        out, column_map = import_update.with_ids(doc, rows, {2: matches[2][0]})

        self.assertEqual(out, rows)
        self.assertEqual(column_map["0"], "name")


class TestUpdateCheck(UpdateCase):
    def _check(self, lines, **kwargs):
        session, mapped = self._session(lines, **kwargs)
        self.assertTrue(mapped["success"], mapped)
        reply = self._run("dry_run_import", {"session": session})
        self.assertTrue(reply["success"], reply)
        return session, reply["result"]

    def test_rows_with_no_match_or_many_are_reported_apart(self):
        frappe.db.set_value("Customer", self.customers["b"], "tax_id", f"TAX-A-{self.tag}")

        _session, result = self._check(
            [
                f"TAX-A-{self.tag},{self.group},x",
                f"TAX-Z-{self.tag},{self.group},y",
                f"TAX-Y-{self.tag},{self.group},z",
            ]
        )

        causes = {g["cause"]: g for g in result["groups"]}
        self.assertEqual(causes["No match"]["count"], 2)
        self.assertEqual(causes["More than one match"]["count"], 1)
        self.assertEqual(sorted(causes["More than one match"]["records"]), sorted(self.customers.values()))
        self.assertEqual((result["ready"], result["failed"]), (0, 3))

    def test_two_rows_for_the_same_record_are_both_refused(self):
        _session, result = self._check(
            [
                f"TAX-A-{self.tag},{self.group},https://one.example",
                f"tax-a-{self.tag},{self.group},https://two.example",
            ]
        )

        self.assertEqual([g["cause"] for g in result["groups"]], ["Same record twice"])
        self.assertEqual(result["failed"], 1)  # the second; the first stays ready

    def test_a_row_that_changes_nothing_is_unchanged_not_failed(self):
        _session, result = self._check([f"TAX-A-{self.tag},{self.group},https://a.example"])

        self.assertEqual((result["ready"], result["failed"], result["unchanged"]), (0, 0, 1))

    def test_the_check_writes_nothing(self):
        self._check([f"TAX-A-{self.tag},{self.group},https://new.example"])

        self.assertEqual(frappe.db.get_value("Customer", self.customers["a"], "website"), "https://a.example")

    def test_erpnext_rules_still_apply_to_an_update(self):
        _session, result = self._check([f"TAX-A-{self.tag},No Such Group {self.tag},x"])

        self.assertEqual(result["groups"][0]["cause"], "Missing link")

    def test_the_results_keep_each_rows_record(self):
        session, _result = self._check([f"TAX-A-{self.tag},{self.group},https://new.example"])

        self.assertEqual(import_update.checked_records(self._doc(session)), {2: self.customers["a"]})
