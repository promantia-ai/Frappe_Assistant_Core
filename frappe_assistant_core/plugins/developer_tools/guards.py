# Copyright (C) 2025 Promantia
# Developer Tools Plugin — Shared security/validation guards

import os
import re

import frappe
from frappe import _

PROTECTED_APPS = {
    "frappe",
    "frappe_assistant_core",
    "erpnext",
    "hrms",
    "payments",
    "india_compliance",
    "lending",
    "education",
}


DEV_MODE_REQUIRED_TOOLS = {"bench_execute", "write_file"}

_APP_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

ALLOWED_TEXT_EXTENSIONS = {".py", ".js", ".json", ".html", ".css", ".txt", ".md"}


def assert_developer_mode():
    """
    Raises frappe.PermissionError if developer_mode is not enabled in site_config.json.
    Call this as the first line in execute() for tools that modify the bench
    filesystem or run bench operations (bench_execute, write_file).
    """
    if not frappe.conf.get("developer_mode"):
        frappe.throw(
            _(
                "This tool requires developer_mode=1 in site_config.json. "
                "It is not available on production sites."
            ),
            frappe.PermissionError,
        )


def assert_system_manager():
    """
    Raises frappe.PermissionError if current user
    does not have System Manager role.
    Call this as the first line in every tool's execute().
    """
    user = frappe.session.user

    if user == "Guest":
        frappe.throw(_("Guest users are not allowed to use Developer Tools."), frappe.PermissionError)

    if "System Manager" not in frappe.get_roles(user):
        frappe.throw(
            _(
                "User {0} does not have System Manager role. "
                "System Manager is required for all Developer Tools."
            ).format(user),
            frappe.PermissionError,
        )


def resolve_and_validate_path(relative_path):
    """
    Resolves a relative path against the bench apps/ directory.
    Validates:
      - No null bytes
      - Max 10 directory levels deep
      - No path traversal (../../)
      - No symlink escapes outside apps/
    Returns the resolved absolute path if valid.
    Raises frappe.ValidationError if invalid.
    """
    # Null byte check
    if "\x00" in relative_path:
        frappe.throw(_("Invalid path: null bytes are not allowed."), frappe.ValidationError)

    # Depth check
    parts = [p for p in relative_path.replace("\\", "/").split("/") if p]
    if len(parts) > 15:
        frappe.throw(
            _("Invalid path: too many directory levels ({0}). Max is 15.").format(len(parts)),
            frappe.ValidationError,
        )

    # Build full path
    apps_path = get_apps_path()
    full_path = os.path.join(apps_path, relative_path)

    # Resolve symlinks and traversal
    real_path = os.path.realpath(full_path)

    # Boundary check
    if not real_path.startswith(apps_path + os.sep) and real_path != apps_path:
        frappe.throw(
            _(
                "Invalid path: resolves outside the apps/ directory. "
                "Path traversal and symlink escapes are not allowed."
            ),
            frappe.ValidationError,
        )

    return real_path


def assert_valid_app_name(app_name):
    """
    Raises frappe.ValidationError if app_name does not match ^[a-z][a-z0-9_]*$.
    """
    if not _APP_NAME_RE.match(app_name):
        frappe.throw(
            _(
                "Invalid app_name '{0}'. Must match ^[a-z][a-z0-9_]*$ "
                "(lowercase letters, digits, underscores; must start with a letter)."
            ).format(app_name),
            frappe.ValidationError,
        )


def assert_required(value, message):
    """
    Raises frappe.ValidationError with the given message if value is falsy.
    """
    if not value:
        frappe.throw(message, frappe.ValidationError)


def assert_not_protected_app(app_name, message):
    """
    Raises frappe.PermissionError with the given message if app_name is in PROTECTED_APPS.
    """
    if app_name in PROTECTED_APPS:
        frappe.throw(message, frappe.PermissionError)


def get_apps_path():
    """
    Returns the absolute path to the bench's apps/ directory.
    """
    bench_path = frappe.utils.get_bench_path()
    return os.path.join(bench_path, "apps")


def get_app_path(app_name):
    """
    Returns the absolute path to app_name's directory under the bench's apps/ directory.
    """
    return os.path.join(get_apps_path(), app_name)


def assert_app_dir_exists(app_path, message):
    """
    Raises frappe.ValidationError with the given message if app_path is not a directory.
    """
    if not os.path.isdir(app_path):
        frappe.throw(message, frappe.ValidationError)


def assert_allowed_extension(ext, message):
    """
    Raises frappe.ValidationError with the given message if ext is not in ALLOWED_TEXT_EXTENSIONS.
    """
    if ext not in ALLOWED_TEXT_EXTENSIONS:
        frappe.throw(message, frappe.ValidationError)


def assert_within_size_limit(size_bytes, max_bytes, message):
    """
    Raises frappe.ValidationError with the given message if size_bytes exceeds max_bytes.
    """
    if size_bytes > max_bytes:
        frappe.throw(message, frappe.ValidationError)


def build_failure(message, **extra_keys):
    """
    Returns {"success": False, "error": message, "message": message, **extra_keys}.
    Use for soft (non-exception) tool failures so base_tool.py's outer response["error"]
    and the nested response["result"]["message"] both carry the real failure text.
    """
    return {"success": False, "error": message, "message": message, **extra_keys}
