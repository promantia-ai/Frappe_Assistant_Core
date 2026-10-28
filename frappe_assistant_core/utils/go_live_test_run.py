# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
The go-live test run (#34424): the client's main flows end to end, on the client's own setup, to
find the gaps before the client does. Buying first (request, order, receipt, bill, payment), so the
received stock is there to sell; then selling (quotation, order, delivery, invoice, payment). Each
document is made from the one before with ERPNext's own "Make" functions and submitted as the user,
so their permissions, workflows, taxes, accounts and numbering are what is tested.

Nothing is kept: the whole run is one savepoint, rolled back at the end, with what a submit sends
out (jobs, emails, realtime events, the naming series' lock) held back as the dry run holds it back.
The ticket's other way, a test company deleted afterwards, would test a copy of the settings, not
the client's own, and leave a company's worth of records to delete.
"""

from typing import Any, Dict, List, Optional

import frappe
from frappe import _
from frappe.utils import flt, nowdate

from frappe_assistant_core.utils import import_dry_run as dry_run

SAVEPOINT = "fac_go_live_test_run"
#: Each step's own: a step that fails part-way leaves nothing for the next step to pass on
STEP_SAVEPOINT = "fac_go_live_step"
BUY, SELL = "buy", "sell"
FLOWS = (BUY, SELL)
#: The rate used when no price list has one for the item, so the money steps still have an amount
FALLBACK_RATE = 100.0
PASSED, FAILED, NOT_RUN = "passed", "failed", "not run"


class TestRunProblem(Exception):
    """The run can't start as asked. The message is shown to the user."""


# -- what the run uses ----------------------------------------------------------------------


def _company(company: Optional[str]) -> str:
    if company:
        return _given("Company", company)
    default = frappe.defaults.get_user_default("Company")
    if default and frappe.db.exists("Company", default) and frappe.has_permission("Company", "read", default):
        return default
    names = frappe.get_list("Company", pluck="name")
    if len(names) == 1:
        return names[0]
    raise TestRunProblem(_("This site has {0} companies: say which one to test.").format(len(names)))


def _first(doctype: str, filters: Dict[str, Any], what: str) -> str:
    """The oldest record the user may read: never one their permissions hide."""
    names = frappe.get_list(doctype, filters=filters, pluck="name", order_by="creation asc", limit=1)
    name = names[0] if names else None
    if not name:
        raise TestRunProblem(_("No {0} to test with: name one, or create one first.").format(what))
    return name


def _given(doctype: str, name: Optional[str]) -> Optional[str]:
    # one the user may not read is "not found", as it is to them
    if name and (not frappe.db.exists(doctype, name) or not frappe.has_permission(doctype, "read", name)):
        raise TestRunProblem(_("{0} {1} not found.").format(_(doctype), name))
    return name


def _item(item: Optional[str]) -> str:
    if _given("Item", item):
        return item
    plain = {"disabled": 0, "has_variants": 0, "has_batch_no": 0, "has_serial_no": 0}
    both = {**plain, "is_sales_item": 1, "is_purchase_item": 1}
    if frappe.db.exists("Item", {**both, "is_stock_item": 1}):
        return _first("Item", {**both, "is_stock_item": 1}, _("item"))
    return _first("Item", both, _("item bought and sold"))


def _warehouse(company: str, warehouse: Optional[str]) -> str:
    if _given("Warehouse", warehouse):
        return warehouse
    # ERPNext v16 dropped Stock Settings' default warehouse
    default = frappe.get_meta("Stock Settings").has_field("default_warehouse") and (
        frappe.db.get_single_value("Stock Settings", "default_warehouse")
    )
    if (
        default
        and frappe.db.get_value("Warehouse", default, "company") == company
        and frappe.has_permission("Warehouse", "read", default)
    ):
        return default
    return _first(
        "Warehouse", {"company": company, "is_group": 0, "disabled": 0}, _("warehouse of {0}").format(company)
    )


def setup(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """What the run uses: the given company, item, customer, supplier and warehouse, else the
    site's defaults or its first ones. Raises TestRunProblem for one that isn't there."""
    company = _company(arguments.get("company"))
    flows = [f for f in (arguments.get("flows") or FLOWS) if f in FLOWS] or list(FLOWS)
    used = {
        "company": company,
        "item": _item(arguments.get("item")),
        "qty": flt(arguments.get("qty")) or 1,
        "rate": flt(arguments.get("rate")) or None,
        "flows": [f for f in FLOWS if f in flows],
    }
    used["warehouse"] = _warehouse(company, arguments.get("warehouse"))
    if BUY in used["flows"]:
        used["supplier"] = _given("Supplier", arguments.get("supplier")) or _first(
            "Supplier", {"disabled": 0, "on_hold": 0}, _("supplier")
        )
    if SELL in used["flows"]:
        used["customer"] = _given("Customer", arguments.get("customer")) or _first(
            "Customer", {"disabled": 0}, _("customer")
        )
    return used


# -- the steps ------------------------------------------------------------------------------


def _with_rate(doc, used: Dict[str, Any], notes: List[str]) -> None:
    """A document whose item came in at zero (no price in the price list) gets the run's rate,
    saved, so the invoice and payment still have an amount: and the user is told why."""
    if any(flt(row.rate) for row in doc.items):
        return
    rate = used["rate"] or FALLBACK_RATE
    for row in doc.items:
        row.rate = rate
    doc.save()
    notes.append(
        _("{0} had no price for {1} in {2}; the run used {3}.").format(
            _(doc.doctype),
            used["item"],
            doc.get("selling_price_list") or doc.get("buying_price_list") or "-",
            rate,
        )
    )


def _made(doc, used: Dict[str, Any], notes: List[str]):
    """Insert and submit a document the run made, at the run's rate when its price came in at zero."""
    if used["rate"]:
        for row in doc.items:
            row.rate = used["rate"]
    doc.insert()
    _with_rate(doc, used, notes)
    doc.submit()
    return doc


def _payment(doctype: str, name: str):
    from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

    pe = get_payment_entry(doctype, name)
    pe.reference_no = "FAC go-live test"
    pe.reference_date = nowdate()
    pe.insert()
    pe.submit()
    return pe


def _buy_steps(used: Dict[str, Any], notes: List[str]) -> List[tuple]:
    from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt
    from erpnext.stock.doctype.material_request.material_request import make_purchase_order
    from erpnext.stock.doctype.purchase_receipt.purchase_receipt import make_purchase_invoice

    company, item, qty, warehouse = used["company"], used["item"], used["qty"], used["warehouse"]
    today = nowdate()

    def request(_before):
        doc = frappe.get_doc(
            {
                "doctype": "Material Request",
                "material_request_type": "Purchase",
                "company": company,
                "transaction_date": today,
                "schedule_date": today,
                "items": [{"item_code": item, "qty": qty, "warehouse": warehouse, "schedule_date": today}],
            }
        )
        doc.insert()
        doc.submit()
        return doc

    def order(mr):
        doc = make_purchase_order(mr.name)
        doc.supplier = used["supplier"]
        doc.schedule_date = today
        for row in doc.items:
            row.warehouse = warehouse
            row.schedule_date = today
        return _made(doc, used, notes)

    return [
        ("Material Request", request),
        ("Purchase Order", order),
        ("Purchase Receipt", lambda po: _made(make_purchase_receipt(po.name), used, notes)),
        ("Purchase Invoice", lambda pr: _made(make_purchase_invoice(pr.name), used, notes)),
        ("Payment Entry", lambda pi: _payment("Purchase Invoice", pi.name)),
    ]


def _sell_steps(used: Dict[str, Any], notes: List[str]) -> List[tuple]:
    from erpnext.selling.doctype.quotation.quotation import make_sales_order
    from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note
    from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice

    company, item, qty, warehouse = used["company"], used["item"], used["qty"], used["warehouse"]
    today = nowdate()

    def quotation(_before):
        doc = frappe.get_doc(
            {
                "doctype": "Quotation",
                "quotation_to": "Customer",
                "party_name": used["customer"],
                "company": company,
                "transaction_date": today,
                "items": [{"item_code": item, "qty": qty, "warehouse": warehouse}],
            }
        )
        return _made(doc, used, notes)

    def order(quote):
        doc = make_sales_order(quote.name)
        doc.delivery_date = today
        doc.set_warehouse = warehouse
        for row in doc.items:
            row.warehouse = warehouse
            row.delivery_date = today
        return _made(doc, used, notes)

    return [
        ("Quotation", quotation),
        ("Sales Order", order),
        ("Delivery Note", lambda so: _made(make_delivery_note(so.name), used, notes)),
        ("Sales Invoice", lambda dn: _made(make_sales_invoice(dn.name), used, notes)),
        ("Payment Entry", lambda si: _payment("Sales Invoice", si.name)),
    ]


def _why(doctype: str, e: Exception) -> str:
    """ERPNext's message for a step that failed, with the workflow named when one is in the way."""
    _kind, message = dry_run.refusal(e)
    workflow = frappe.db.get_value("Workflow", {"document_type": doctype, "is_active": 1}, "name")
    if workflow:
        message = _("{0} has the approval workflow {1}, which the test run can't approve. {2}").format(
            _(doctype), workflow, message
        )
    return message


def _run_flow(flow: str, steps: List[tuple]) -> List[Dict[str, Any]]:
    """Each step from the document before it; after a failure the rest of the flow is not run."""
    results, before, failed = [], None, None
    for doctype, make in steps:
        if failed:
            results.append(
                {
                    "flow": flow,
                    "step": doctype,
                    "status": NOT_RUN,
                    "message": _("Not run: {0} failed.").format(_(failed)),
                }
            )
            continue
        frappe.clear_messages()
        frappe.db.savepoint(STEP_SAVEPOINT)
        try:
            before = make(before)
            results.append(
                {
                    "flow": flow,
                    "step": doctype,
                    "status": PASSED,
                    "amount": flt(before.get("grand_total") or before.get("paid_amount")) or None,
                    "taxes": len(before.get("taxes") or []) or None,
                }
            )
        except Exception as e:
            frappe.db.rollback(save_point=STEP_SAVEPOINT)
            failed = doctype
            results.append({"flow": flow, "step": doctype, "status": FAILED, "message": _why(doctype, e)})
    return results


def run(used: Dict[str, Any]) -> Dict[str, Any]:
    """Run the flows and roll every document back. Nothing the run made is kept."""
    notes: List[str] = []
    steps: List[Dict[str, Any]] = []
    flags = frappe.local.flags
    with dry_run.own_messages(), dry_run.held_back():
        # held_back marks the request as an import, which turns some of ERPNext's checks off:
        # the run is the user's real work, checked as it will be on the first day.
        flags.in_import = False
        frappe.db.savepoint(SAVEPOINT)
        try:
            if BUY in used["flows"]:
                steps += _run_flow(BUY, _buy_steps(used, notes))
            if SELL in used["flows"]:
                steps += _run_flow(SELL, _sell_steps(used, notes))
        finally:
            frappe.db.rollback(save_point=SAVEPOINT)
    failed = [s for s in steps if s["status"] == FAILED]
    return {
        "ready": not failed and all(s["status"] == PASSED for s in steps),
        "used": used,
        "steps": steps,
        "notes": notes,
        "kept": _("Nothing was kept: every document of the run was rolled back."),
    }
