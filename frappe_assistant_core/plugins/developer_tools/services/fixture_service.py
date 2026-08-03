# Copyright (C) 2025 Promantia
# Developer Tools Plugin — FixtureService

import ast
import json
import os
import threading

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


def _pending_export_marker_path(app_name):
    """
    Path to the marker signaling app_name's background fixture-file/hooks.py
    write (scheduled by export_fixtures) hasn't finished yet. Lives under
    logs/, never under apps/, so creating/removing it never touches this
    bench's watched-path reloader.
    """
    bench_path = frappe.utils.get_bench_path()
    marker_dir = os.path.join(bench_path, "logs", "fac_export_pending")
    return os.path.join(marker_dir, f"{app_name}.marker")


def _is_export_pending(app_name):
    return os.path.exists(_pending_export_marker_path(app_name))


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

        if _is_export_pending(app_name):
            return build_failure(
                f"A previous fixture export for '{app_name}' is still finishing in the "
                f"background. Wait a few seconds and retry.",
                export_in_progress=True,
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

        # Reading any existing fixture file here is safe (no watched-path write yet) —
        # it's needed to compute the merged content that gets written later, in the
        # background.
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

        fixture_content = json.dumps(final_records, indent=2, default=str)

        hooks_file = os.path.join(app_path, app_name, "hooks.py")
        hooks_updated = False
        hooks_update_warning = None
        new_hooks_content = None

        if not os.path.exists(hooks_file):
            hooks_update_warning = (
                f"hooks.py not found at '{hooks_file}'; the fixture was exported but not "
                f"registered, so a real 'bench migrate'/install will not pick it up."
            )
        else:
            # Reading + ast-parsing the existing hooks.py is safe (no write yet) — this
            # computes the new content entirely in memory; only the actual write below
            # is deferred.
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
                            except (ValueError, SyntaxError, TypeError) as e:
                                # ast.literal_eval raises these when the existing
                                # `fixtures = ...` value isn't a plain literal (e.g. a
                                # function call or variable) — leave hooks.py untouched
                                # rather than guess, but say so instead of silently
                                # reporting hooks_updated: false with no explanation.
                                hooks_update_warning = (
                                    f"Found an existing 'fixtures' assignment in hooks.py that "
                                    f"isn't a plain list literal, so it could not be updated "
                                    f"automatically ({e}). Add the fixture entry for '{doctype}' "
                                    f"to hooks.py manually."
                                )

            if not fixtures_found:
                new_fixtures_str = f"\nfixtures = {json.dumps([new_entry], indent=4)}\n"
                content += new_fixtures_str
                hooks_updated = True

            if hooks_updated:
                new_hooks_content = content

        # Everything above is DB reads (frappe.get_all/get_doc) or reads/pure computation
        # against files already on disk — no apps/ write has happened yet, so the result
        # below is already fully known. Only the actual writes are deferred: this bench's
        # dev server auto-restarts on any file change under apps/ (watchdog-based
        # reloader), and writing fixture_file/hooks_file synchronously here would risk
        # dropping this request's own HTTP response mid-flight even though the export
        # already succeeded — the exact pattern already fixed for remove_app.
        marker_path = _pending_export_marker_path(app_name)
        os.makedirs(os.path.dirname(marker_path), exist_ok=True)
        open(  # nosemgrep: frappe-security-file-traversal — path derived from validated app_name, not user input
            marker_path, "w"
        ).close()

        def _write_files():
            # Runs on a background Timer thread with no initialized frappe.local —
            # must stick to plain filesystem calls only, no frappe.* here.
            os.makedirs(fixture_dir, exist_ok=True)
            with open(  # nosemgrep: frappe-security-file-traversal — path derived from validated app_name/doctype, not user input
                fixture_file, "w"
            ) as f:
                f.write(fixture_content)
            if new_hooks_content is not None:
                with open(  # nosemgrep: frappe-security-file-traversal — path derived from validated app_name, not user input
                    hooks_file, "w"
                ) as f:
                    f.write(new_hooks_content)
            try:
                os.remove(marker_path)
            except OSError:
                pass

        # Delayed so this request's response has a chance to flush before the file
        # writes fire the reloader's watched-path restart. Reduces, but can't fully
        # guarantee against, that race — see note above.
        threading.Timer(1.5, _write_files).start()

        result = {
            "success": True,
            "app_name": app_name,
            "doctype": doctype,
            "records_exported": len(full_records),
            "fixture_file": os.path.join(app_name, app_name, "fixtures", f"{doctype_snake}.json"),
            "hooks_updated": hooks_updated,
            "file_write": "in_progress",
            "message": (
                f"{len(full_records)} {doctype} record(s) will be written to "
                f"fixtures/{doctype_snake}.json in the background"
                + (" and hooks.py updated" if hooks_updated else "")
                + ". This may cause a brief dev-server restart in the next couple of "
                "seconds, which is expected and not a failure."
            ),
        }
        if hooks_update_warning is not None:
            result["hooks_update_warning"] = hooks_update_warning
        return result
