# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Opening stock (#33996): the old system's closing stock becomes an Opening Stock Stock
Reconciliation at the cut-off date.

Stock Reconciliation has Allow Import off, and one holds every row of the file, so Data Import
isn't used. The session, the file reading, the grouped dry run, the fixes and the results file
are the import's own; this module adds the setup, the row checks, the creation and the report.
An opening-stock session keeps its settings in ``template_options.opening_stock``.
"""

import re
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any, Dict, List, Optional

import erpnext
import frappe
from frappe import _
from frappe.utils import cstr, getdate

from frappe_assistant_core.plugins.core.import_mapping import save_mapping, session_headers
from frappe_assistant_core.plugins.core.import_session import read_sheet_rows, readable_session_file

TARGET = "Stock Reconciliation"
ITEM_TABLE = "Stock Reconciliation Item"
PURPOSE = "Opening Stock"
DEFAULT_TIME = "23:59:59"
CHUNK_ROWS = 1000
ITEM_FIELDS = ("item_code", "warehouse", "qty", "valuation_rate", "amount", "batch_no", "serial_no")
EXTRA_FIELDS = ("uom", "expiry_date")  # read by this module; not Stock Reconciliation Item fields
REQUIRED_FIELDS = ("item_code", "warehouse", "qty")


class OpeningStockProblem(Exception):
    """Opening stock can't be set up, checked or created. The message is shown to the user."""


def settings(session) -> Dict[str, Any]:
    options = frappe.parse_json(session.template_options or "{}") or {}
    return dict(options.get("opening_stock") or {})


def is_opening_stock(session) -> bool:
    return bool(settings(session))


def setup_problem(company: str, posting_date: str, difference_account: Optional[str]) -> Optional[str]:
    """Why opening stock can't be set up with these values, or None."""
    if not frappe.has_permission(TARGET, "create") or not frappe.has_permission(TARGET, "submit"):
        return _(
            "Opening stock is created and submitted as a Stock Reconciliation, and you can't do both. "
            "A Stock Manager can import it."
        )
    if not company or not frappe.db.exists("Company", company):
        return _("Company {0} doesn't exist.").format(company)
    try:
        date = getdate(posting_date)
    except Exception:
        return _("'{0}' isn't a date. Give the cut-off date as YYYY-MM-DD.").format(posting_date)
    from erpnext.accounts.utils import FiscalYearError, get_fiscal_year

    try:
        get_fiscal_year(date, company=company)
    except FiscalYearError:
        return _("{0} isn't in any fiscal year of {1}. Create the fiscal year first.").format(date, company)
    if not difference_account:
        return _(
            "{0} has no Temporary Opening account. Name the Balance Sheet account the opening stock "
            "is posted against, as difference_account."
        ).format(company)
    account = frappe.db.get_value(
        "Account", difference_account, ["company", "is_group", "report_type"], as_dict=True
    )
    if not account or account.company != company or account.is_group:
        return _("{0} isn't an account of {1} that entries can be posted to.").format(
            difference_account, company
        )
    if erpnext.is_perpetual_inventory_enabled(company) and account.report_type != "Balance Sheet":
        return _(
            "Opening stock is posted against a Balance Sheet account, such as Temporary Opening; "
            "{0} is a Profit and Loss account."
        ).format(difference_account)
    return None


def default_difference_account(company: str) -> Optional[str]:
    return frappe.db.get_value("Account", {"company": company, "account_type": "Temporary", "is_group": 0})


