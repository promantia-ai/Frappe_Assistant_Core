# Frappe Assistant Core - JSON/XML chat attachment extraction tests
# Copyright (C) 2025 Paul Clinton
#
# AGPL-3.0 License

"""JSON and XML pass the upload allowlist, so they have to reach FACO too.
They used to fall through _detect_file_type as "unknown": dropped from the
prompt, and — now that failed extractions are logged — an Error Log per send.
"""

import base64
from unittest.mock import patch

import frappe

from frappe_assistant_core.chat.api.chat.helpers import _attach_files_to_message, _extract_file_attachments
from frappe_assistant_core.chat.api.settings.uploads import upload_message_file
from frappe_assistant_core.tests.base_test import BaseAssistantTest


class TestJsonXmlAttachments(BaseAssistantTest):
    def _assert_reaches_faco(self, file_name: str, content_type: str, text: str, message_name: str):
        with patch(
            "frappe_assistant_core.chat.api.settings.access.can_use_faco",
            return_value={"can_use": True},
        ):
            result = upload_message_file(
                file_data=base64.b64encode(text.encode()).decode(),
                file_name=file_name,
                content_type=content_type,
            )
        _attach_files_to_message([result["file"]["file_url"]], message_name)
        logs_before = frappe.db.count("Error Log")

        extracted = _extract_file_attachments(message_name)

        self.assertEqual(frappe.db.count("Error Log"), logs_before)
        self.assertIn(f"File: {file_name}", extracted)
        self.assertIn(text, extracted)

    def test_json_attachment_reaches_faco(self):
        # Unique per run: Frappe content-addresses uploads.
        tag = frappe.generate_hash(length=8)
        self._assert_reaches_faco(
            "customers.json",
            "application/json",
            f'[{{"customer_name": "Acme {tag}", "territory": "India"}}]',
            "FACMSG-TEST-JSON",
        )

    def test_xml_attachment_reaches_faco(self):
        tag = frappe.generate_hash(length=8)
        self._assert_reaches_faco(
            "customers.xml",
            "text/xml",
            f'<customers><customer territory="India">Acme {tag}</customer></customers>',
            "FACMSG-TEST-XML",
        )
