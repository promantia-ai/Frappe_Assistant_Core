# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""FAC Onboarding Document (#34419 P0.3): saved apart from the chat, private to its owner."""

from unittest.mock import MagicMock, patch

import frappe

from frappe_assistant_core.tests.base_test import BaseAssistantTest
from frappe_assistant_core.utils import onboarding_document


class TestOnboardingDocument(BaseAssistantTest):
    def setUp(self):
        super().setUp()
        frappe.set_user("Administrator")

    def _save(self, document_type="Training Guide", **kw):
        return onboarding_document.save(document_type, "A guide", "## Step 1", data={"fields": ["a"]}, **kw)

    def test_each_document_type_saves_with_its_naming_series(self):
        for document_type in onboarding_document.TYPES:
            doc = self._save(document_type)
            self.assertRegex(doc.name, r"^FAC-DOC-\d{4}-\d{5}$")
            self.assertEqual(doc.document_type, document_type)
            self.assertEqual(doc.status, "Draft")

    def test_an_unknown_type_is_refused(self):
        with self.assertRaises(frappe.ValidationError):
            self._save("Love Letter")

    def test_the_facts_are_kept_and_hashed(self):
        doc = self._save()
        self.assertEqual(frappe.parse_json(doc.data), {"fields": ["a"]})
        self.assertEqual(doc.source_hash, onboarding_document.source_hash({"fields": ["a"]}))

    def test_the_hash_ignores_key_order_and_notices_a_change(self):
        self.assertEqual(
            onboarding_document.source_hash({"a": 1, "b": 2}),
            onboarding_document.source_hash({"b": 2, "a": 1}),
        )
        self.assertNotEqual(
            onboarding_document.source_hash({"a": 1}), onboarding_document.source_hash({"a": 2})
        )

    def test_the_owner_reads_it_and_another_desk_user_cannot(self):
        owner = self.make_desk_user("onb-owner")
        other = self.make_desk_user("onb-other")
        frappe.set_user(owner)
        doc = self._save()
        self.assertTrue(frappe.has_permission(onboarding_document.DOCTYPE, "read", doc.name))
        frappe.set_user(other)
        self.assertFalse(frappe.has_permission(onboarding_document.DOCTYPE, "read", doc.name))
        self.assertNotIn(doc.name, frappe.get_list(onboarding_document.DOCTYPE, pluck="name"))

    def test_a_system_manager_reads_every_document(self):
        owner = self.make_desk_user("onb-owner")
        frappe.set_user(owner)
        doc = self._save()
        frappe.set_user("Administrator")
        self.assertIn(doc.name, frappe.get_list(onboarding_document.DOCTYPE, pluck="name"))

    def test_chat_retention_leaves_the_documents(self):
        from frappe_assistant_core.utils import import_retention

        doc = self._save()
        import_retention.purge_old_sessions()
        self.assertTrue(frappe.db.exists(onboarding_document.DOCTYPE, doc.name))
        import_retention.purge([], "x")
        self.assertTrue(frappe.db.exists(onboarding_document.DOCTYPE, doc.name))

    def test_pdf_uses_generate_document_when_it_is_there(self):
        doc = self._save()
        tool = MagicMock()
        tool._safe_execute.return_value = {"success": True, "file_url": "/private/files/guide.pdf"}
        registry = MagicMock()
        registry.get_tool.return_value = tool
        with patch("frappe_assistant_core.core.tool_registry.get_tool_registry", return_value=registry):
            self.assertEqual(onboarding_document.pdf(doc.name), {"file_url": "/private/files/guide.pdf"})
        sent = tool._safe_execute.call_args[0][0]
        self.assertEqual(sent["content"], "## Step 1")

    def test_pdf_falls_back_to_the_print_page_without_generate_document(self):
        doc = self._save()
        registry = MagicMock()
        registry.get_tool.return_value = None
        with patch("frappe_assistant_core.core.tool_registry.get_tool_registry", return_value=registry):
            result = onboarding_document.pdf(doc.name)
        self.assertEqual(list(result), ["print_url"])
        self.assertIn(doc.name, result["print_url"])
