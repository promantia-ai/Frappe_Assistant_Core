# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""The generic parts of a plan that is approved once and run in the background (#34421, S-D2).

Migration plans (``import_plan``) and setup plans (``setup_plan``) share these: putting the steps in the
order they need, a step that can't leave its plan "Running", and reading a plan's progress. What is
particular to a plan (what a step is, where the plan is stored) stays with it.
"""

from typing import Any, Callable, Dict, List, Optional, Tuple

import frappe


def read_progress(raw: Optional[str]) -> Dict[str, Any]:
    """A plan's progress, kept as JSON text."""
    return frappe.parse_json(raw or "{}") or {}


def topological(
    nodes: List[Any],
    needs: Dict[str, List[Any]],
    rank: Optional[Callable[[Any], Any]] = None,
) -> Tuple[List[Tuple[Any, List[Any]]], List[Tuple[Any, Any]]]:
    """``nodes`` (each with a ``name``) in an order where every node comes after the ones it needs.

    ``needs`` is node name -> the nodes it needs first. Among nodes free to go the one given first goes
    first, or the lowest ``rank`` when given. Returns the nodes each with what it waits for, and a
    ``(node, other)`` pair for every point where the rest wait on each other: that node is placed anyway,
    so a cycle never stops the order."""
    ordered: List[Tuple[Any, List[Any]]] = []
    cycles: List[Tuple[Any, Any]] = []
    placed = set()
    waiting = list(nodes)
    while waiting:
        ready = [n for n in waiting if all(o.name in placed for o in needs[n.name])]
        free = ready[0] if ready else None
        if ready and rank:
            free = min(ready, key=lambda n: (rank(n), waiting.index(n)))
        if free is None:  # every node left waits on another
            free = waiting[0]
            other = next(o for o in needs[free.name] if o.name not in placed)
            cycles.append((free, other))
        waiting.remove(free)
        placed.add(free.name)
        ordered.append((free, needs[free.name]))
    return ordered, cycles


def guarded(
    work: Callable[[], None], savepoint: str, log_title: str, on_failure: Callable[[Exception], None]
) -> None:
    """Run a step of a plan. Whatever goes wrong in it is rolled back to ``savepoint`` and given to
    ``on_failure``, which ends the plan saying why, instead of leaving it Running with nothing to carry it
    on."""
    frappe.db.savepoint(savepoint)
    try:
        work()
    except Exception as e:
        try:
            frappe.db.rollback(save_point=savepoint)
        except Exception:
            if frappe.flags.in_test:
                raise
            frappe.db.rollback()  # a commit inside the step released the savepoint
        frappe.log_error(title=log_title, message=frappe.get_traceback())
        on_failure(e)
