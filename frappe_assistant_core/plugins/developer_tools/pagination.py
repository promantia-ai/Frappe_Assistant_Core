# Copyright (C) 2025 Promantia
# Developer Tools Plugin — Shared pagination/coercion helpers

import os

import frappe


def safe_getsize(path):
    """
    Returns os.path.getsize(path), or 0 if the path cannot be stat'd.
    """
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def coerce_int_in_range(value, min_value, max_value, type_error_message, range_error_message):
    """
    Coerces value to int, raising frappe.ValidationError with type_error_message if it is not
    convertible, then raising frappe.ValidationError with range_error_message (formatted with
    the coerced value, if it contains a placeholder) if the coerced value falls outside
    [min_value, max_value]. Returns the coerced int.
    """
    try:
        value = int(value)
    except (TypeError, ValueError):
        frappe.throw(type_error_message, frappe.ValidationError)

    if value < min_value or value > max_value:
        frappe.throw(range_error_message.format(value), frappe.ValidationError)

    return value


def coerce_offset(value):
    """
    Coerces value to a non-negative int, defaulting to 0 if it is not convertible or negative.
    """
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = 0
    if value < 0:
        value = 0
    return value


def paginate(items, offset, limit):
    """
    Slices items[offset : offset + limit] and reports whether more remain.
    Returns (page, total, truncated).
    """
    total = len(items)
    page = items[offset : offset + limit]
    truncated = (offset + len(page)) < total
    return page, total, truncated
