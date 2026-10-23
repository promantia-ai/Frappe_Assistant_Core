# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
#34413 and #34438: the review's smaller findings on the data migration, one test each.
"""

import unittest
from unittest.mock import patch

import frappe

from frappe_assistant_core.assistant_core.doctype.fac_import_session.fac_import_session import engine_session
from frappe_assistant_core.tests.test_import_mapping import ImportToolTestCase
from frappe_assistant_core.utils import import_failures


class TestFailedRowsFile(unittest.TestCase):
    def test_a_cell_that_starts_like_a_formula_is_not_run_by_a_spreadsheet(self):
        for cell in ('=HYPERLINK("http://x")', "+1+1", "-2+3", "@SUM(A1)", "\tx"):
            with self.subTest(cell=cell):
                self.assertEqual(import_failures.csv_cell(cell), "'" + cell)

    def test_numbers_and_plain_text_are_left_as_they_are(self):
        for cell in ("-5", "+3.5", "Acme", "", "1=1"):
            with self.subTest(cell=cell):
                self.assertEqual(import_failures.csv_cell(cell), cell)


class TestSessionDoctype(ImportToolTestCase):
    def test_the_owner_is_indexed_as_every_list_filters_on_it(self):
        self.assertTrue(frappe.get_meta("FAC Import Session").get_field("user").search_index)

    def test_a_save_keeps_its_history_without_the_large_json_fields(self):
        session = self._session()
        doc = engine_session(session)  # only the engine writes a session
        doc.validation_summary = frappe.as_json({"rows": ["x" * 1000] * 50})
        doc.status = "Dry Run Failed"
        doc.save(ignore_version=False)  # Frappe skips Versions under tests unless asked

        version = frappe.get_last_doc(
            "Version", filters={"ref_doctype": "FAC Import Session", "docname": session}
        )
        changed = {change[0] for change in frappe.parse_json(version.data).get("changed", [])}
        self.assertIn("status", changed)
        self.assertNotIn("validation_summary", changed)

    def test_the_sites_max_file_size_is_kept_when_the_engine_reads_a_file(self):
        from frappe_assistant_core.utils import import_session

        file_doc = frappe.get_doc(
            {"doctype": "File", "file_name": "small.csv", "content": b"Name\nAcme\n", "is_private": 1}
        ).insert()
        with patch.object(import_session, "get_max_file_size", return_value=4):
            with self.assertRaises(import_session.FileProblem) as raised:
                import_session.read_sheet_rows(file_doc)
        self.assertIn("largest file this site takes", str(raised.exception))


class TestMessages(unittest.TestCase):
    def test_the_dry_run_and_the_import_read_a_message_log_alike(self):
        from frappe_assistant_core.utils import import_dry_run

        log = [frappe.as_json({"message": "<b>Row 3</b>:  refused"}), {"message": "last  one"}, "plain"]

        self.assertEqual(import_dry_run.message_texts(log), ["Row 3: refused", "last one", "plain"])
        self.assertEqual(import_failures.log_reason(frappe.as_json(log)), "plain")

    def test_a_rows_check_leaves_the_requests_own_messages_alone(self):
        from frappe_assistant_core.utils import import_dry_run

        frappe.clear_messages()
        frappe.msgprint(frappe._("before the check"))
        with import_dry_run.own_messages():
            frappe.msgprint(frappe._("the row's"))
            self.assertEqual(len(frappe.local.message_log), 1)

        self.assertEqual(
            [
                frappe.parse_json(m)["message"] if isinstance(m, str) else m["message"]
                for m in frappe.local.message_log
            ],
            ["before the check"],
        )
        frappe.clear_messages()


class TestRealImportOptions(unittest.TestCase):
    def test_the_import_parses_with_the_date_formats_the_check_used(self):
        from frappe_assistant_core.utils import import_run

        session = frappe._dict(
            template_options=frappe.as_json(
                {
                    "column_to_field_map": {"0": "x"},
                    "column_to_date_format_map": {"2": "%d-%m-%Y"},
                    "undo": {},
                }
            )
        )

        options = import_run.data_import_options(session, {"0": "customer_name"})

        self.assertEqual(
            options,
            {"column_to_field_map": {"0": "customer_name"}, "column_to_date_format_map": {"2": "%d-%m-%Y"}},
        )
