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
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import erpnext
import frappe
from frappe import _
from frappe.utils import cstr, flt, getdate

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


# -- checking the rows -----------------------------------------------------------------------

CAUSE_TOTALS_ROW = "Totals row"
CAUSE_NOT_STOCK = "Can't hold stock"
CAUSE_OTHER_COMPANY = "Warehouse of another company"
CAUSE_GROUP_WAREHOUSE = "Group warehouse"
CAUSE_NO_CONVERSION = "No UOM conversion"
CAUSE_NEGATIVE = "Negative quantity"
CAUSE_ZERO = "Zero quantity"
CAUSE_NO_RATE = "No valuation rate"
CAUSE_NEEDS_BATCH = "Batch needed"
CAUSE_NEEDS_SERIAL = "Serial numbers needed"
CAUSE_SERIAL_COUNT = "Serial numbers don't match the quantity"
CAUSE_SAME_ITEM = "Same item and warehouse"
CAUSE_HAS_STOCK = "Already has stock"
NEW_CAUSES = (
    CAUSE_TOTALS_ROW,
    CAUSE_NOT_STOCK,
    CAUSE_OTHER_COMPANY,
    CAUSE_GROUP_WAREHOUSE,
    CAUSE_NO_CONVERSION,
    CAUSE_NEGATIVE,
    CAUSE_ZERO,
    CAUSE_NO_RATE,
    CAUSE_NEEDS_BATCH,
    CAUSE_NEEDS_SERIAL,
    CAUSE_SERIAL_COUNT,
    CAUSE_SAME_ITEM,
    CAUSE_HAS_STOCK,
)
# Failures are one group per cause, so "4 rows can't hold stock" is one decision. These are one
# group per value, because the fix depends on the value: a name to map, an item to allow.
BY_VALUE = (CAUSE_NO_RATE, CAUSE_OTHER_COMPANY, CAUSE_GROUP_WAREHOUSE)
LABELS = {
    "item_code": "Item Code",
    "warehouse": "Warehouse",
    "qty": "Quantity",
    "valuation_rate": "Valuation Rate",
    "amount": "Amount",
}


@dataclass
class Ready:
    row: int
    item_code: str
    warehouse: str
    qty: float
    valuation_rate: float
    batch_no: str = ""
    serial_no: str = ""
    expiry_date: str = ""


def check_can_run(session) -> None:
    from frappe_assistant_core.plugins.core import import_dry_run as dry_run

    dry_run.check_status(session)
    stock = settings(session)
    problem = setup_problem(stock.get("company"), stock.get("posting_date"), stock.get("difference_account"))
    if problem:
        raise dry_run.DryRunProblem(problem)
    if not session.source_file:
        raise dry_run.DryRunProblem(
            _("The file {0} is only in the chat. Upload it in File Manager and pass its file_id.").format(
                session.file_name
            )
        )


class _StockChecker:
    """Groups failures the dry run's way, so its results file, fixes and reply are reused."""

    def __init__(self):
        from frappe_assistant_core.plugins.core import import_dry_run as dry_run

        self.doctype = TARGET
        self.key_df = None
        self.groups: Dict[tuple, Dict[str, Any]] = {}
        self.rows: List[Dict[str, Any]] = []
        self._group_of = dry_run._Checker._group
        self._id_of = dry_run._Checker._group_id

    def _group_id(self, key):
        return self._id_of(self, key)

    def fail(self, row: int, key: tuple, info: Dict[str, Any]) -> None:
        payload = SimpleNamespace(rows=[SimpleNamespace(row_number=row)])
        group_id = self._group_of(self, key, info, payload)
        self.rows.append({"row": row, "status": "failed", "groups": [group_id]})

    def mark(self, row: int, status: str) -> None:
        self.rows.append({"row": row, "status": status})


def _problem(cause: str, field: str, value: str, message: str):
    from frappe_assistant_core.plugins.core import import_dry_run as dry_run

    by_value = cause in BY_VALUE or cause == dry_run.CAUSE_MISSING_LINK
    key = (cause, ITEM_TABLE, field) + ((value,) if by_value else ())
    info = {
        "cause": cause,
        "parent": ITEM_TABLE,
        "field": field,
        "label": _(LABELS[field]),
        "value": value if by_value else None,
        "message": message,
    }
    return key, info


