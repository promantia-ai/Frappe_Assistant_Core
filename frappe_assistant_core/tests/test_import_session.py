"""
Tests for import sessions (#33988): the FAC Import Session doctype and the
start_import_session tool.

A session is the server-side record of one file moving through a data migration:
its details, the mapping, the fixes, the validation results and a log of every
step. The file itself stays where the user attached it; no copy is saved.
"""

import datetime
import io

import frappe
from openpyxl import Workbook

from frappe_assistant_core.core.tool_registry import get_tool_registry
from frappe_assistant_core.tests.base_test import BaseAssistantTest

CSV_CONTENT = b"Customer Name,Customer Group,Territory\n" + b"".join(
    f"Customer {i},Retail,India\n".encode() for i in range(1, 26)
)


def _xlsx_bytes(sheets: dict) -> bytes:
    """Build an .xlsx in memory. ``sheets`` maps sheet name -> list of rows."""
    wb = Workbook()
    wb.remove(wb.active)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class TestImportSession(BaseAssistantTest):
    def setUp(self):
        super().setUp()
        self.registry = get_tool_registry()
        # A plain desk user: owns the files they upload, holds no import rights.
        self.user = self.make_throwaway_user("importer", roles=("Blogger",))
        frappe.set_user(self.user)
        self.files_before = set(frappe.get_all("File", pluck="name"))
        self.sessions_before = frappe.db.count("FAC Import Session")

    def tearDown(self):
        frappe.local.ar_session_id = None
        frappe.set_user("Administrator")
        # The transaction rollback removes File rows but not the files on disk.
        for name in set(frappe.get_all("File", pluck="name")) - self.files_before:
            frappe.delete_doc("File", name, force=True, ignore_permissions=True)
        super().tearDown()

    # -- helpers ---------------------------------------------------------------

    def _upload(self, file_name: str, content: bytes):
        """A private File owned by the current user, as a chat upload creates."""
        return frappe.get_doc(
            {"doctype": "File", "file_name": file_name, "content": content, "is_private": 1}
        ).insert()

    def _run(self, tool_name, arguments):
        """Call a tool the way the MCP endpoint does: through _safe_execute."""
        return self.registry.get_tool(tool_name)._safe_execute(arguments)

    def _start(self, **arguments):
        response = self._run("start_import_session", arguments)
        self.assertTrue(response["success"], response)
        return response["result"]

    def _start_fails(self, expected_text=None, **arguments):
        response = self._run("start_import_session", arguments)
        self.assertFalse(response["success"], response)
        if expected_text:
            self.assertIn(expected_text, response["error"])
        return response

    def _new_sessions(self):
        """Sessions created since this test began (the rollback runs per class, not per test)."""
        return frappe.db.count("FAC Import Session") - self.sessions_before

    def _new_files(self):
        return set(frappe.get_all("File", pluck="name")) - self.files_before

    # -- registration ------------------------------------------------------------

    def test_tool_is_registered_in_core_plugin(self):
        from frappe_assistant_core.plugins.core.plugin import CorePlugin

        self.assertIn("start_import_session", CorePlugin().get_tools())

    def test_tool_is_classified_read_only(self):
        # It writes only the session and its working copy, never business data,
        # so it must not raise an approval card.
        from frappe_assistant_core.utils.tool_category_detector import detect_tool_category

        tool = self.registry.get_tool("start_import_session")
        self.assertEqual(detect_tool_category(tool), "read_only")

    # -- criterion 1: open a session, show sheets, columns and row counts -----------

    def test_csv_opens_a_session_with_columns_count_and_samples(self):
        file_doc = self._upload("customers.csv", CSV_CONTENT)

        result = self._start(file_url=file_doc.file_url)

        self.assertEqual(result["columns"], ["Customer Name", "Customer Group", "Territory"])
        self.assertEqual(result["row_count"], 25)
        self.assertEqual(len(result["sample_rows"]), 10)
        self.assertEqual(result["sample_rows"][0], ["Customer 1", "Retail", "India"])
        doc = frappe.get_doc("FAC Import Session", result["session_id"])
        self.assertEqual(doc.source_file, file_doc.name)
        self.assertEqual(doc.user, self.user)
        self.assertEqual(doc.status, "File Read")

    def test_session_name_follows_the_imp_year_series(self):
        file_doc = self._upload("customers.csv", CSV_CONTENT)

        session_id = self._start(file_url=file_doc.file_url)["session_id"]

        self.assertRegex(session_id, rf"^IMP-{datetime.date.today().year}-\d{{5}}$")

    def test_only_ten_sample_rows_reach_the_conversation(self):
        file_doc = self._upload("customers.csv", CSV_CONTENT)

        result = self._start(file_url=file_doc.file_url)

        self.assertNotIn("Customer 11", frappe.as_json(result))

    def test_workbook_lists_every_sheet_and_uses_the_first_with_data(self):
        content = _xlsx_bytes(
            {
                "Notes": [],
                "Customers": [["Name", "Group"], ["A", "Retail"], ["B", "Retail"]],
                "Old": [["Code"], ["X"]],
            }
        )
        file_doc = self._upload("export.xlsx", content)

        result = self._start(file_url=file_doc.file_url)

        self.assertEqual(result["sheet"], "Customers")
        self.assertEqual(
            [(s["name"], s["row_count"]) for s in result["sheets"]],
            [("Notes", 0), ("Customers", 2), ("Old", 1)],
        )
        stored = frappe.parse_json(frappe.db.get_value("FAC Import Session", result["session_id"], "sheets"))
        self.assertEqual([s["name"] for s in stored], ["Notes", "Customers", "Old"])

    def test_sheet_argument_chooses_the_sheet(self):
        content = _xlsx_bytes({"Customers": [["Name"], ["A"]], "Items": [["Code"], ["I-1"]]})
        file_doc = self._upload("export.xlsx", content)

        result = self._start(file_url=file_doc.file_url, sheet="Items")

        self.assertEqual(result["sheet"], "Items")
        self.assertEqual(result["columns"], ["Code"])

    def test_unknown_sheet_lists_the_sheets_that_exist(self):
        file_doc = self._upload("export.xlsx", _xlsx_bytes({"Customers": [["Name"], ["A"]]}))

        self._start_fails("Customers", file_url=file_doc.file_url, sheet="Suppliers")

        self.assertEqual(self._new_sessions(), 0)

    def test_file_without_rows_is_refused(self):
        file_doc = self._upload("empty.csv", b"Name,Group\n")

        self._start_fails("no rows", file_url=file_doc.file_url)

        self.assertEqual(self._new_sessions(), 0)

    def test_unsupported_file_type_is_refused(self):
        file_doc = self._upload("notes.txt", b"hello")

        self._start_fails(".csv, .xlsx or .xls", file_url=file_doc.file_url)

        self.assertEqual(self._new_sessions(), 0)

    def test_excel_dates_and_whole_numbers_read_as_plain_text(self):
        content = _xlsx_bytes({"Stock": [["Date", "Qty"], [datetime.datetime(2026, 4, 1), 12.0]]})
        file_doc = self._upload("stock.xlsx", content)

        result = self._start(file_url=file_doc.file_url)

        self.assertEqual(result["sample_rows"][0], ["2026-04-01", "12"])

    # -- no copy of the file is kept ------------------------------------------------------

    def test_a_site_file_is_read_but_no_copy_is_saved(self):
        file_doc = self._upload("customers.csv", CSV_CONTENT)

        session_id = self._start(file_url=file_doc.file_url)["session_id"]

        self.assertEqual(self._new_files(), {file_doc.name})
        doc = frappe.get_doc("FAC Import Session", session_id)
        self.assertEqual(doc.source_file, file_doc.name)
        self.assertEqual(doc.file_source, "Site File")

    # -- a file that stays in the chat (Claude, ChatGPT) -----------------------------------

    def test_details_of_a_chat_attachment_are_recorded_without_a_file(self):
        # Claude or ChatGPT reads the attachment itself and passes only its details.
        result = self._start(
            file_name="customers.xlsx",
            sheet="Customers",
            columns=["Customer Name", "Customer Group"],
            row_count=15,
            sample_rows=[["Grant Plastics", "Commercial"], ["Kaveri Textiles", "Retial"]],
        )

        self.assertEqual(self._new_files(), set())
        doc = frappe.get_doc("FAC Import Session", result["session_id"])
        self.assertEqual(doc.file_source, "Chat Attachment")
        self.assertFalse(doc.source_file)
        self.assertEqual((doc.file_name, doc.sheet), ("customers.xlsx", "Customers"))
        self.assertEqual(
            frappe.parse_json(doc.sheets),
            [
                {
                    "name": "Customers",
                    "columns": ["Customer Name", "Customer Group"],
                    "row_count": 15,
                    "sample_rows": [["Grant Plastics", "Commercial"], ["Kaveri Textiles", "Retial"]],
                }
            ],
        )
        self.assertEqual([(s.step, s.outcome) for s in doc.steps], [("Read File", "Success")])
        self.assertIn("15 rows", doc.steps[0].message)

    def test_a_chat_attachment_needs_its_columns_and_row_count(self):
        self._start_fails("columns", file_name="customers.xlsx", row_count=15)
        self._start_fails("row_count", file_name="customers.xlsx", columns=["Name"])

        self.assertEqual(self._new_sessions(), 0)

    def test_at_most_ten_sample_rows_are_kept_from_the_chat(self):
        rows = [[f"Customer {i}"] for i in range(1, 16)]

        result = self._start(file_name="customers.csv", columns=["Name"], row_count=15, sample_rows=rows)

        stored = frappe.parse_json(frappe.db.get_value("FAC Import Session", result["session_id"], "sheets"))
        self.assertEqual(len(stored[0]["sample_rows"]), 10)

    def test_chat_attachment_target_permission_is_checked_first(self):
        self._start_fails(
            "permission to create Currency",
            file_name="currencies.csv",
            columns=["Currency Name"],
            row_count=1,
            target_doctype="Currency",
        )

        self.assertEqual(self._new_sessions(), 0)

    def test_neither_a_file_url_nor_a_file_name_is_refused(self):
        self._start_fails("file_name")

    # -- criterion 4: permission on the target doctype, before any work -------------------

    def test_target_without_create_permission_is_refused_before_reading(self):
        file_doc = self._upload("currencies.csv", b"Currency Name\nXYZ\n")

        self._start_fails(
            "permission to create Currency", file_url=file_doc.file_url, target_doctype="Currency"
        )

        self.assertEqual(self._new_sessions(), 0)
        self.assertEqual(set(frappe.get_all("File", pluck="name")) - self.files_before, {file_doc.name})

    def test_target_without_import_permission_is_refused(self):
        # Any desk user may create a Contact (role All, if owner); only System
        # Manager holds Import on it.
        file_doc = self._upload("contacts.csv", b"First Name\nAsha\n")

        self._start_fails("Import permission", file_url=file_doc.file_url, target_doctype="Contact")

        self.assertEqual(self._new_sessions(), 0)

    def test_target_that_cannot_be_imported_is_refused(self):
        file_doc = self._upload("todo.csv", b"Description\nCall\n")

        self._start_fails("can't be imported", file_url=file_doc.file_url, target_doctype="ToDo")

    def test_permitted_target_is_stored_on_the_session(self):
        frappe.set_user("Administrator")
        file_doc = self._upload("currencies.csv", b"Currency Name\nXYZ\n")

        session_id = self._start(file_url=file_doc.file_url, target_doctype="Currency")["session_id"]

        doc = frappe.get_doc("FAC Import Session", session_id)
        self.assertEqual(doc.target_doctype, "Currency")
        self.assertEqual(doc.import_type, "Insert New Records")

    # -- file access -----------------------------------------------------------------------

    def test_another_users_private_file_cannot_be_read(self):
        frappe.set_user(self.make_throwaway_user("other", roles=("Blogger",)))
        theirs = self._upload("theirs.csv", CSV_CONTENT)
        frappe.set_user(self.user)

        response = self._start_fails(file_url=theirs.file_url)

        self.assertNotIn("Customer 1", frappe.as_json(response))
        self.assertEqual(self._new_sessions(), 0)

    # -- criterion 2: the same conversation, later ------------------------------------------

    def test_session_records_the_conversation_it_belongs_to(self):
        # FAC Chat sends X-AR-Session-Id on every tool call; fac_endpoint stores it here.
        frappe.local.ar_session_id = "chat-123"
        file_doc = self._upload("customers.csv", CSV_CONTENT)

        session_id = self._start(file_url=file_doc.file_url)["session_id"]

        self.assertEqual(frappe.db.get_value("FAC Import Session", session_id, "chat_session_id"), "chat-123")

    def test_mapping_fixes_and_validation_come_back_in_a_later_turn(self):
        file_doc = self._upload("customers.csv", CSV_CONTENT)
        session_id = self._start(file_url=file_doc.file_url)["session_id"]
        # Production writes these through the mapping, rules and dry-run tools of
        # #33989-#33991; set directly here to prove a later turn reads them back.
        doc = frappe.get_doc("FAC Import Session", session_id)
        doc.template_options = frappe.as_json({"column_to_field_map": {"0": "customer_name"}})
        doc.transformation_rules = frappe.as_json([{"type": "trim", "column": "*"}])
        doc.validation_summary = frappe.as_json({"ok": 20, "failed": 5})
        doc.save()

        response = self._run("get_document", {"doctype": "FAC Import Session", "name": session_id})

        self.assertTrue(response["success"], response)
        data = response["result"]["data"]
        self.assertEqual(
            frappe.parse_json(data["template_options"]), {"column_to_field_map": {"0": "customer_name"}}
        )
        self.assertEqual(frappe.parse_json(data["transformation_rules"]), [{"type": "trim", "column": "*"}])
        self.assertEqual(frappe.parse_json(data["validation_summary"]), {"ok": 20, "failed": 5})

    # -- criterion 3 and privacy: who sees a session --------------------------------------

    def test_another_user_cannot_read_the_session(self):
        file_doc = self._upload("customers.csv", CSV_CONTENT)
        session_id = self._start(file_url=file_doc.file_url)["session_id"]

        other = self.make_throwaway_user("other", roles=("Blogger",))

        self.assertFalse(frappe.has_permission("FAC Import Session", "read", session_id, user=other))
        frappe.set_user(other)
        self.assertNotIn(session_id, frappe.get_list("FAC Import Session", pluck="name"))

    def test_a_system_manager_sees_every_session(self):
        file_doc = self._upload("customers.csv", CSV_CONTENT)
        session_id = self._start(file_url=file_doc.file_url)["session_id"]

        manager = self.make_throwaway_user("manager", roles=("System Manager",))

        self.assertTrue(frappe.has_permission("FAC Import Session", "read", session_id, user=manager))

    # -- audit ---------------------------------------------------------------------------------

    def test_reading_the_file_is_logged_as_the_first_step(self):
        file_doc = self._upload("customers.csv", CSV_CONTENT)

        session_id = self._start(file_url=file_doc.file_url)["session_id"]

        steps = frappe.get_doc("FAC Import Session", session_id).steps
        self.assertEqual([(s.step, s.outcome, s.user) for s in steps], [("Read File", "Success", self.user)])
        self.assertIn("25 rows", steps[0].message)
