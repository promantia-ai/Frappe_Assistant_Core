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

"""A document the consultant hands to the client, kept apart from the chat (#34419).

A setup record, a go-live checklist, a training guide or a hypercare report. It lives here, not in
a conversation or an import session, so chat retention never deletes it. Its owner and System
Managers can read it; the stories that share it say who else can (a training guide for the people
with its role, a hypercare report for its consultant).
"""

from frappe.model.document import Document


class FACOnboardingDocument(Document):
    pass
