# frappe_assistant_core/plugins/core/import_opening_balances.py
# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Opening account balances (#33998): the old system's closing trial balance becomes one opening
Journal Entry (voucher type Opening Entry) at the cut-off date, with the file's balance checked
first. Receivable, Payable and Stock accounts are left to the opening invoices and opening stock
imports; one balancing line to the Temporary Opening account keeps the entry balanced.

It is the third special engine, after opening stock and opening invoices: the dry run reaches it
through ``import_dry_run.special_engine``. An opening-balances session keeps its settings in
``template_options.opening_balances``. This module holds the setup and its pre-checks; the row
checks, creation and report are added on top.
"""

from typing import Any, Dict, Optional

import frappe
from frappe import _
from frappe.utils import cstr, getdate

from frappe_assistant_core.plugins.core import import_opening_invoices as opening_invoices
from frappe_assistant_core.plugins.core.import_mapping import save_mapping, session_headers

TARGET = "Journal Entry"
VOUCHER_TYPE = "Opening Entry"
IMPORT_TOOL = "import_opening_balances"
ACCOUNT_TABLE = "accounts"
FIELDS = ("account", "debit", "credit", "balance")
REQUIRED_FIELDS = ("account",)
MAPPED = {
    "account": "accounts.account",
    "debit": "accounts.debit_in_account_currency",
    "credit": "accounts.credit_in_account_currency",
}
PL_MODES = ("stop", "retained_earnings")
COVERED_TYPES = ("Receivable", "Payable", "Stock")  # left to the opening invoices / stock imports


class OpeningBalancesProblem(Exception):
    """Opening balances can't be set up, checked or created. The message is shown to the user."""


def settings(session) -> Dict[str, Any]:
    options = frappe.parse_json(session.template_options or "{}") or {}
    return dict(options.get("opening_balances") or {})


def is_opening_balances(session) -> bool:
    return bool(settings(session))


def temporary_account(company: str) -> Optional[str]:
    return opening_invoices.temporary_account(company)


def setup_problem(
    company: str,
    cut_off: str,
    profit_and_loss: str,
    retained_earnings_account: Optional[str],
    session=None,
) -> Optional[str]:
    """Why opening balances can't be set up with these values, or None."""
    if not frappe.has_permission(TARGET, "create") or not frappe.has_permission(TARGET, "submit"):
        return _(
            "Opening balances are created and submitted as a Journal Entry, and you can't do both. "
            "An Accounts Manager can import them."
        )
    if not company or not frappe.db.exists("Company", company):
        return _("Company {0} doesn't exist.").format(company)
    if not cut_off:
        return _("Give the cut-off date (the last day in the old system) as YYYY-MM-DD.")
    try:
        date = getdate(cut_off)
    except Exception:
        frappe.clear_messages()
        return _("'{0}' isn't a date. Give the cut-off date as YYYY-MM-DD.").format(cut_off)
    if not opening_invoices.in_fiscal_year(date, company):
        return _("{0} isn't in any fiscal year of {1}. Create the fiscal year first.").format(date, company)
    if not temporary_account(company):
        return _(
            "{0} has no Temporary Opening account (an account of type Temporary). Add one to the "
            "chart of accounts first."
        ).format(company)
    if frappe.db.exists("Period Closing Voucher", {"company": company, "docstatus": 1}):
        return _(
            "{0} has a submitted Period Closing Voucher, and ERPNext doesn't accept an opening entry "
            "once a period is closed."
        ).format(company)
    own = set((settings(session).get("created") or {}) if session else ())
    existing = [
        name
        for name in frappe.get_all(
            "Journal Entry",
            filters={"company": company, "posting_date": date, "is_opening": "Yes", "docstatus": 1},
            pluck="name",
        )
        if name not in own
    ]
    if existing:
        return _("{0} already has an opening Journal Entry on {1}: {2}.").format(
            company, date, ", ".join(existing)
        )
    if cstr(profit_and_loss) not in PL_MODES:
        return _(
            "profit_and_loss is stop (leave Profit and Loss rows out until the user decides) or "
            "retained_earnings (roll them into a retained-earnings account)."
        )
    if profit_and_loss == "retained_earnings":
        account = (
            frappe.db.get_value(
                "Account",
                retained_earnings_account,
                ["company", "root_type", "is_group"],
                as_dict=True,
            )
            if retained_earnings_account
            else None
        )
        if not account or account.company != company or account.root_type != "Equity" or account.is_group:
            return _(
                "Name the retained-earnings account: an Equity account of {0} that isn't a group."
            ).format(company)
    return None


