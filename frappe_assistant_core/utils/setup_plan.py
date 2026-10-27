# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Setup plans (#34421): the discovery interview's decisions as records and settings, approved once.

``current_settings`` reads what the site already answers, area by area (S-D5). ``build`` turns the
interview's answers into plan items without writing anything to the site. ``edit`` changes a Draft
plan's items. A plan is applied by ``run_job`` (the second half of this module) through the generic
parts in ``plan_runner``.
"""

import json
from typing import Any, Dict, List, Optional, Tuple

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, getdate

from frappe_assistant_core.utils import compat
from frappe_assistant_core.utils.customisation import CUSTOMISATION_DOCTYPES

PLAN = "FAC Setup Plan"
ITEM = "FAC Setup Plan Item"
#: Single DocTypes and records a plan changes are those of the interview's areas; this is every area.
MAX_ITEMS = 300


class SetupProblem(Exception):
    """The plan can't be made or changed as asked. The message is shown to the user."""


# -- the areas of the interview (S-D4) ------------------------------------------------------------

AREAS: Tuple[Dict[str, Any], ...] = (
    {
        "key": "company",
        "title": "Company and branches",
        "questions": [
            "Is this one legal business, or several with separate books? (Separate books are separate companies.)",
            "Does the business work from more than one branch or location?",
        ],
    },
    {
        "key": "gst",
        "title": "GST registration",
        "india_only": True,
        "questions": [
            "Is the business registered for GST? What is its GSTIN, and does each branch have its own?"
        ],
    },
    {
        "key": "fiscal_year",
        "title": "Financial year",
        "questions": ["Which month does the financial year start in, and which year are you starting in?"],
    },
    {
        "key": "chart_of_accounts",
        "title": "Chart of accounts",
        "questions": [
            "Does the client already have a chart of accounts of their own (an export from their old system)?"
        ],
        "hand_off": "import_chart_of_accounts",
    },
    {
        "key": "items",
        "title": "What they sell and buy",
        "questions": [
            "What kinds of things do you sell and buy (goods you stock, services, both)?",
            "How do you group them, and in what units do you count them?",
        ],
    },
    {
        "key": "stock",
        "title": "Stock and valuation",
        "questions": [
            "Where do you keep goods: one store or several?",
            "Do you track batches or serial numbers?",
            "When the same item is bought at different prices, should the oldest stock be used first (FIFO) or "
            "should ERPNext use the average cost?",
            "May stock go below zero (selling before the purchase is entered)?",
        ],
    },
    {
        "key": "pricing",
        "title": "Price lists and currencies",
        "questions": [
            "Do you sell at different prices to different customers (wholesale, retail)?",
            "Do you buy or sell in a currency other than your own?",
        ],
    },
    {
        "key": "payment_terms",
        "title": "Payment terms",
        "questions": ["What payment terms do you give or get (due on receipt, 30 days, ...)?"],
    },
    {
        "key": "approvals",
        "title": "Approvals",
        "questions": [
            "Does anything need someone's approval before it goes out (a threshold and who approves)?"
        ],
    },
    {
        "key": "naming",
        "title": "Numbering",
        "questions": ["How should invoice and order numbers look?"],
    },
)
AREA_KEYS = tuple(area["key"] for area in AREAS)


def _single(doctype: str, fieldname: str) -> Any:
    if not frappe.db.exists("DocType", doctype) or not compat.has_field(doctype, fieldname):
        return None
    return frappe.db.get_single_value(doctype, fieldname)


def _count(doctype: str, **filters) -> int:
    if not frappe.db.exists("DocType", doctype):
        return 0
    return frappe.db.count(doctype, filters)


