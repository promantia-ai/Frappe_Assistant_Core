# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Hypercare Summary (#34426): what went wrong on the site in a period after go-live, with fixes and
training points. Counted in utils/hypercare, which also sends it to the consultant every week."""

from frappe_assistant_core.utils import hypercare


def execute(filters=None):
    return hypercare.execute(filters)
