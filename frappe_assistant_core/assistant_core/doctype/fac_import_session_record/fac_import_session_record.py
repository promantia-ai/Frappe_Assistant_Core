# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""FAC Import Session Record: a master record an import session created, so it can be undone."""

from frappe.model.document import Document


class FACImportSessionRecord(Document):
    pass
