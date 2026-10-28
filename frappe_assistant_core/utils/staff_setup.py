# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Staff setup (#34423): the client's staff list turned into users, role profiles and user permissions,
and "what can this person see and do?" answered from Frappe's own permission checks.

The plan is by group: one role profile per kind of work, with its roles, its user permissions
(company, warehouse, territory, branch) and its people. Adjusting a group in chat changes everyone in
it. Everything is created together or nothing is.

FAC access is never granted here: a FAC Chat seat is billed, so it is given in FAC's own settings.
"""

from typing import Any, Dict, List

import frappe
from frappe import _
from frappe.permissions import AUTOMATIC_ROLES
from frappe.utils import validate_email_address

from frappe_assistant_core.utils.permissions import ASSISTANT_ACCESS_ROLES

SAVEPOINT = "fac_set_up_staff"
#: Roles a staff list never hands out: admin rights and FAC access are given by hand
REFUSED_ROLES = frozenset(AUTOMATIC_ROLES) | frozenset(ASSISTANT_ACCESS_ROLES) | {"System Manager"}
#: The order access is described in, widest first
ACCESS = ("create and submit", "create", "edit", "read only")


class StaffProblem(Exception):
    """The staff plan can't be applied as given. Every problem is listed; nothing was created."""

    def __init__(self, problems: List[str]):
        super().__init__("\n".join(problems))
        self.problems = problems


def _names(full_name: str) -> Dict[str, str]:
    first, _sep, last = (full_name or "").strip().partition(" ")
    return {"first_name": first, "last_name": last.strip()}


def check_group(index: int, group: Dict[str, Any], seen: set, planned: frozenset = frozenset()) -> List[str]:
    """Every problem of one group; ``seen`` holds the emails of the groups before it, and ``planned``
    the (DocType, name) records a setup plan makes before its staff, which a limit may name."""
    where = group.get("name") or _("group {0}").format(index)
    problems = []
    if not group.get("name"):
        problems.append(_("{0}: give the group a name; it becomes the role profile.").format(where))
    roles = group.get("roles") or []
    if not roles:
        problems.append(_("{0}: no roles.").format(where))
    for role in roles:
        if role in REFUSED_ROLES:
            problems.append(
                _("{0}: {1} is not given from a staff list; give it by hand.").format(where, role)
            )
        elif not frappe.db.exists("Role", {"name": role, "disabled": 0}):
            problems.append(_("{0}: role {1} not found.").format(where, role))
    if group.get("name") and frappe.db.exists("Role Profile", group["name"]):
        existing = set(
            frappe.get_all(
                "Has Role", filters={"parent": group["name"], "parenttype": "Role Profile"}, pluck="role"
            )
        )
        if existing != set(roles):
            problems.append(
                _(
                    "{0}: the role profile already exists with the roles {1}; use those roles or another name."
                ).format(where, ", ".join(sorted(existing)) or "-")
            )
    for permission in group.get("user_permissions") or []:
        allow, value = permission.get("allow"), permission.get("for_value")
        if not allow or not frappe.db.exists("DocType", allow):
            problems.append(_("{0}: {1} is not a DocType to restrict by.").format(where, allow))
        elif (allow, value) not in planned and not frappe.db.exists(allow, value):
            problems.append(_("{0}: {1} {2} not found.").format(where, _(allow), value))
    if not group.get("people"):
        problems.append(_("{0}: no people.").format(where))
    for person in group.get("people") or []:
        email = (person.get("email") or "").strip().lower()
        if not validate_email_address(email):
            problems.append(_("{0}: {1} is not an email address.").format(where, person.get("email")))
        elif email in seen:
            problems.append(_("{0} is in more than one group.").format(email))
        if not (person.get("full_name") or "").strip():
            problems.append(_("{0}: {1} has no name.").format(where, email))
        seen.add(email)
    return problems


def check(groups: List[Dict[str, Any]]) -> None:
    """Raise StaffProblem with every problem of the plan, before anything is created."""
    if not groups:
        raise StaffProblem([_("The plan has no groups.")])
    seen, problems = set(), []
    for index, group in enumerate(groups, start=1):
        problems += check_group(index, group, seen)
    if problems:
        raise StaffProblem(problems)


def _role_profile(group: Dict[str, Any]) -> bool:
    """The group's role profile, made when missing. True when it was made."""
    if frappe.db.exists("Role Profile", group["name"]):
        return False
    frappe.get_doc(
        {
            "doctype": "Role Profile",
            "role_profile": group["name"],
            "roles": [{"role": r} for r in group["roles"]],
        }
    ).insert()
    return True


