# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
The go-live readiness check (#34424): every item a site needs before the client goes live, each
Pass, Warn or Fail with the fix. It is the ERPNext report "Go-Live Readiness", so FACO runs it with
generate_report, makes the PDF for the client to sign with generate_document, and it opens in Desk
too. Fail means a transaction will fail or post wrongly on the first day; Warn means it works but
will bite later. Every check is made here, not by the model, and nothing is written.
"""

import glob
import os
from typing import Any, Callable, Dict, List, Optional

import frappe
from frappe import _
from frappe.model.naming import NamingSeries, parse_naming_series
from frappe.permissions import AUTOMATIC_ROLES
from frappe.utils import add_days, cint, cstr, date_diff, escape_html, getdate, now_datetime, nowdate

PASS, WARN, FAIL = "Pass", "Warn", "Fail"
STATUSES = (FAIL, WARN, PASS)

#: Days before the financial year ends from which the next one should exist
NEXT_YEAR_DAYS = 45
#: A backup older than this many days is a warning
BACKUP_DAYS = 2
#: How many names a check lists before it says "and N more"
SHOWN = 10

#: The documents of the first day's work: their numbering, and their print format
NUMBERED = (
    "Quotation",
    "Sales Order",
    "Delivery Note",
    "Sales Invoice",
    "Material Request",
    "Purchase Order",
    "Purchase Receipt",
    "Purchase Invoice",
    "Payment Entry",
    "Journal Entry",
)
PRINTED = ("Quotation", "Sales Order", "Delivery Note", "Sales Invoice", "Purchase Order")


def _row(area: str, check: str, status: str, detail: str, fix: str = "", company: str = "") -> Dict:
    return {
        "area": area,
        "check": check,
        "status": status,
        "detail": detail,
        "fix": fix if status != PASS else "",
        "company": company,
    }


def _names(names: List[str]) -> str:
    """'A, B, C' or 'A, B, C and 4 more'."""
    shown = ", ".join(names[:SHOWN])
    more = len(names) - SHOWN
    return shown + (" " + _("and {0} more").format(more) if more > 0 else "")


def _uses_stock() -> bool:
    return bool(frappe.db.exists("Item", {"is_stock_item": 1, "disabled": 0}))


def _has_india_compliance() -> bool:
    return "india_compliance" in frappe.get_installed_apps()


# -- company defaults -----------------------------------------------------------------------


def _account_problems(company: str, accounts: Dict[str, Optional[str]]) -> List[str]:
    """Why each set account can't be posted to: a group, disabled, or another company's."""
    named = [a for a in accounts.values() if a]
    found = (
        {
            a.name: a
            for a in frappe.get_all(
                "Account", filters={"name": ["in", named]}, fields=["name", "is_group", "disabled", "company"]
            )
        }
        if named
        else {}
    )
    problems = []
    for label, account in accounts.items():
        if not account:
            continue
        row = found.get(account)
        if not row:
            problems.append(_("{0} ({1}) doesn't exist").format(label, account))
        elif row.is_group:
            problems.append(_("{0} ({1}) is a group, which can't be posted to").format(label, account))
        elif row.disabled:
            problems.append(_("{0} ({1}) is disabled").format(label, account))
        elif row.company != company:
            problems.append(_("{0} ({1}) belongs to {2}").format(label, account, row.company))
    return problems


def _defaults_check(company: Dict, check: str, fields: Dict[str, str], missing_status: str, why: str):
    """One row for a group of the company's default accounts: Fail when one can't be posted to,
    ``missing_status`` when one is empty."""
    accounts = {label: company.get(field) for field, label in fields.items()}
    empty = [label for label, account in accounts.items() if not account]
    broken = _account_problems(company.name, accounts)
    fix = _("Set them on the Company {0} (Accounts tab).").format(company.name)
    if broken:
        return _row(_("Company defaults"), check, FAIL, "; ".join(broken) + ".", fix, company.name)
    if empty:
        detail = _("Not set: {0}. {1}").format(", ".join(empty), why)
        return _row(_("Company defaults"), check, missing_status, detail, fix, company.name)
    return _row(_("Company defaults"), check, PASS, ", ".join(a for a in accounts.values()), "", company.name)


def company_defaults(company: Dict) -> List[Dict]:
    rows = [
        _defaults_check(
            company,
            _("Receivable and payable accounts"),
            {
                "default_receivable_account": _("Default Receivable Account"),
                "default_payable_account": _("Default Payable Account"),
            },
            FAIL,
            _("Invoices fail unless every customer and supplier has their own account."),
        ),
        _defaults_check(
            company,
            _("Round off account"),
            {"round_off_account": _("Round Off Account")},
            FAIL,
            _("Any invoice with rounding fails to post."),
        ),
        _defaults_check(
            company,
            _("Income and expense accounts"),
            {
                "default_income_account": _("Default Income Account"),
                "default_expense_account": _("Default Cost of Goods Sold Account"),
            },
            WARN,
            _("Every item then needs its own account."),
        ),
    ]
    if not company.round_off_cost_center or not company.cost_center:
        rows.append(
            _row(
                _("Company defaults"),
                _("Cost centers"),
                FAIL if not company.round_off_cost_center else WARN,
                _("The default cost center or the round off cost center is not set."),
                _("Set them on the Company {0} (Accounts tab).").format(company.name),
                company.name,
            )
        )
    else:
        rows.append(
            _row(_("Company defaults"), _("Cost centers"), PASS, company.cost_center, "", company.name)
        )
    if cint(company.enable_perpetual_inventory):
        rows.append(
            _defaults_check(
                company,
                _("Stock accounts"),
                {
                    "stock_adjustment_account": _("Stock Adjustment Account"),
                    "stock_received_but_not_billed": _("Stock Received But Not Billed"),
                },
                FAIL,
                _("With perpetual inventory on, stock transactions fail to post."),
            )
        )
    return rows


# -- fiscal year ----------------------------------------------------------------------------


def _years_of(company: str) -> List[Dict]:
    """The enabled fiscal years that apply to ``company``: an empty companies table is every company."""
    years = frappe.get_all(
        "Fiscal Year", filters={"disabled": 0}, fields=["name", "year_start_date", "year_end_date"]
    )
    links = frappe.get_all(
        "Fiscal Year Company", filters={"parenttype": "Fiscal Year"}, fields=["parent", "company"]
    )
    named: Dict[str, set] = {}
    for link in links:
        named.setdefault(link.parent, set()).add(link.company)
    return [y for y in years if not named.get(y.name) or company in named[y.name]]


def fiscal_year(company: str) -> List[Dict]:
    today = getdate(nowdate())
    years = _years_of(company)
    current = next((y for y in years if y.year_start_date <= today <= y.year_end_date), None)
    area, check = _("Fiscal year"), _("Fiscal year covers today")
    if not current:
        return [
            _row(
                area,
                check,
                FAIL,
                _("No fiscal year covers today: every transaction fails with 'Fiscal Year not found'."),
                _("Create the Fiscal Year for this year."),
                company,
            )
        ]
    rows = [_row(area, check, PASS, current.name, "", company)]
    if date_diff(current.year_end_date, today) <= NEXT_YEAR_DAYS:
        after = add_days(current.year_end_date, 1)
        if not any(y.year_start_date <= getdate(after) <= y.year_end_date for y in years):
            rows.append(
                _row(
                    area,
                    _("Next fiscal year"),
                    WARN,
                    _("{0} ends on {1} and the next year doesn't exist yet.").format(
                        current.name, current.year_end_date
                    ),
                    _("Create the next Fiscal Year before {0}.").format(current.year_end_date),
                    company,
                )
            )
    return rows


# -- taxes ----------------------------------------------------------------------------------


def taxes(company: str) -> List[Dict]:
    area = _("Taxes")
    india = _has_india_compliance()
    fix = (
        _("India Compliance creates the GST templates: check GST Settings for this company.")
        if india
        else _("Create a {0} for this company, and mark the usual one as default.")
    )
    rows = []
    templates = {}
    for doctype, check in (
        ("Sales Taxes and Charges Template", _("Sales tax templates")),
        ("Purchase Taxes and Charges Template", _("Purchase tax templates")),
    ):
        found = frappe.get_all(
            doctype,
            filters={"company": company, "disabled": 0},
            fields=["name", "is_default", "tax_category"],
        )
        templates[doctype] = found
        if not found:
            rows.append(
                _row(
                    area,
                    check,
                    WARN,
                    _("No enabled template: invoices are made without taxes unless typed in."),
                    fix.format(_(doctype)),
                    company,
                )
            )
        elif not any(t.is_default for t in found):
            rows.append(
                _row(
                    area,
                    check,
                    WARN,
                    _("{0} templates, none of them the default: each invoice needs one picked.").format(
                        len(found)
                    ),
                    _("Mark the usual {0} as default.").format(_(doctype)),
                    company,
                )
            )
        else:
            rows.append(_row(area, check, PASS, _names([t.name for t in found]), "", company))

    categories = frappe.get_all("Tax Category", filters={"disabled": 0}, pluck="name")
    used = {t.tax_category for found in templates.values() for t in found if t.tax_category}
    unused = [c for c in categories if c not in used]
    if unused:
        rows.append(
            _row(
                area,
                _("Tax categories"),
                WARN,
                _("No tax template of this company uses {0}: a party in that category gets no taxes.").format(
                    _names(unused)
                ),
                _("Give each tax category a sales and a purchase template, or disable the category."),
                company,
            )
        )
    else:
        detail = _names(categories) if categories else _("No tax categories are used.")
        rows.append(_row(area, _("Tax categories"), PASS, detail, "", company))
    return rows


# -- naming series --------------------------------------------------------------------------


def _default_series(doctype: str) -> Optional[str]:
    field = frappe.get_meta(doctype).get_field("naming_series")
    if not field:
        return None
    options = [o.strip() for o in cstr(field.options).split("\n") if o.strip()]
    return field.default if field.default in options else (options[0] if options else None)


def _next_name(series: str) -> str:
    """The name the next document of ``series`` gets, read without moving the counter. Read without
    a lock too (NamingSeries.get_current_value takes one): a report must not hold up an invoice."""
    table = frappe.qb.DocType("Series")
    found = (
        frappe.qb.from_(table)
        .select(table.current)
        .where(table.name == NamingSeries(series).get_prefix())
        .run(pluck=True)
    )
    current = cint(found[0]) if found else 0
    return parse_naming_series(series, number_generator=lambda prefix, digits: str(current + 1).zfill(digits))


def naming_series() -> List[Dict]:
    """The next number of each day-one document mustn't be taken already: an import that kept the
    old system's numbers in the same series leaves the counter behind, and the first new document
    fails with 'Duplicate name'."""
    area, rows, taken, unknown = _("Naming series"), [], [], []
    for doctype in NUMBERED:
        series = _default_series(doctype)
        if not series:
            continue
        try:
            upcoming = _next_name(series)
        except Exception:
            # a series that needs the document ({company}, .FY.) can't be read without one
            unknown.append(doctype)
            continue
        if frappe.db.exists(doctype, upcoming):
            taken.append(_("{0}: next is {1}, which already exists").format(_(doctype), upcoming))
    if taken:
        rows.append(
            _row(
                area,
                _("Next numbers are free"),
                FAIL,
                "; ".join(taken) + ".",
                _(
                    "Move the series' current number past the last document (Document Naming Settings, "
                    "Update Series Counter)."
                ),
            )
        )
    else:
        checked = len(NUMBERED) - len(unknown)
        rows.append(
            _row(area, _("Next numbers are free"), PASS, _("{0} document types checked.").format(checked))
        )
    return rows


# -- stock ----------------------------------------------------------------------------------


def stock(company: Dict) -> List[Dict]:
    area, name, rows = _("Warehouses and stock"), company.name, []
    leaves = frappe.get_all(
        "Warehouse", filters={"company": name, "is_group": 0, "disabled": 0}, fields=["name", "account"]
    )
    if not leaves:
        rows.append(
            _row(
                area,
                _("Warehouses"),
                FAIL,
                _("The company has no warehouse to keep stock in."),
                _("Create a warehouse (not a group) for {0}.").format(name),
                name,
            )
        )
    else:
        rows.append(_row(area, _("Warehouses"), PASS, _names([w.name for w in leaves]), "", name))
        if cint(company.enable_perpetual_inventory) and not company.default_inventory_account:
            missing = [w.name for w in leaves if not w.account]
            if missing:
                rows.append(
                    _row(
                        area,
                        _("Warehouse accounts"),
                        FAIL,
                        _(
                            "No stock account for {0}, and the company has no default inventory account."
                        ).format(_names(missing)),
                        _(
                            "Set the Default Inventory Account on the Company, or an account on each warehouse."
                        ),
                        name,
                    )
                )
    if leaves:
        negative = frappe.get_all(
            "Bin",
            filters={"warehouse": ["in", [w.name for w in leaves]], "actual_qty": ["<", 0]},
            fields=["item_code", "warehouse", "actual_qty"],
            order_by="actual_qty asc",
        )
        if negative:
            rows.append(
                _row(
                    area,
                    _("No negative stock"),
                    FAIL,
                    _("{0} items are below zero: {1}.").format(
                        len(negative),
                        _names([f"{b.item_code} in {b.warehouse} ({b.actual_qty:g})" for b in negative]),
                    ),
                    _("Post the missing receipts or a Stock Reconciliation before go-live."),
                    name,
                )
            )
        else:
            rows.append(_row(area, _("No negative stock"), PASS, _("No item is below zero."), "", name))
    return rows


def stock_settings() -> List[Dict]:
    area, rows = _("Warehouses and stock"), []
    settings = frappe.get_cached_doc("Stock Settings")
    if not settings.valuation_method:
        rows.append(
            _row(
                area,
                _("Valuation method"),
                WARN,
                _("Not set; it can't be changed once stock is posted."),
                _("Choose FIFO or Moving Average in Stock Settings now."),
            )
        )
    else:
        rows.append(_row(area, _("Valuation method"), PASS, settings.valuation_method))
    if cint(settings.allow_negative_stock):
        rows.append(
            _row(
                area,
                _("Negative stock not allowed"),
                WARN,
                _("Stock may go below zero, the most common reason for wrong stock values."),
                _("Untick Allow Negative Stock in Stock Settings."),
            )
        )
    else:
        rows.append(_row(area, _("Negative stock not allowed"), PASS, _("Stock can't go below zero.")))
    return rows


# -- price lists, printing, email, scheduler, backups, users --------------------------------


def price_lists() -> List[Dict]:
    area, rows = _("Price lists"), []
    for settings, field, check in (
        ("Selling Settings", "selling_price_list", _("Default selling price list")),
        ("Buying Settings", "buying_price_list", _("Default buying price list")),
    ):
        name = frappe.db.get_single_value(settings, field)
        enabled = name and frappe.db.get_value("Price List", name, "enabled")
        if not name:
            rows.append(
                _row(
                    area,
                    check,
                    WARN,
                    _("Not set: every new document needs one picked."),
                    _("Set it in {0}.").format(_(settings)),
                )
            )
        elif not cint(enabled):
            rows.append(
                _row(
                    area,
                    check,
                    WARN,
                    _("{0} is disabled.").format(name),
                    _("Enable the Price List {0}, or pick another in {1}.").format(name, _(settings)),
                )
            )
        else:
            rows.append(_row(area, check, PASS, name))
    return rows


def printing(company: Dict) -> List[Dict]:
    area, rows = _("Print formats and letterhead"), []
    head = company.default_letter_head or frappe.db.get_value(
        "Letter Head", {"is_default": 1, "disabled": 0}, "name"
    )
    if head:
        rows.append(_row(area, _("Letterhead"), PASS, head, "", company.name))
    else:
        rows.append(
            _row(
                area,
                _("Letterhead"),
                WARN,
                _("No default letterhead: printed documents have no header."),
                _("Create a Letter Head and set it as the company's default."),
                company.name,
            )
        )
    return rows


def print_formats() -> List[Dict]:
    standard = [d for d in PRINTED if not frappe.get_meta(d).default_print_format]
    if standard:
        fix = _(
            "Set the client's print format as the default (Customize Form, Default Print Format), "
            "if they want their own."
        )
        return [
            _row(
                _("Print formats and letterhead"),
                _("Print formats"),
                WARN,
                _("{0} print with ERPNext's Standard format.").format(_names([_(d) for d in standard])),
                fix,
            )
        ]
    return [
        _row(
            _("Print formats and letterhead"),
            _("Print formats"),
            PASS,
            _names([f"{_(d)}: {frappe.get_meta(d).default_print_format}" for d in PRINTED]),
        )
    ]


def email_account() -> List[Dict]:
    name = frappe.db.get_value("Email Account", {"enable_outgoing": 1, "default_outgoing": 1}, "name")
    if name:
        return [_row(_("Email"), _("Outgoing email account"), PASS, name)]
    return [
        _row(
            _("Email"),
            _("Outgoing email account"),
            WARN,
            _("No default outgoing email account: invoices and notifications can't be emailed."),
            _("Set up an Email Account with Enable Outgoing and Default Outgoing."),
        )
    ]


def scheduler() -> List[Dict]:
    from frappe.utils.scheduler import is_scheduler_inactive

    area, rows = _("System"), []
    if is_scheduler_inactive(verbose=False):
        rows.append(
            _row(
                area,
                _("Scheduler on"),
                FAIL,
                _("The scheduler is off: emails, recurring documents and scheduled jobs stop."),
                _("Turn it on: bench --site {0} enable-scheduler.").format(frappe.local.site),
            )
        )
    else:
        rows.append(_row(area, _("Scheduler on"), PASS, _("The scheduler is on.")))
    failed = frappe.db.count(
        "Scheduled Job Log", {"status": "Failed", "creation": [">=", add_days(now_datetime(), -7)]}
    )
    if failed:
        rows.append(
            _row(
                area,
                _("Scheduled jobs"),
                WARN,
                _("{0} scheduled jobs failed in the last 7 days.").format(failed),
                _("Open the Scheduled Job Log and fix the failing jobs."),
            )
        )
    return rows


def backup_files() -> List[str]:
    """The site's on-server database backups."""
    return glob.glob(os.path.join(frappe.get_site_path("private", "backups"), "*-database.sql*"))


def backups() -> List[Dict]:
    files = backup_files()
    check = _("Recent backup")
    if not files:
        return [
            _row(
                _("System"),
                check,
                WARN,
                _("No backup of the database on this server."),
                _("Take a backup (bench --site {0} backup) and set up regular ones.").format(
                    frappe.local.site
                ),
            )
        ]
    newest = max(os.path.getmtime(f) for f in files)
    age = (now_datetime().timestamp() - newest) / 86400
    if age > BACKUP_DAYS:
        return [
            _row(
                _("System"),
                check,
                WARN,
                _("The newest backup is {0} days old.").format(int(age)),
                _("Check the backups run every day."),
            )
        ]
    return [_row(_("System"), check, PASS, _("A backup was taken in the last {0} days.").format(BACKUP_DAYS))]


def users_without_roles() -> List[Dict]:
    """Enabled users who have only the roles every user gets: they log in and can do nothing."""
    users = frappe.get_all(
        "User", filters={"enabled": 1, "name": ["not in", ["Administrator", "Guest"]]}, pluck="name"
    )
    with_roles = (
        set(
            frappe.get_all(
                "Has Role",
                filters={
                    "parenttype": "User",
                    "parent": ["in", users],
                    "role": ["not in", list(AUTOMATIC_ROLES)],
                },
                pluck="parent",
            )
        )
        if users
        else set()
    )
    bare = sorted(u for u in users if u not in with_roles)
    if bare:
        return [
            _row(
                _("Users"),
                _("Every user has a role"),
                WARN,
                _("Users with no role, who can log in and do nothing ({0}): {1}.").format(
                    len(bare), _names(bare)
                ),
                _("Give each a role or role profile, or disable the user."),
            )
        ]
    return [_row(_("Users"), _("Every user has a role"), PASS, _("{0} users checked.").format(len(users)))]


# -- opening entries ------------------------------------------------------------------------

#: (DocType, filters) of an opening entry
OPENING = (
    ("Journal Entry", {"is_opening": "Yes"}),
    ("Sales Invoice", {"is_opening": "Yes"}),
    ("Purchase Invoice", {"is_opening": "Yes"}),
    ("Stock Reconciliation", {"purpose": "Opening Stock"}),
)


def opening_entries(company: str) -> List[Dict]:
    drafts = []
    for doctype, filters in OPENING:
        count = frappe.db.count(doctype, {**filters, "company": company, "docstatus": 0})
        if count:
            drafts.append(f"{count} {_(doctype)}")
    if drafts:
        return [
            _row(
                _("Opening entries"),
                _("Opening entries posted"),
                FAIL,
                _("Drafts not posted: {0}. The opening balances aren't in the books yet.").format(
                    ", ".join(drafts)
                ),
                _("Submit them, or delete the ones made by mistake."),
                company,
            )
        ]
    return [
        _row(
            _("Opening entries"),
            _("Opening entries posted"),
            PASS,
            _("No draft opening entries."),
            "",
            company,
        )
    ]


# -- the report -----------------------------------------------------------------------------

COMPANY_FIELDS = [
    "name",
    "default_receivable_account",
    "default_payable_account",
    "round_off_account",
    "round_off_cost_center",
    "default_income_account",
    "default_expense_account",
    "cost_center",
    "enable_perpetual_inventory",
    "stock_adjustment_account",
    "stock_received_but_not_billed",
    "default_inventory_account",
    "default_letter_head",
]


def companies(company: Optional[str]) -> List[Dict]:
    filters = {"name": company} if company else {}
    return frappe.get_all("Company", filters=filters, fields=COMPANY_FIELDS, order_by="name")


def check(company: Optional[str] = None) -> List[Dict]:
    """Every row of the check, failures first, in the order of the areas within a status."""
    found = companies(company)
    if company and not found:
        frappe.throw(_("Company {0} not found.").format(company))
    stock_used = _uses_stock()
    per_company: List[Callable] = [company_defaults, lambda c: fiscal_year(c.name), lambda c: taxes(c.name)]
    if stock_used:
        per_company.append(stock)
    per_company += [printing, lambda c: opening_entries(c.name)]
    rows: List[Dict] = []
    for c in found:
        for checks in per_company:
            rows += checks(c)
    rows += naming_series()
    if stock_used:
        rows += stock_settings()
    for checks in (price_lists, print_formats, email_account, scheduler, backups, users_without_roles):
        rows += checks()
    order = {status: i for i, status in enumerate(STATUSES)}
    return sorted(rows, key=lambda r: order[r["status"]])


def counts(rows: List[Dict]) -> Dict[str, int]:
    return {status: sum(1 for r in rows if r["status"] == status) for status in STATUSES}


def headline(rows: List[Dict]) -> str:
    n = counts(rows)
    ready = _("Ready to go live.") if not n[FAIL] else _("Not ready to go live.")
    return _("{0} fail, {1} warn and {2} pass. {3}").format(n[FAIL], n[WARN], n[PASS], ready)


def columns() -> List[Dict[str, Any]]:
    return [
        {"fieldname": "status", "label": _("Status"), "fieldtype": "Data", "width": 80},
        {"fieldname": "area", "label": _("Area"), "fieldtype": "Data", "width": 170},
        {"fieldname": "check", "label": _("Check"), "fieldtype": "Data", "width": 220},
        {
            "fieldname": "company",
            "label": _("Company"),
            "fieldtype": "Link",
            "options": "Company",
            "width": 140,
        },
        {"fieldname": "detail", "label": _("Detail"), "fieldtype": "Data", "width": 420},
        {"fieldname": "fix", "label": _("Fix"), "fieldtype": "Data", "width": 360},
    ]


def summary_cards(rows: List[Dict]) -> List[Dict[str, Any]]:
    n = counts(rows)
    return [
        {"value": n[FAIL], "label": _("Fail"), "datatype": "Int", "indicator": "Red" if n[FAIL] else "Green"},
        {
            "value": n[WARN],
            "label": _("Warn"),
            "datatype": "Int",
            "indicator": "Orange" if n[WARN] else "Green",
        },
        {"value": n[PASS], "label": _("Pass"), "datatype": "Int", "indicator": "Green"},
        {
            "value": _("Yes") if not n[FAIL] else _("No"),
            "label": _("Ready to Go Live"),
            "datatype": "Data",
            "indicator": "Green" if not n[FAIL] else "Red",
        },
    ]


def _cell(value: Any) -> str:
    return escape_html(cstr(value)).replace("|", "\\|").replace("\n", " ")


def document(rows: List[Dict], company: Optional[str]) -> str:
    """The checklist as a markdown document for generate_document, with a place for the client to
    sign: the same rows as the report, so nothing is typed out again."""
    out = [
        "**"
        + _("Generated on {0} by {1}.").format(
            str(now_datetime())[:16], escape_html(frappe.utils.get_fullname(frappe.session.user))
        )
        + "**",
        "",
        _("Company: {0}").format(escape_html(company)) if company else _("Every company of the site."),
        "",
        headline(rows),
        "",
        "| " + " | ".join([_("Status"), _("Area"), _("Check"), _("Detail"), _("Fix")]) + " |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        out.append(
            "| " + " | ".join(_cell(r[k]) for k in ("status", "area", "check", "detail", "fix")) + " |"
        )
    out += [
        "",
        "## " + _("Sign-off"),
        "",
        _("We have reviewed this checklist and agree the site is ready to go live."),
        "",
        "| | " + _("Name") + " | " + _("Signature") + " | " + _("Date") + " |",
        "|---|---|---|---|",
        "| " + _("Client") + " | | | |",
        "| " + _("Consultant") + " | | | |",
    ]
    return "\n".join(out)


def execute(filters: Optional[Dict[str, Any]] = None):
    """The Go-Live Readiness report: columns, rows, the headline and the summary cards. With the
    ``document`` filter, the message is the whole checklist as a document, for the PDF."""
    filters = frappe._dict(filters or {})
    company = cstr(filters.get("company")).strip() or None
    rows = check(company)
    shown = [{**r, "detail": escape_html(r["detail"]), "fix": escape_html(r["fix"])} for r in rows]
    message = document(rows, company) if cint(filters.get("document")) else headline(rows)
    return columns(), shown, message, None, summary_cards(rows)
