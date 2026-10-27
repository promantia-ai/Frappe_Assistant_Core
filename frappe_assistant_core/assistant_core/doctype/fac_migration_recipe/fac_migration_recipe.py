# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""A migration saved as a recipe (#34420): how each file of a finished migration was mapped, so the
same migration can be run again on new files, on another site, or for the transactions made after
the cut-off. Holds no row data (M-D1)."""

import json

import frappe
from frappe import _
from frappe.model.document import Document

FORMAT_VERSION = 1
JSON_FIELDS = ("header_signature", "column_map", "transformation_rules", "engine_settings")


class FACMigrationRecipe(Document):
    def validate(self):
        if not self.format_version:
            self.format_version = FORMAT_VERSION
        if not self.created_by:
            self.created_by = frappe.session.user
        if not self.created_on:
            self.created_on = frappe.utils.now_datetime()
        if not self.fac_version:
            self.fac_version = frappe.get_attr("frappe_assistant_core.__version__")
        for row in self.steps:
            for fieldname in JSON_FIELDS:
                value = row.get(fieldname)
                if not value:
                    continue
                try:
                    json.loads(value)
                except ValueError:
                    frappe.throw(
                        _("Step {0}: {1} is not valid JSON.").format(
                            row.position, row.meta.get_label(fieldname)
                        )
                    )