def _user(person: Dict[str, Any], profile: str, welcome: bool) -> str:
    user = frappe.get_doc(
        {
            "doctype": "User",
            "email": person["email"].strip().lower(),
            **_names(person["full_name"]),
            "user_type": "System User",
            "send_welcome_email": 1 if welcome else 0,
            # v16 moves this into the user's role profiles table itself
            "role_profile_name": profile,
        }
    )
    if user.meta.has_field("assistant_enabled"):
        # FAC access is a billed seat: given in FAC's settings, never by a staff list
        user.assistant_enabled = 0
    user.flags.no_welcome_mail = not welcome
    user.insert()
    return user.name


def set_up(groups: List[Dict[str, Any]], send_welcome_email: bool = False) -> Dict[str, Any]:
    """Create the role profiles, users and user permissions of the plan, all or nothing. People who
    are users already are left as they are and listed."""
    check(groups)
    created, skipped, profiles, restrictions = [], [], [], 0
    frappe.db.savepoint(SAVEPOINT)
    try:
        for group in groups:
            profiles.append({"role_profile": group["name"], "made": _role_profile(group)})
            for person in group["people"]:
                email = person["email"].strip().lower()
                if frappe.db.exists("User", email):
                    skipped.append({"email": email, "reason": _("already a user; not changed")})
                    continue
                user = _user(person, group["name"], send_welcome_email)
                for permission in group.get("user_permissions") or []:
                    frappe.get_doc(
                        {
                            "doctype": "User Permission",
                            "user": user,
                            "allow": permission["allow"],
                            "for_value": permission["for_value"],
                            "apply_to_all_doctypes": 1,
                        }
                    ).insert()
                    restrictions += 1
                created.append({"email": user, "role_profile": group["name"]})
    except Exception:
        frappe.db.rollback(save_point=SAVEPOINT)
        raise
    return {
        "created": created,
        "skipped": skipped,
        "role_profiles": profiles,
        "user_permissions": restrictions,
        "welcome_emails": bool(send_welcome_email) and bool(created),
        "fac_access": _("Not given: FAC Chat seats are billed, so add them in FAC's user settings."),
    }


# -- what can this person see and do --------------------------------------------------------


def _profiles(user: str) -> List[str]:
    if frappe.db.exists("DocType", "User Role Profile"):
        return frappe.get_all("User Role Profile", filters={"parent": user}, pluck="role_profile")
    profile = frappe.db.get_value("User", user, "role_profile_name")
    return [profile] if profile else []


def _access(doctype: str, user: str) -> str:
    def can(ptype):
        return frappe.has_permission(doctype, ptype, user=user)

    if can("create"):
        return ACCESS[0] if frappe.get_meta(doctype).is_submittable and can("submit") else ACCESS[1]
    if can("write"):
        return ACCESS[2]
    return ACCESS[3] if can("read") else ""


def _approves(roles: List[str]) -> List[Dict[str, Any]]:
    out = []
    for name in frappe.get_all("Workflow", filters={"is_active": 1}, pluck="name"):
        workflow = frappe.get_cached_doc("Workflow", name)
        actions = [
            _("{0}: {1} to {2}").format(t.state, t.action, t.next_state)
            for t in workflow.transitions
            if t.allowed in roles
        ]
        if actions:
            out.append({"doctype": workflow.document_type, "workflow": name, "actions": actions})
    return out


def _limits(user: str) -> Dict[str, List[str]]:
    from frappe.permissions import get_user_permissions

    limits = {}
    for allow, rows in (get_user_permissions(user) or {}).items():
        limits[allow] = sorted(
            row.get("doc")
            + (
                " " + _("(only on {0})").format(row.get("applicable_for"))
                if row.get("applicable_for")
                else ""
            )
            for row in rows
        )
    return limits


def access_summary(user: str) -> Dict[str, Any]:
    """What ``user`` can see and do, asked of Frappe's permission checks for every DocType their roles
    name: by module, each DocType as create and submit, create, edit or read only; the workflow
    steps they can act on; and the companies, warehouses or territories they are limited to."""
    every_role = frappe.get_roles(user)
    roles = [r for r in every_role if r not in AUTOMATIC_ROLES]
    summary = {
        "roles": roles,
        "role_profiles": _profiles(user),
        "is_system_manager": "System Manager" in roles,
        "approves": _approves(roles),
        "limited_to": _limits(user),
    }
    if summary["is_system_manager"]:
        # Hundreds of DocTypes: listing them says less than this
        return {**summary, "can": {}, "note": _("System Manager: can see and change almost everything.")}
    named = set()
    for doctype in ("DocPerm", "Custom DocPerm"):
        named |= set(
            frappe.get_all(doctype, filters={"role": ["in", every_role], "permlevel": 0}, pluck="parent")
        )
    rows = frappe.get_all(
        "DocType",
        filters={"name": ["in", list(named) or [""]], "istable": 0, "issingle": 0},
        fields=["name", "module"],
        order_by="module asc, name asc",
    )
    can: Dict[str, Dict[str, str]] = {}
    for row in rows:
        access = _access(row.name, user)
        if access:
            can.setdefault(row.module, {})[row.name] = access
    return {**summary, "can": can}
