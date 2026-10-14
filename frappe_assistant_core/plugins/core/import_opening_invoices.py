# frappe_assistant_core/plugins/core/import_opening_invoices.py
# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Opening receivables and payables (#33997): the old system's unpaid customer invoices and
supplier bills become opening Sales or Purchase Invoices, made the way ERPNext's Opening Invoice
Creation Tool makes them.

That tool is one shared record for the whole site; it creates missing parties without
permission checks and reports failures only to the Error Log. So it isn't run: its own code
builds each invoice (one line of the outstanding amount against the Temporary Opening account),
and each is inserted and submitted here, in a savepoint of its own. An opening-invoices session
keeps its settings in ``template_options.opening_invoices``.
"""

from typing import Any, Dict, Optional

import frappe
from frappe import _
from frappe.utils import cstr, getdate

from frappe_assistant_core.plugins.core.import_mapping import save_mapping, session_headers

TOOL = "Opening Invoice Creation Tool"
INVOICE_DOCTYPES = {"Sales": "Sales Invoice", "Purchase": "Purchase Invoice"}
PARTY_TYPES = {"Sales": "Customer", "Purchase": "Supplier"}
PARTY_FIELDS = {"Customer": "customer", "Supplier": "supplier"}
PARTY_NAME_FIELDS = {"Customer": "customer_name", "Supplier": "supplier_name"}
FIELDS = ("party", "outstanding_amount", "invoice_number", "posting_date", "due_date", "item_name")
MAPPED = ("party", "outstanding_amount", "posting_date", "due_date")  # fields of the invoice itself
REQUIRED_FIELDS = ("party", "outstanding_amount")
REQUEST_LIMIT = 100  # more invoices than this are created by a background job
COMMIT_EVERY = 25  # the job commits this many invoices at a time, with the session's record of them


class OpeningInvoicesProblem(Exception):
    """Opening invoices can't be set up, checked or created. The message is shown to the user."""


def settings(session) -> Dict[str, Any]:
    options = frappe.parse_json(session.template_options or "{}") or {}
    return dict(options.get("opening_invoices") or {})


def is_opening_invoices(session) -> bool:
    return bool(settings(session))


def is_party(session, doctype: str) -> bool:
    """Whether ``doctype`` is the party of an opening-invoices ``session``."""
    return bool(session) and settings(session).get("party_type") == doctype


def temporary_account(company: str) -> Optional[str]:
    return frappe.db.get_value("Account", {"company": company, "account_type": "Temporary", "is_group": 0})


def in_fiscal_year(date, company: str) -> bool:
    from erpnext.accounts.utils import get_fiscal_year

    return bool(get_fiscal_year(date, company=company, boolean=True))


def setup_problem(company: str, invoice_type: str, cut_off: str) -> Optional[str]:
    """Why opening invoices can't be set up with these values, or None."""
    doctype = INVOICE_DOCTYPES.get(cstr(invoice_type))
    if not doctype:
        return _("invoice_type is Sales (what customers owe) or Purchase (what is owed to suppliers).")
    if not frappe.has_permission(doctype, "create") or not frappe.has_permission(doctype, "submit"):
        return _(
            "Opening invoices are created and submitted as {0}s, and you can't do both. An Accounts "
            "Manager can import them."
        ).format(_(doctype))
    if not company or not frappe.db.exists("Company", company):
        return _("Company {0} doesn't exist.").format(company)
    if not cut_off:
        return _("Give the cut-off date (the last day in the old system) as YYYY-MM-DD.")
    try:
        date = getdate(cut_off)
    except Exception:
        frappe.clear_messages()
        return _("'{0}' isn't a date. Give the cut-off date as YYYY-MM-DD.").format(cut_off)
    if not in_fiscal_year(date, company):
        return _("{0} isn't in any fiscal year of {1}. Create the fiscal year first.").format(date, company)
    if not temporary_account(company):
        return _(
            "{0} has no Temporary Opening account (an account of type Temporary). Add one to the "
            "chart of accounts first."
        ).format(company)
    if not frappe.get_cached_value("Company", company, "cost_center"):
        return _("{0} has no default cost center. Set it on the company first.").format(company)
    return None


def setup(session, company: str, invoice_type: str, cut_off: str, columns: Dict[str, str]) -> Dict[str, Any]:
    """Store the company, the invoice type, the cut-off and the columns on the session, which
    becomes Mapped. Raises OpeningInvoicesProblem, listing every wrong column at once."""
    if session.status not in ("File Read", "Mapped", "Dry Run Failed", "Ready to Import"):
        raise OpeningInvoicesProblem(
            _("This session is {0}, so it can't be set up again.").format(_(session.status))
        )
    options = frappe.parse_json(session.template_options or "{}") or {}
    if (options.get("opening_stock") or {}).get("created"):
        raise OpeningInvoicesProblem(
            _("This session brought in opening stock. Start a new session for the invoices.")
        )
    earlier = settings(session)
    if earlier.get("created") and earlier.get("invoice_type") != invoice_type:
        raise OpeningInvoicesProblem(
            _("This session already made {0} invoices. Start a new session for the other kind.").format(
                _(earlier["invoice_type"])
            )
        )
    problem = setup_problem(company, invoice_type, cut_off)
    if problem:
        raise OpeningInvoicesProblem(problem)

    doctype, party_type = INVOICE_DOCTYPES[invoice_type], PARTY_TYPES[invoice_type]
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
                mapping[str(headers.index(column))] = PARTY_FIELDS[party_type] if field == "party" else field
            else:
                extra[field] = headers.index(column)
    missing = [field for field in REQUIRED_FIELDS if field not in given]
    if missing:
        problems.append(_("Map a column to each of: {0}.").format(", ".join(missing)))
    if problems:
        raise OpeningInvoicesProblem(" ".join(problems))
    for position in range(len(headers)):
        mapping.setdefault(str(position), "Don't Import")

    invoices = {
        "company": company,
        "invoice_type": invoice_type,
        "doctype": doctype,
        "party_type": party_type,
        "cut_off": str(getdate(cut_off)),
        "temporary_opening_account": temporary_account(company),
        "currency": frappe.get_cached_value("Company", company, "default_currency"),
        "columns": extra,
    }
    for kept in ("created", "failed"):  # what earlier imports made stays on the session
        if earlier.get(kept):
            invoices[kept] = earlier[kept]
    options.pop("opening_stock", None)  # a session is one kind of special import
    options["opening_invoices"] = invoices
    session.template_options = frappe.as_json(options)
    session.import_type = "Insert New Records"
    save_mapping(
        session,
        doctype,
        mapping,
        _("Opening {0} invoices for {1}, cut-off {2}, against {3}.").format(
            _(invoice_type), company, invoices["cut_off"], invoices["temporary_opening_account"]
        ),
    )
    return {"opening_invoices": invoices, "column_to_field_map": mapping}


def check_can_run(session) -> None:
    from frappe_assistant_core.plugins.core import import_dry_run as dry_run

    dry_run.check_status(session)
    invoices = settings(session)
    problem = setup_problem(invoices.get("company"), invoices.get("invoice_type"), invoices.get("cut_off"))
    if problem:
        raise dry_run.DryRunProblem(problem)
    if not session.source_file:
        raise dry_run.DryRunProblem(
            _("The file {0} is only in the chat. Upload it in File Manager and pass its file_id.").format(
                session.file_name
            )
        )


def run(session_name: str, commit_progress: bool = False) -> Dict[str, Any]:
    raise NotImplementedError  # Task 3