def setup(
    session,
    company: str,
    posting_date: str,
    columns: Dict[str, str],
    posting_time: Optional[str] = None,
    difference_account: Optional[str] = None,
) -> Dict[str, Any]:
    """Store the company, cut-off, account and columns on the session, which becomes Mapped.
    Raises OpeningStockProblem, listing every wrong column at once."""
    if session.status not in ("File Read", "Mapped", "Dry Run Failed", "Ready to Import"):
        raise OpeningStockProblem(
            _("This session is {0}, so it can't be set up again.").format(_(session.status))
        )
    difference_account = difference_account or default_difference_account(company)
    problem = setup_problem(company, posting_date, difference_account)
    if problem:
        raise OpeningStockProblem(problem)

    headers = session_headers(session)
    mapping, extra, problems = {}, {}, []
    for column, field in (columns or {}).items():
        if column not in headers:
            problems.append(_("There is no column '{0}'. Columns: {1}.").format(column, ", ".join(headers)))
        elif field not in ITEM_FIELDS + EXTRA_FIELDS:
            problems.append(
                _("'{0}' can't be mapped. Map columns to: {1}.").format(
                    field, ", ".join(ITEM_FIELDS + EXTRA_FIELDS)
                )
            )
        elif field in ITEM_FIELDS:
            mapping[str(headers.index(column))] = f"items.{field}"
        else:
            extra[field] = headers.index(column)
    given = {target.split(".", 1)[1] for target in mapping.values()}
    missing = [field for field in REQUIRED_FIELDS if field not in given]
    if missing:
        problems.append(_("Map a column to each of: {0}.").format(", ".join(missing)))
    if "valuation_rate" not in given and "amount" not in given:
        problems.append(_("Map a column to valuation_rate or to amount (the stock value)."))
    if problems:
        raise OpeningStockProblem(" ".join(problems))
    for position in range(len(headers)):
        mapping.setdefault(str(position), "Don't Import")

    stock = {
        "company": company,
        "posting_date": str(getdate(posting_date)),
        "posting_time": posting_time or DEFAULT_TIME,
        "difference_account": difference_account,
        "perpetual": bool(erpnext.is_perpetual_inventory_enabled(company)),
        "columns": extra,
    }
    options = frappe.parse_json(session.template_options or "{}") or {}
    options["opening_stock"] = stock
    session.template_options = frappe.as_json(options)
    session.import_type = "Insert New Records"
    save_mapping(
        session,
        TARGET,
        mapping,
        _("Opening stock for {0} at {1} {2}, against {3}.").format(
            company, stock["posting_date"], stock["posting_time"], difference_account
        ),
    )
    return {"opening_stock": stock, "column_to_field_map": mapping}


# -- reading the file ------------------------------------------------------------------------

TOTALS = re.compile(r"^\s*(grand\s*|sub\s*-?\s*)?totals?\s*:?\s*$", re.IGNORECASE)


@dataclass
class Line:
    row: int
    cells: Dict[str, str] = dataclass_field(default_factory=dict)


def number(text) -> Optional[float]:
    """A number as accountants write it: '1,250.50', '(12)' for -12. None when blank."""
    text = cstr(text).strip().replace(",", "").replace(" ", "")
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    value = float(text.strip("()"))
    return -value if negative else value


def is_totals_row(cells: Dict[str, str]) -> bool:
    item = cstr(cells.get("item_code")).strip()
    has_number = any(cstr(cells.get(f)).strip() for f in ("qty", "valuation_rate", "amount"))
    return has_number and (not item or bool(TOTALS.match(item)))


def read_lines(session) -> List[Line]:
    """Every non-blank row of the session's sheet, as field → text, with value maps applied."""
    from frappe_assistant_core.plugins.core.import_dry_run import column_to_field_map
    from frappe_assistant_core.plugins.core.import_fixes import apply_value_maps

    rows = apply_value_maps(session, read_sheet_rows(readable_session_file(session), session.sheet or None))
    positions = {
        int(position): target.split(".", 1)[1]
        for position, target in column_to_field_map(session).items()
        if str(position).isdigit() and target.startswith("items.")
    }
    positions.update({int(p): f for f, p in (settings(session).get("columns") or {}).items()})
    lines = []
    for index, row in enumerate(rows[1:], start=2):
        cells = {f: cstr(row[p]).strip() if p < len(row) else "" for p, f in positions.items()}
        if any(cells.values()):
            lines.append(Line(index, cells))
    return lines