def _facts(company: str) -> Dict[str, Dict[str, Any]]:
    """What the site says about each area for ``company``: whether it is set, and a sentence."""
    abbr = frappe.db.get_value("Company", company, "abbr")
    out: Dict[str, Dict[str, Any]] = {}

    branches = _count("Branch")
    out["company"] = {
        "set": bool(abbr),
        "summary": _("Company {0} ({1}), currency {2}, {3} branches.").format(
            company, abbr, frappe.db.get_value("Company", company, "default_currency"), branches
        ),
    }
    gstin = frappe.db.get_value("Company", company, "gstin") if compat.has_field("Company", "gstin") else None
    out["gst"] = {
        "set": bool(gstin),
        "summary": _("GSTIN {0}.").format(gstin) if gstin else _("No GSTIN on the company."),
    }
    year = frappe.db.get_value(
        "Fiscal Year",
        {"year_start_date": ("<=", frappe.utils.today()), "year_end_date": (">=", frappe.utils.today())},
    )
    out["fiscal_year"] = {
        "set": bool(year),
        "summary": _("Financial year {0} covers today.").format(year)
        if year
        else _("No financial year covers today."),
    }
    accounts = _count("Account", company=company)
    out["chart_of_accounts"] = {
        "set": accounts > 0,
        "summary": _("{0} accounts. Importing the client's own chart is its own step.").format(accounts),
    }
    groups = max(_count("Item Group") - 1, 0)  # not the root
    items = _count("Item")
    out["items"] = {
        "set": groups > 3 or items > 0,
        "summary": _("{0} item groups below the root, {1} items.").format(groups, items),
    }
    method = _single("Stock Settings", "valuation_method")
    warehouses = _count("Warehouse", company=company, is_group=0)
    out["stock"] = {
        "set": bool(method) and warehouses > 0,
        "summary": _("Valuation {0}, negative stock {1}, {2} stock warehouses.").format(
            method or "-",
            _("allowed") if cint(_single("Stock Settings", "allow_negative_stock")) else _("not allowed"),
            warehouses,
        ),
        "valuation_method": method,
    }
    lists = _count("Price List", enabled=1)
    currencies = _count("Currency", enabled=1)
    out["pricing"] = {
        "set": lists > 2,
        "summary": _("{0} price lists, {1} currencies enabled.").format(lists, currencies),
    }
    terms = _count("Payment Terms Template")
    out["payment_terms"] = {"set": terms > 0, "summary": _("{0} payment terms templates.").format(terms)}
    workflows = _count("Workflow")
    out["approvals"] = {
        "set": workflows > 0,
        "summary": _(
            "{0} workflows. An approval is a workflow: its own step, or listed as a fit-gap."
        ).format(workflows),
    }
    out["naming"] = {"set": False, "summary": _("Numbering is asked about; the default series are in use.")}
    return out


def current_settings(company: str) -> List[Dict[str, Any]]:
    """The areas of the interview in order, each with what the site already says (S-D5): an area that is
    set carries ``set: True`` and its summary and is asked only if the consultant wants to change it; one
    that isn't carries the plain-word questions to ask. GST is left out where India Compliance isn't
    installed."""
    if not frappe.db.exists("Company", company):
        raise SetupProblem(_("Company {0} doesn't exist.").format(company))
    facts = _facts(company)
    out = []
    for area in AREAS:
        if area.get("india_only") and not compat.has_india_compliance():
            continue
        fact = facts[area["key"]]
        entry = {
            "area": area["key"],
            "title": _(area["title"]),
            "set": bool(fact["set"]),
            "summary": fact["summary"],
        }
        if area.get("hand_off"):
            entry["hand_off"] = area["hand_off"]
        if not fact["set"]:
            entry["questions"] = [_(q) for q in area["questions"]]
        out.append(entry)
    return out


# -- building the plan's items ----------------------------------------------------------------------


def _item(
    area: str,
    decision: str,
    doctype: str,
    name: Optional[str],
    values: Dict[str, Any],
    depends_on: Optional[List[int]] = None,
    created_record: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "area": area,
        "decision": decision,
        "target_doctype": doctype,
        "target_name": name,
        "values": json.dumps(values, sort_keys=True, default=str),
        "depends_on": ", ".join(str(i) for i in depends_on or []),
        "is_customisation": int(doctype in CUSTOMISATION_DOCTYPES),
        "created_record": created_record,
    }


def _select_options(doctype: str, fieldname: str) -> List[str]:
    df = frappe.get_meta(doctype).get_field(fieldname)
    return [o for o in (df.options or "").split("\n") if o] if df else []


def _typed(plan_answers: Dict[str, Any], key: str, kind) -> Any:
    value = plan_answers.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, kind):
        raise SetupProblem(_("{0} in the answers isn't the expected kind of value.").format(key))
    return value


