# Copyright (C) 2025 Promantia
# Developer Tools Plugin — FixtureService

import ast
import json
import os

import frappe
from frappe import _

from frappe_assistant_core.plugins.developer_tools.guards import (
    assert_app_dir_exists,
    assert_not_protected_app,
    assert_required,
    build_failure,
    get_app_path,
)

ALLOWED_FIXTURE_DOCTYPES = {
    "Custom Field",
    "Client Script",
    "Server Script",
    "Property Setter",
    "Role",
    "Workflow",
    "Print Format",
    "Notification",
}


def _convert_filters_to_list(filters: dict) -> list:
    result = []
    for field, value in filters.items():
        if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
            result.append([field, value[0], value[1]])
        else:
            result.append([field, "=", value])
    return result


class FixtureService:
    """
    Business logic for bench_execute's fixture-export action.

    Each @staticmethod implements one action and takes the raw arguments
    dict passed to BenchExecute.execute(). bench_execute.py's execute()
    calls straight into these and returns their result directly — no
    try/except here or at the call site, so ValidationError/PermissionError
    propagate uncaught to base_tool.py's error handling.
    """

    @staticmethod
    def export_fixtures(arguments):
        assert_required(arguments.get("app_name"), _("app_name is required for export_fixtures."))
        assert_required(arguments.get("doctype"), _("doctype is required for export_fixtures."))
        assert_required(arguments.get("filters"), _("filters is required for export_fixtures."))

        app_name = arguments.get("app_name")
        doctype = arguments.get("doctype")
        filters = arguments.get("filters") or {}

        assert_not_protected_app(
            app_name,
            _("Cannot export fixtures to protected app '{0}'.").format(app_name),
        )

        app_path = get_app_path(app_name)

        assert_app_dir_exists(
            app_path,
            _("App '{0}' does not exist on this bench. Use create_app to create it first.").format(app_name),
        )

        if doctype not in ALLOWED_FIXTURE_DOCTYPES:
            frappe.throw(
                _("DocType '{0}' is not allowed as a fixture. Allowed: {1}").format(
                    doctype, ", ".join(sorted(ALLOWED_FIXTURE_DOCTYPES))
                ),
                frappe.ValidationError,
            )

        records = frappe.get_all(doctype, filters=filters, fields=["*"])

        if not records:
            return build_failure(f"No {doctype} records found with given filters.")

        full_records = []
        for record in records:
            doc = frappe.get_doc(doctype, record["name"])
            full_records.append(doc.as_dict())

        doctype_snake = frappe.scrub(doctype)
        fixture_dir = os.path.join(app_path, app_name, "fixtures")
        fixture_file = os.path.join(fixture_dir, f"{doctype_snake}.json")
        os.makedirs(fixture_dir, exist_ok=True)

        if os.path.exists(fixture_file):
            with open(  # nosemgrep: frappe-security-file-traversal — path validated by resolve_and_validate_path()
                fixture_file
            ) as f:
                existing_data = json.load(f)
            existing_names = {r["name"] for r in existing_data}
            new_records = [r for r in full_records if r["name"] not in existing_names]
            existing_data.extend(new_records)
            final_records = existing_data
        else:
            final_records = full_records

        with open(  # nosemgrep: frappe-security-file-traversal — path validated by resolve_and_validate_path()
            fixture_file, "w"
        ) as f:
            json.dump(final_records, f, indent=2, default=str)

        hooks_file = os.path.join(app_path, app_name, "hooks.py")
        hooks_updated = False

        if os.path.exists(hooks_file):
            with open(  # nosemgrep: frappe-security-file-traversal — path validated by resolve_and_validate_path()
                hooks_file
            ) as f:
                content = f.read()

            hooks_filters = _convert_filters_to_list(filters)
            new_entry = {"dt": doctype, "filters": hooks_filters}
            tree = ast.parse(content)
            fixtures_found = False

            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id == "fixtures":
                            fixtures_found = True
                            try:
                                existing_entries = ast.literal_eval(node.value)
                                existing_entry_index = None
                                for i, e in enumerate(existing_entries):
                                    if isinstance(e, dict) and (
                                        e.get("dt") == doctype or e.get("doctype") == doctype
                                    ):
                                        existing_entry_index = i
                                        break

                                if existing_entry_index is None:
                                    existing_entries.append(new_entry)
                                    hooks_updated = True
                                elif existing_entries[existing_entry_index] != new_entry:
                                    existing_entries[existing_entry_index] = new_entry
                                    hooks_updated = True

                                if hooks_updated:
                                    new_fixtures_str = f"fixtures = {json.dumps(existing_entries, indent=4)}"
                                    lines = content.split("\n")
                                    start_line = node.lineno - 1
                                    end_line = node.end_lineno
                                    new_lines = (
                                        lines[:start_line] + new_fixtures_str.split("\n") + lines[end_line:]
                                    )
                                    content = "\n".join(new_lines)
                            except Exception:
                                pass

            if not fixtures_found:
                new_fixtures_str = f"\nfixtures = {json.dumps([new_entry], indent=4)}\n"
                content += new_fixtures_str
                hooks_updated = True

            with open(  # nosemgrep: frappe-security-file-traversal — path validated by resolve_and_validate_path()
                hooks_file, "w"
            ) as f:
                f.write(content)

        return {
            "success": True,
            "app_name": app_name,
            "doctype": doctype,
            "records_exported": len(full_records),
            "fixture_file": os.path.join(app_name, app_name, "fixtures", f"{doctype_snake}.json"),
            "hooks_updated": hooks_updated,
            "message": f"{len(full_records)} {doctype} records exported to {app_name}",
        }
