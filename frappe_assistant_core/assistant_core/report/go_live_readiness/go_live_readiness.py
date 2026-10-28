# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Go-Live Readiness (#34424): every item a site needs before the client goes live, Pass, Warn or
Fail with the fix. The checks are made in go_live_check."""

from frappe_assistant_core.utils import go_live_check


def execute(filters=None):
    return go_live_check.execute(filters)