def setup(
    session,
    company: str,
    cut_off: str,
    columns: Dict[str, str],
    profit_and_loss: str = "stop",
    retained_earnings_account: Optional[str] = None,
) -> Dict[str, Any]:
    """Store the company, cut-off, Profit and Loss handling and columns on the session, which
    becomes Mapped. Raises OpeningBalancesProblem, listing every wrong column at once."""
    if session.status not in ("File Read", "Mapped", "Dry Run Failed", "Ready to Import"):
        raise OpeningBalancesProblem(
            _("This session is {0}, so it can't be set up again.").format(_(session.status))
        )
    options = frappe.parse_json(session.template_options or "{}") or {}
    if (options.get("opening_stock") or {}).get("created") or (options.get("opening_invoices") or {}).get(
        "created"
    ):
        raise OpeningBalancesProblem(
            _("This session brought in opening stock or invoices. Start a new session for the balances.")
        )
    earlier = settings(session)
    if earlier.get("created") and earlier.get("company") != company:
        raise OpeningBalancesProblem(
            _(
                "This session already made the opening entry for {0}. Start a new session for the "
                "other company."
            ).format(earlier["company"])
        )
    if earlier.get("created") and earlier.get("cut_off") != cut_off:
        raise OpeningBalancesProblem(
            _(
                "This session already made the opening entry at {0}. Start a new session for another "
                "cut-off date."
            ).format(earlier["cut_off"])
        )
    profit_and_loss = profit_and_loss or "stop"
    problem = setup_problem(company, cut_off, profit_and_loss, retained_earnings_account, session)
    if problem:
        raise OpeningBalancesProblem(problem)

    headers = session_headers(session)
    mapping, extra, given, problems = {}, {}, set(), []
    for column, field in (columns or {}).items():
        if column not in headers:
            problems.append(_("There is no column '{0}'. Columns: {1}.").format(column, ", ".join(headers)))
        elif field not in FIELDS:
            problems.append(_("'{0}' can't be mapped. Map columns to: {1}.").format(field, ", ".join(FIELDS)))
        else:
            given.add(field)
            if field in MAPPED:
                mapping[str(headers.index(column))] = MAPPED[field]
            else:
                extra[field] = headers.index(column)
    missing = [field for field in REQUIRED_FIELDS if field not in given]
    if missing:
        problems.append(_("Map a column to each of: {0}.").format(", ".join(missing)))
    if not given & {"balance", "debit", "credit"}:
        problems.append(_("Map a column to balance (signed), or to debit and credit."))
    if problems:
        raise OpeningBalancesProblem(" ".join(problems))
    for position in range(len(headers)):
        mapping.setdefault(str(position), "Don't Import")

    balances = {
        "company": company,
        "cut_off": str(getdate(cut_off)),
        "profit_and_loss": profit_and_loss,
        "retained_earnings_account": retained_earnings_account
        if profit_and_loss == "retained_earnings"
        else None,
        "temporary_opening_account": temporary_account(company),
        "currency": frappe.get_cached_value("Company", company, "default_currency"),
        "columns": extra,
    }
    if earlier.get("created"):  # what an earlier import made stays on the session
        balances["created"] = earlier["created"]
    for other in ("opening_stock", "opening_invoices"):  # a session is one kind of special import
        if not (options.get(other) or {}).get("created"):
            options.pop(other, None)
    options["opening_balances"] = balances
    session.template_options = frappe.as_json(options)
    session.import_type = "Insert New Records"
    save_mapping(
        session,
        TARGET,
        mapping,
        _("Opening balances for {0}, cut-off {1}, against {2}.").format(
            company, balances["cut_off"], balances["temporary_opening_account"]
        ),
    )
    return {"opening_balances": balances, "column_to_field_map": mapping}


def check_can_run(session) -> None:
    from frappe_assistant_core.plugins.core import import_dry_run as dry_run

    dry_run.check_status(session)
    balances = settings(session)
    problem = setup_problem(
        balances.get("company"),
        balances.get("cut_off"),
        balances.get("profit_and_loss"),
        balances.get("retained_earnings_account"),
        session,
    )
    if problem:
        raise dry_run.DryRunProblem(problem)
    if not session.source_file:
        raise dry_run.DryRunProblem(
            _("The file {0} is only in the chat. Upload it in File Manager and pass its file_id.").format(
                session.file_name
            )
        )


def run(session_name: str, commit_progress: bool = False) -> Dict[str, Any]:
    raise NotImplementedError("The opening-balances check is added with the row checks.")