def _warehouse(name: str, company: str):
    """The warehouse named ``name``, or the only one of ``company`` whose warehouse_name it is."""
    fields = ["name", "company", "is_group", "disabled"]
    found = frappe.db.get_value("Warehouse", name, fields, as_dict=True)
    if found:
        return found
    matches = frappe.get_all("Warehouse", filters={"warehouse_name": name, "company": company}, fields=fields)
    return matches[0] if len(matches) == 1 else None


def _conversion(item_code: str, uom: str) -> Optional[float]:
    return frappe.db.get_value(
        "UOM Conversion Detail", {"parent": item_code, "uom": uom}, "conversion_factor"
    )


def _has_stock(item_code: str, warehouse: str, posting_date: str) -> bool:
    if flt(frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty")):
        return True
    return bool(
        frappe.db.exists(
            "Stock Ledger Entry",
            {
                "item_code": item_code,
                "warehouse": warehouse,
                "posting_date": [">", posting_date],
                "is_cancelled": 0,
            },
        )
    )


def _line_problem(line: Line, stock: Dict[str, Any], allowed_zero: set):
    """The line's problem as (key, info), or the Ready line."""
    from frappe_assistant_core.plugins.core import import_dry_run as dry_run

    cells = line.cells
    if is_totals_row(cells):
        return (CAUSE_TOTALS_ROW,), {
            "cause": CAUSE_TOTALS_ROW,
            "message": _("A totals row, not an item."),
        }
    values = {}
    for name in ("qty", "valuation_rate", "amount"):
        try:
            values[name] = number(cells.get(name))
        except ValueError:
            return _problem(
                dry_run.CAUSE_BAD_VALUE, name, cells[name], _("'{0}' isn't a number.").format(cells[name])
            )

    code = cells.get("item_code", "")
    item = frappe.db.get_value(
        "Item",
        code,
        [
            "name",
            "disabled",
            "has_variants",
            "is_stock_item",
            "is_fixed_asset",
            "has_batch_no",
            "has_serial_no",
            "stock_uom",
        ],
        as_dict=True,
    )
    if not item:
        return _problem(
            dry_run.CAUSE_MISSING_LINK, "item_code", code, _("Item '{0}' doesn't exist.").format(code)
        )
    if item.disabled or item.has_variants or not item.is_stock_item or item.is_fixed_asset:
        return _problem(
            CAUSE_NOT_STOCK,
            "item_code",
            code,
            _("The item is disabled, a template, a fixed asset or not a stock item."),
        )

    given = cells.get("warehouse", "")
    warehouse = _warehouse(given, stock["company"])
    if not warehouse:
        return _problem(
            dry_run.CAUSE_MISSING_LINK, "warehouse", given, _("Warehouse '{0}' doesn't exist.").format(given)
        )
    if warehouse.company != stock["company"]:
        return _problem(
            CAUSE_OTHER_COMPANY,
            "warehouse",
            given,
            _("{0} belongs to {1}, not {2}.").format(warehouse.name, warehouse.company, stock["company"]),
        )
    if warehouse.is_group or warehouse.disabled:
        return _problem(
            CAUSE_GROUP_WAREHOUSE,
            "warehouse",
            given,
            _("{0} is a group or disabled, so it can't hold stock.").format(warehouse.name),
        )

    qty, rate, amount = values["qty"] or 0, values["valuation_rate"], values["amount"]
    uom = cells.get("uom", "")
    if uom and uom != item.stock_uom:
        factor = _conversion(item.name, uom)
        if not factor:
            return _problem(
                CAUSE_NO_CONVERSION,
                "item_code",
                code,
                _("The item has no conversion from the file's unit to its stock unit."),
            )
        qty, rate = qty * factor, (rate / factor if rate else rate)
    if qty < 0:
        return _problem(CAUSE_NEGATIVE, "qty", cells.get("qty", ""), _("Opening stock can't be negative."))
    if qty == 0:
        return _problem(CAUSE_ZERO, "qty", cells.get("qty", ""), _("There is no stock to bring in."))
    if not rate and amount:
        rate = amount / qty
    if not rate and item.name not in allowed_zero:
        return _problem(
            CAUSE_NO_RATE,
            "item_code",
            code,
            _("{0} has no valuation rate, so it would be valued at zero.").format(code),
        )

    batch = cells.get("batch_no", "")
    serials = [s for s in re.split(r"[\s,]+", cells.get("serial_no", "")) if s]
    if item.has_batch_no and not batch:
        return _problem(CAUSE_NEEDS_BATCH, "item_code", code, _("The item is tracked by batch."))
    if item.has_serial_no and not serials:
        return _problem(CAUSE_NEEDS_SERIAL, "item_code", code, _("The item is tracked by serial number."))
    if item.has_serial_no and len(serials) != qty:
        return _problem(
            CAUSE_SERIAL_COUNT,
            "item_code",
            code,
            _("The number of serial numbers differs from the quantity."),
        )
    if _has_stock(item.name, warehouse.name, stock["posting_date"]):
        return _problem(
            CAUSE_HAS_STOCK,
            "item_code",
            code,
            _("The item already has stock, or stock moves after the cut-off, in this warehouse."),
        )
    return Ready(
        line.row,
        item.name,
        warehouse.name,
        qty,
        flt(rate),
        batch,
        "\n".join(serials),
        cells.get("expiry_date", ""),
    )


def _allowed_zero(session) -> set:
    rules = frappe.parse_json(session.transformation_rules or "{}") or {}
    return set(rules.get("zero_rate_items") or [])


def _check_lines(session, checker: _StockChecker) -> List[Ready]:
    from frappe_assistant_core.plugins.core import import_dry_run as dry_run

    stock, excluded, allowed = settings(session), dry_run._excluded_rows(session), _allowed_zero(session)
    ready, seen = [], {}
    for line in read_lines(session):
        if line.row in excluded:
            checker.mark(line.row, "excluded")
            continue
        outcome = _line_problem(line, stock, allowed)
        if not isinstance(outcome, Ready):
            checker.fail(line.row, *outcome)
            continue
        key = (outcome.item_code, outcome.warehouse, outcome.batch_no)
        if key in seen:
            checker.fail(
                line.row,
                *_problem(
                    CAUSE_SAME_ITEM,
                    "item_code",
                    outcome.item_code,
                    _("The same item is in this warehouse in row {0} too.").format(seen[key]),
                ),
            )
            continue
        seen[key] = line.row
        ready.append(outcome)
    return ready


def _totals(ready: List[Ready]) -> Dict[str, Any]:
    warehouses: Dict[str, Dict[str, Any]] = {}
    for line in ready:
        entry = warehouses.setdefault(line.warehouse, {"qty": 0, "value": 0, "rows": 0})
        entry["qty"] += line.qty
        entry["value"] += line.qty * line.valuation_rate
        entry["rows"] += 1
    precision = frappe.get_precision(ITEM_TABLE, "amount") or 2
    for entry in warehouses.values():
        entry["qty"], entry["value"] = flt(entry["qty"], 6), flt(entry["value"], precision)
    return {
        "total_qty": flt(sum(e["qty"] for e in warehouses.values()), 6),
        "total_value": flt(sum(e["value"] for e in warehouses.values()), precision),
        "warehouses": warehouses,
    }


def run(session_name: str, commit_progress: bool = False) -> Dict[str, Any]:
    """The dry run of an opening-stock session: every row checked, ERPNext's own validation of
    the ready rows, the result stored the dry run's way, with the totals."""
    from frappe_assistant_core.plugins.core import import_dry_run as dry_run

    session = frappe.get_doc(dry_run.SESSION_DOCTYPE, session_name)
    file_doc = readable_session_file(session)
    checker = _StockChecker()
    ready = _check_lines(session, checker)
    ready = _erpnext_check(session, ready, checker)
    for line in ready:
        checker.mark(line.row, "ready")
    checker.rows.sort(key=lambda r: r["row"])
    summary = dry_run._store(session_name, file_doc, checker, len(checker.rows))
    summary["opening_stock"] = _totals(ready)
    frappe.db.set_value(
        dry_run.SESSION_DOCTYPE,
        session_name,
        "validation_summary",
        frappe.as_json(summary),
        update_modified=False,
    )
    return summary


def ready_lines(session) -> List[Ready]:
    """The lines that pass the row checks now."""
    return _check_lines(session, _StockChecker())


def _erpnext_check(session, ready: List[Ready], checker: _StockChecker) -> List[Ready]:
    return ready  # ERPNext's own validation of the draft is added with its test, in the next task
