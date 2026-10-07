# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
FAC Import Session: one uploaded file moving through a data migration — read,
map, dry run, import.

The assistant is rebuilt on every turn, so this record is what keeps the file,
the mapping, the fixes and the validation results between messages. Its step
log records what was done, by whom, and what failed.
"""

from typing import Optional

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime

from frappe_assistant_core.chat.utils.permissions import _doc_owner_or_admin, _user_or_admin


class FACImportSession(Document):
    def log_step(
        self,
        step: str,
        outcome: str,
        message: str,
        rows_ok: Optional[int] = None,
        rows_failed: Optional[int] = None,
    ) -> None:
        """Append a row to the step log. The caller saves the session."""
        self.append(
            "steps",
            {
                "timestamp": now_datetime(),
                "step": step,
                "outcome": outcome,
                "user": frappe.session.user,
                "rows_ok": rows_ok,
                "rows_failed": rows_failed,
                "message": message,
            },
        )


# Same rule as FAC Chat Message: a session is visible to its user and to System Managers.
def get_permission_query_conditions(user=None):
    return _user_or_admin(user, "FAC Import Session")


def has_permission(doc, user=None, permission_type=None):
    return _doc_owner_or_admin(doc, user)