def _from_answers(company: str, answers: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The items the interview's answers become, in the order of its areas. Raises SetupProblem for an
    answer ERPNext can't take."""
    abbr = frappe.db.get_value("Company", company, "abbr")
    items: List[Dict[str, Any]] = []

    for name in _typed(answers, "branches", list) or []:
        items.append(_item("company", _("Branch {0}").format(name), "Branch", name, {"branch": name}))

    year = _typed(answers, "fiscal_year", dict)
    if year:
        start, end = getdate(year.get("from")), getdate(year.get("to"))
        if end <= start:
            raise SetupProblem(_("The financial year must end after it starts."))
        label = f"{start.year}-{end.year}" if start.year != end.year else str(start.year)
        items.append(
            _item(
                "fiscal_year",
                _("Financial year {0}, {1} to {2}").format(label, start, end),
                "Fiscal Year",
                label,
                {"year": label, "year_start_date": str(start), "year_end_date": str(end)},
            )
        )

    for entry in _typed(answers, "item_groups", list) or []:
        name = entry["name"] if isinstance(entry, dict) else entry
        parent = (entry.get("parent") if isinstance(entry, dict) else None) or "All Item Groups"
        items.append(
            _item(
                "items",
                _("Item group {0} under {1}").format(name, parent),
                "Item Group",
                name,
                {"item_group_name": name, "parent_item_group": parent, "is_group": 0},
            )
        )
    for name in _typed(answers, "uoms", list) or []:
        items.append(_item("items", _("Unit {0}").format(name), "UOM", name, {"uom_name": name}))

    method = _typed(answers, "valuation_method", str)
    if method:
        allowed = _select_options("Stock Settings", "valuation_method")
        if allowed and method not in allowed:
            raise SetupProblem(
                _("Valuation method {0} isn't one of: {1}.").format(method, ", ".join(allowed))
            )
        items.append(
            _item(
                "stock",
                _("Value stock by {0}").format(method),
                "Stock Settings",
                "Stock Settings",
                {"valuation_method": method},
            )
        )
    if answers.get("allow_negative_stock") is not None:
        allow = cint(answers["allow_negative_stock"])
        items.append(
            _item(
                "stock",
                _("Stock may go below zero") if allow else _("Stock may not go below zero"),
                "Stock Settings",
                "Stock Settings",
                {"allow_negative_stock": allow},
            )
        )
    for entry in _typed(answers, "warehouses", list) or []:
        name = entry["name"] if isinstance(entry, dict) else entry
        values = {
            "warehouse_name": name,
            "company": company,
            "is_group": cint(entry.get("is_group")) if isinstance(entry, dict) else 0,
        }
        if isinstance(entry, dict) and entry.get("parent"):
            values["parent_warehouse"] = entry["parent"]
        items.append(_item("stock", _("Warehouse {0}").format(name), "Warehouse", f"{name} - {abbr}", values))

    for code in _typed(answers, "currencies", list) or []:
        items.append(
            _item("pricing", _("Enable currency {0}").format(code), "Currency", code, {"enabled": 1})
        )
    for entry in _typed(answers, "price_lists", list) or []:
        name = entry["name"]
        values = {
            "price_list_name": name,
            "currency": entry.get("currency") or frappe.db.get_value("Company", company, "default_currency"),
            "selling": cint(entry.get("selling", 1)),
            "buying": cint(entry.get("buying", 0)),
            "enabled": 1,
        }
        items.append(_item("pricing", _("Price list {0}").format(name), "Price List", name, values))
    for key, doctype, field in (
        ("default_selling_price_list", "Selling Settings", "selling_price_list"),
        ("default_buying_price_list", "Buying Settings", "buying_price_list"),
    ):
        value = _typed(answers, key, str)
        if value:
            items.append(
                _item(
                    "pricing",
                    _("Default {0}: {1}").format(field.replace("_", " "), value),
                    doctype,
                    doctype,
                    {field: value},
                )
            )

    for entry in _typed(answers, "payment_terms", list) or []:
        name, days = entry["name"], cint(entry.get("due_days"))
        items.append(
            _item(
                "payment_terms",
                _("Payment term {0}: due {1} days after the invoice").format(name, days),
                "Payment Term",
                name,
                {
                    "payment_term_name": name,
                    "invoice_portion": 100,
                    "due_date_based_on": "Day(s) after invoice date",
                    "credit_days": days,
                    "description": entry.get("description") or name,
                },
            )
        )

    for raw in _typed(answers, "items", list) or []:
        items.append(_raw_item(raw))
    return items


def _raw_item(raw: Any) -> Dict[str, Any]:
    """An item the interview's model composed itself (anything the answers above don't name)."""
    if not isinstance(raw, dict):
        raise SetupProblem(_("Each item is an object."))
    area, doctype = cstr(raw.get("area")), cstr(raw.get("target_doctype"))
    if area not in AREA_KEYS:
        raise SetupProblem(_("Area {0} isn't one of: {1}.").format(area, ", ".join(AREA_KEYS)))
    if not doctype:
        raise SetupProblem(_("An item needs its target_doctype."))
    values = raw.get("values")
    if not isinstance(values, dict) or not values:
        raise SetupProblem(_("An item needs the values it sets, as an object."))
    return _item(
        area,
        cstr(raw.get("decision")) or doctype,
        doctype,
        cstr(raw.get("target_name")) or None,
        values,
        [cint(i) for i in raw.get("depends_on") or []],
        cstr(raw.get("created_record")) or None,
    )


def check_items(items: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """The items that can be applied on this site, and a sentence for each that can't: its DocType or a
    field isn't on this site (a version or an app the interview assumed), or a customisation hasn't been
    created yet. Nothing is guessed: such an item is left out and said so (RC-9's rule)."""
    good, problems = [], []
    for index, item in enumerate(items, start=1):
        doctype = item["target_doctype"]
        if not frappe.db.exists("DocType", doctype):
            problems.append(_("Item {0}: {1} isn't on this site.").format(index, doctype))
            continue
        meta = frappe.get_meta(doctype)
        missing = [k for k in json.loads(item["values"]) if not meta.has_field(k) and k != "name"]
        if missing:
            problems.append(
                _("Item {0}: {1} has no field {2} on this site.").format(index, doctype, ", ".join(missing))
            )
            continue
        good.append(item)
    return good, problems


def build(company: str, answers: Dict[str, Any]) -> Dict[str, Any]:
    """The plan the interview's answers make, written nowhere: ``items`` (each with whether the site
    already has it), ``gaps`` (what ERPNext doesn't cover as asked) and ``problems`` (what was left out
    and why)."""
    if not frappe.db.exists("Company", company):
        raise SetupProblem(_("Company {0} doesn't exist.").format(company))
    if not isinstance(answers, dict):
        raise SetupProblem(_("The answers are an object."))
    items, problems = check_items(_from_answers(company, answers))
    if len(items) > MAX_ITEMS:
        raise SetupProblem(_("A plan has at most {0} items: split it by area.").format(MAX_ITEMS))
    for item in items:
        item["already_set"] = int(already_set(item))
        if item["is_customisation"] and not item["created_record"]:
            problems.append(
                _(
                    "{0}: create it with create_document first (its own approval), then give its name as created_record."
                ).format(item["decision"])
            )
    gaps = []
    for gap in _typed(answers, "fit_gap", list) or []:
        if not isinstance(gap, dict) or not gap.get("requirement"):
            raise SetupProblem(_("A fit-gap item needs its requirement."))
        gaps.append({k: cstr(gap.get(k)) for k in ("requirement", "why_not_standard", "suggestion")})
    return {"items": items, "gaps": gaps, "problems": problems}


def _same(meta, fieldname: str, current: Any, wanted: Any) -> bool:
    df = meta.get_field(fieldname)
    if df and df.fieldtype in ("Check", "Int"):
        return cint(current) == cint(wanted)
    if df and df.fieldtype in ("Float", "Currency", "Percent"):
        return flt(current) == flt(wanted)
    return cstr(current) == cstr(wanted)


def already_set(item: Dict[str, Any]) -> bool:
    """Whether the site already has this item as planned: a single DocType with those values, or a
    record of that name with them. A customisation counts when the record it names exists."""
    doctype, name = item["target_doctype"], item.get("target_name")
    values = json.loads(item["values"] or "{}")
    meta = frappe.get_meta(doctype)
    if item.get("is_customisation") or doctype in CUSTOMISATION_DOCTYPES:
        return (
            bool(item.get("created_record")) and frappe.db.exists(doctype, item["created_record"]) is not None
        )
    if meta.issingle:
        return all(_same(meta, k, frappe.db.get_single_value(doctype, k), v) for k, v in values.items())
    if not name or not frappe.db.exists(doctype, name):
        return False
    current = frappe.db.get_value(doctype, name, list(values), as_dict=True) or {}
    return all(_same(meta, k, current.get(k), v) for k, v in values.items())


# -- saving and changing a draft -----------------------------------------------------------------------


def save(company: str, answers: Dict[str, Any], title: Optional[str] = None, failure_rule: str = "stop"):
    """Save the plan the answers make as a Draft. Writes only the plan."""
    if failure_rule not in ("stop", "continue"):
        raise SetupProblem(_("The failure rule is stop or continue."))
    built = build(company, answers)
    if not built["items"] and not built["gaps"]:
        raise SetupProblem(_("The answers make no plan items: nothing to save."))
    doc = frappe.get_doc(
        {
            "doctype": PLAN,
            "title": title or _("Setup of {0}").format(company),
            "company": company,
            "failure_rule": failure_rule,
            "answers": json.dumps(answers, default=str),
            "items": built["items"],
            "gaps": built["gaps"],
        }
    )
    doc.insert()
    return doc, built["problems"]


def _draft(name: str):
    doc = frappe.get_doc(PLAN, name)
    doc.check_permission("write")
    if doc.status != "Draft":
        raise SetupProblem(
            _("Plan {0} is {1}: only a Draft can be changed. Its items are what was approved.").format(
                name, _(doc.status)
            )
        )
    return doc


def edit(plan_name: str, changes: Dict[str, Any]):
    """Change a Draft plan (SP-3) and write nothing to the site. ``changes``: ``items`` ({item number:
    {decision, values}} merged into the item), ``remove`` (item numbers), ``add`` (items as the interview
    composes them), ``failure_rule``. Item numbers are the 1-based positions the plan shows."""
    doc = _draft(plan_name)
    if not isinstance(changes, dict) or not changes:
        raise SetupProblem(_("Say what to change."))
    rows = list(doc.items)

    def row(number) -> Any:
        number = cint(number)
        if not 1 <= number <= len(rows):
            raise SetupProblem(_("The plan has no item {0}.").format(number))
        return rows[number - 1]

    for number, change in (changes.get("items") or {}).items():
        target = row(number)
        if not isinstance(change, dict):
            raise SetupProblem(_("Item {0}: say the decision or the values to change.").format(number))
        if change.get("decision"):
            target.decision = cstr(change["decision"])
        if change.get("values"):
            target.values = json.dumps(
                {**json.loads(target.values or "{}"), **change["values"]}, sort_keys=True, default=str
            )
        if change.get("created_record"):
            target.created_record = cstr(change["created_record"])
        _checked = check_items([_as_dict(target)])
        if _checked[1]:
            raise SetupProblem(" ".join(_checked[1]))
        target.already_set = int(already_set(_as_dict(target)))
    removed = {cint(n) for n in changes.get("remove") or []}
    for number in removed:
        row(number)
    keep = [r for i, r in enumerate(rows, start=1) if i not in removed]
    doc.set("items", keep)
    for raw in changes.get("add") or []:
        item = _raw_item(raw)
        good, problems = check_items([item])
        if problems:
            raise SetupProblem(" ".join(problems))
        item["already_set"] = int(already_set(item))
        doc.append("items", item)
    if changes.get("failure_rule"):
        if changes["failure_rule"] not in ("stop", "continue"):
            raise SetupProblem(_("The failure rule is stop or continue."))
        doc.failure_rule = changes["failure_rule"]
    doc.save()
    return doc


def _as_dict(row) -> Dict[str, Any]:
    return {
        "target_doctype": row.target_doctype,
        "target_name": row.target_name,
        "values": row.values or "{}",
        "is_customisation": row.is_customisation,
        "created_record": row.created_record,
    }


def describe(doc) -> Dict[str, Any]:
    """The plan as FACO shows it: its items by area, with numbers, and the fit-gap list."""
    return {
        "plan": doc.name,
        "title": doc.title,
        "company": doc.company,
        "status": doc.status,
        "failure_rule": doc.failure_rule,
        "items": [
            {
                "number": item.idx,
                "area": item.area,
                "decision": item.decision,
                "target": f"{item.target_doctype}: {item.target_name}"
                if item.target_name
                else item.target_doctype,
                "already_set": bool(item.already_set),
                "customisation": bool(item.is_customisation),
                "created_record": item.created_record,
                "state": item.state,
                "message": item.message,
            }
            for item in doc.items
        ],
        "fit_gap": [
            {"requirement": g.requirement, "why_not_standard": g.why_not_standard, "suggestion": g.suggestion}
            for g in doc.gaps
        ],
    }
