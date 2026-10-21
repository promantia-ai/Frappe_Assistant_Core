# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
FAC Migration Plan (#33995): the import sessions of one migration, ordered so masters come
before the records that link to them, checked knowing what earlier files will create, and run
after one approval with no further questions.

The order, the masters the plan creates and the run's progress live here; each session points
to its plan through migration_plan. Its step log records the plan's own steps.
"""

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime

from frappe_assistant_core.chat.utils.permissions import _doc_owner_or_admin, _user_or_admin


class FACMigrationPlan(Document):
    def log_step(self, step: str, outcome: str, message: str) -> None:
        """Append a row to the step log. The caller saves the plan."""
        self.append(
            "steps",
            {
                "timestamp": now_datetime(),
                "step": step,
                "outcome": outcome,
                "user": frappe.session.user,
                "message": message,
            },
        )


# Same rule as FAC Import Session: a plan is visible to its user and to System Managers.
def get_permission_query_conditions(user=None):
    return _user_or_admin(user, "FAC Migration Plan")


def has_permission(doc, user=None, permission_type=None):
    return _doc_owner_or_admin(doc, user)
