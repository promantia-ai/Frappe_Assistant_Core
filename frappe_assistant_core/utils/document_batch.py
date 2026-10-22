# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Many documents in one tool call: the list mode of create_document and update_document.

One call is one approval card, so a list lets the user approve many records at once. The
records are checked first, then written all or none: they share a savepoint that is rolled
back on the first failure, and what a write leaves for the commit (its global search entry,
the commit callbacks and the realtime and webhook events it queues) is held until all have
succeeded. Each record goes through the tool's own single-record code, so with the same
permission and field checks.

What a save sends out at once, rather than at the commit, can't be held: a Notification's
Slack or SMS message, or a job a controller enqueues straight away. A record undone with a
failed batch may already have sent those, as a single record that fails after saving would.
"""

from typing import Any, Callable, Dict, List, Optional

import frappe
from frappe import _
from frappe.utils import cstr
from frappe.utils.global_search import update_global_search

MAX_DOCUMENTS = 100
SAVEPOINT = "fac_document_batch"
_UNSET = object()

# Realtime events and webhooks wait in these lists for the commit, which flushes them through
# a callback added with the list. A list that outlives its callback would never be sent again.
_QUEUES = ("_realtime_log", "_webhook_queue")


class BatchProblem(Exception):
    """The list can't be written. Nothing was. ``problems`` says which records and why."""

    def __init__(self, message: str, problems: Optional[List[Dict[str, Any]]] = None, written: bool = False):
        super().__init__(message)
        self.problems = problems or []
        self.written = written  # True when a write failed (and was undone), not a check


def given(value) -> bool:
    """An input counts only when it holds something: clients fill optional inputs with
    empty values ("" or [] or {}), which must read as not given."""
    return value not in (None, "", [], {})


def read_documents(arguments: Dict[str, Any], single_inputs: tuple, with_name: bool) -> List[Dict[str, Any]]:
    """``arguments["documents"]`` checked: a list of up to MAX_DOCUMENTS entries, each with a
    doctype and a data object (and a name, for updates), and not mixed with the single-record
    inputs. Raises BatchProblem."""
    documents = arguments.get("documents")
    clashing = [key for key in single_inputs if given(arguments.get(key))]
    if clashing:
        raise BatchProblem(_("Pass either documents or {0}, not both.").format(" + ".join(single_inputs)))
    if not isinstance(documents, list):
        raise BatchProblem(_("documents must be a list."))
    if len(documents) > MAX_DOCUMENTS:
        raise BatchProblem(
            _("At most {0} documents in one call; this has {1}. Split them into several calls.").format(
                MAX_DOCUMENTS, len(documents)
            )
        )
    keys = ("doctype", "name", "data") if with_name else ("doctype", "data")
    problems = []
    for number, entry in enumerate(documents, start=1):
        if not isinstance(entry, dict):
            problems.append({"document": number, "error": _("Each document must be an object.")})
            continue
        missing = [key for key in keys if key != "data" and not cstr(entry.get(key)).strip()]
        if not isinstance(entry.get("data"), dict):
            missing.append("data")
        if missing:
            problems.append({"document": number, "error": _("Missing {0}.").format(", ".join(missing))})
    if problems:
        raise BatchProblem(_("Nothing was written. Correct these and call again."), problems)
    return documents


def check_access(documents: List[Dict[str, Any]], perm_type: str) -> None:
    """Before anything is written: may the user ``perm_type`` each record, without setting a
    field kept from them? The same checks the tools make for one record. Raises BatchProblem
    listing every record that fails."""
    from frappe_assistant_core.core.security_config import get_restricted_fields, validate_document_access

    problems = []
    for number, entry in enumerate(documents, start=1):
        doctype, name = entry["doctype"], entry.get("name")
        access = validate_document_access(
            user=frappe.session.user, doctype=doctype, name=name, perm_type=perm_type
        )
        if not access.get("success"):
            problems.append(_problem(number, entry, access.get("error")))
            continue
        restricted = get_restricted_fields(doctype, access.get("role"))
        kept = [field for field in entry["data"] if field in restricted]
        if kept:
            problems.append(
                _problem(number, entry, _("Cannot set restricted fields: {0}.").format(", ".join(kept)))
            )
    if problems:
        raise BatchProblem(_("Nothing was written. Correct these and call again."), problems)


