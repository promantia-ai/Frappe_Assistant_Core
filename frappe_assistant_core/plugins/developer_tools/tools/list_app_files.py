# Copyright (C) 2025 Promantia
# Developer Tools Plugin — list_app_files tool

import fnmatch
import os
from typing import Any, Dict

import frappe
from frappe import _

from frappe_assistant_core.core.base_tool import BaseTool
from frappe_assistant_core.plugins.developer_tools.tools import (
    assert_system_manager,
    resolve_and_validate_path,
)

_MAX_RESULTS = 100
_MAX_RESULTS_LIMIT = 200

SKIP_NAMES = {"__pycache__", ".git", "node_modules", "dist", "build", ".pytest_cache"}


def _should_skip(name: str) -> bool:
    if name in SKIP_NAMES:
        return True
    if name.endswith(".egg-info"):
        return True
    if name.startswith("."):
        return True
    return False


class ListAppFiles(BaseTool):
    """
    Lists files and directories inside a Frappe app folder on the bench.
    """

    def __init__(self):
        super().__init__()
        self.name = "list_app_files"
        self.description = (
            "List files and directories inside a Frappe app folder on the bench. "
            "Returns a flat list of entries with name, type, and size. "
            "IMPORTANT: Always call describe_app first to understand the app structure "
            "before using this tool to explore specific folders. "
            "WORKFLOW: Call describe_app to get the full app tree — identify the folder "
            "you need — call list_app_files with that exact path — use read_file to "
            "read specific files found. "
            "Leave path empty to list all apps on the bench. "
            "Use pattern to filter by file type: '*.py' for Python, '*.json' for JSON, "
            "'*.js' for JavaScript. "
            "When pattern is given, only matching files are returned — directories are hidden. "
            "After showing results, ask the user before calling again with the next offset."
        )
        self.category = "Developer Tools"
        self.source_app = "frappe_assistant_core"

        self.inputSchema = {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Directory path relative to bench/apps/. "
                        "Leave empty to list all apps on the bench."
                    ),
                    "default": "",
                },
                "pattern": {
                    "type": "string",
                    "description": (
                        "Glob pattern to filter files e.g. '*.py', '*.json', '*.js'. "
                        "When set, only matching files are returned and directories are hidden."
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum entries to return per page. Default 100, max 200.",
                    "default": 100,
                },
                "offset": {
                    "type": "integer",
                    "description": "Start from this entry number for pagination. Default 0.",
                    "default": 0,
                },
            },
            "required": [],
        }

    def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        assert_system_manager()

        path = arguments.get("path", "").strip()
        pattern = arguments.get("pattern", None)
        max_results = arguments.get("max_results", _MAX_RESULTS)
        offset = arguments.get("offset", 0)

        try:
            max_results = int(max_results)
        except (TypeError, ValueError):
            frappe.throw(_("max_results must be an integer."), frappe.ValidationError)

        if max_results < 1 or max_results > _MAX_RESULTS_LIMIT:
            frappe.throw(
                _("max_results must be between 1 and {0}.").format(_MAX_RESULTS_LIMIT),
                frappe.ValidationError,
            )

        try:
            offset = int(offset)
        except (TypeError, ValueError):
            offset = 0
        if offset < 0:
            offset = 0

        if path == "":
            bench_path = frappe.utils.get_bench_path()
            abs_path = os.path.join(bench_path, "apps")
        else:
            abs_path = resolve_and_validate_path(path)

        if not os.path.isdir(abs_path):
            frappe.throw(
                _("Path '{0}' not found or not a directory.").format(path),
                frappe.ValidationError,
            )

        try:
            raw_entries = os.listdir(abs_path)
        except OSError as e:
            frappe.throw(
                _("Cannot list directory '{0}': {1}").format(path, str(e)),
                frappe.ValidationError,
            )

        dirs = sorted(
            [e for e in raw_entries if os.path.isdir(os.path.join(abs_path, e)) and not _should_skip(e)]
        )
        files = sorted(
            [e for e in raw_entries if os.path.isfile(os.path.join(abs_path, e)) and not _should_skip(e)]
        )

        if pattern:
            files = [f for f in files if fnmatch.fnmatch(f, pattern)]
            dirs = []

        all_entries = []
        for name in dirs:
            all_entries.append({"name": name, "type": "dir", "size": 0})
        for name in files:
            try:
                size = os.path.getsize(os.path.join(abs_path, name))
            except OSError:
                size = 0
            all_entries.append({"name": name, "type": "file", "size": size})

        total = len(all_entries)
        page = all_entries[offset : offset + max_results]
        truncated = (offset + len(page)) < total

        if truncated:
            end = offset + len(page)
            page.append(
                {
                    "name": f"[showing {offset + 1}-{end} of {total}. Call with offset={end} for more]",
                    "type": "notice",
                    "size": 0,
                }
            )

        return {
            "success": True,
            "path": path,
            "entries": page,
            "total": total,
            "files_shown": len(page) - (1 if truncated else 0),
            "truncated": truncated,
        }


list_app_files = ListAppFiles