def write_all(
    documents: List[Dict[str, Any]],
    write: Callable[[Dict[str, Any]], Dict[str, Any]],
    done: str,
    after: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
) -> List[Dict[str, Any]]:
    """``write`` every document, or, if one fails, none.

    ``write`` is the tool's own single-record call and returns its result. ``after`` runs with
    the results inside the same savepoint, so what it saves is undone with a failure too.
    ``done`` is the past tense for messages ("created"). Raises BatchProblem.
    """
    callbacks = _commit_callbacks()
    queues = _commit_queues()
    conf = frappe.local.conf
    search_setting = conf.get("disable_global_search", _UNSET)
    conf["disable_global_search"] = True
    own_commit = vars(frappe.db).get("commit", _UNSET)  # a caller's own stand-in, if any
    frappe.db.commit = _no_commit  # a controller that commits must not end the batch early
    frappe.db.savepoint(SAVEPOINT)
    results = []
    try:
        for number, entry in enumerate(documents, start=1):
            result = write(entry)
            if not result.get("success"):
                error = cstr(result.get("error")).rstrip(".")
                raise BatchProblem(
                    _("Document {0} ({1}) couldn't be {2}: {3}. Nothing was {2}.").format(
                        number, _label(entry), done, error
                    ),
                    [_problem(number, entry, error)],
                    written=True,
                )
            results.append(result)
        if after:
            after(results)
    except Exception:
        frappe.db.rollback(save_point=SAVEPOINT)
        _run_rollback_work(callbacks)
        _restore_commit_callbacks(callbacks)
        _restore_commit_queues(queues)
        raise
    finally:
        if own_commit is _UNSET:
            del frappe.db.commit
        else:
            frappe.db.commit = own_commit
        if search_setting is _UNSET:
            conf.pop("disable_global_search", None)
        else:
            conf["disable_global_search"] = search_setting

    for result in results:
        # The records are written: a search entry that can't be made mustn't report otherwise.
        try:
            update_global_search(frappe.get_doc(result["doctype"], result["name"]))
        except Exception:
            frappe.log_error(title=_("Global search entry not made"), message=frappe.get_traceback())
    return results


def _label(entry: Dict[str, Any]) -> str:
    """ "Note", or "Customer 'CUST-0001'" when the record has a name."""
    label = _(entry["doctype"])
    return f"{label} '{entry['name']}'" if entry.get("name") else label


def _problem(number: int, entry: Dict[str, Any], error: Any) -> Dict[str, Any]:
    problem = {"document": number, "doctype": entry.get("doctype"), "error": cstr(error)}
    if entry.get("name"):
        problem["name"] = entry["name"]
    return problem


def _commit_callbacks() -> Dict[str, list]:
    return {
        name: list(getattr(frappe.db, name)._functions)
        for name in ("before_commit", "after_commit", "before_rollback", "after_rollback")
    }


def _run_rollback_work(callbacks: Dict[str, list]) -> None:
    """Run the after_rollback work the batch queued, as a full rollback would: a File made
    public is moved back to private, a cached document is cleared."""
    queue = frappe.db.after_rollback._functions
    for callback in list(queue)[len(callbacks["after_rollback"]) :]:
        try:
            callback()
        except Exception:
            frappe.log_error(title=_("Document Rollback Error"), message=frappe.get_traceback())


def _restore_commit_callbacks(callbacks: Dict[str, list]) -> None:
    for name, functions in callbacks.items():
        manager = getattr(frappe.db, name)
        manager._functions.clear()
        manager._functions.extend(functions)


def _commit_queues() -> Dict[str, Any]:
    return {
        name: list(getattr(frappe.local, name)) if hasattr(frappe.local, name) else _UNSET for name in _QUEUES
    }


def _restore_commit_queues(queues: Dict[str, Any]) -> None:
    for name, items in queues.items():
        if items is _UNSET:
            if hasattr(frappe.local, name):
                delattr(frappe.local, name)
        else:
            setattr(frappe.local, name, items)


def _no_commit(*args, **kwargs):
    """Stands in for frappe.db.commit while the batch is written."""
    return None
