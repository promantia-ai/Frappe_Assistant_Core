# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Staff setup (#34423): the client's staff list is the Users and roles area of the setup plan. Saving the
plan checks every group; run_setup_plan creates each group's role profile, users and user permissions,
all or nothing per group. test_as_user without checks says what a user can see and do. Run through the
tools as FACO runs them.
"""

import unittest
from unittest.mock import patch

import frappe
from frappe.core.doctype.user.user import User

from frappe_assistant_core.core.tool_registry import get_tool_registry
from frappe_assistant_core.tests.base_test import BaseAssistantTest
from frappe_assistant_core.utils import setup_plan, staff_setup
from frappe_assistant_core.utils.tool_category_detector import detect_tool_category


@unittest.skipUnless("erpnext" in frappe.get_installed_apps(), "needs ERPNext")
class TestStaffSetup(BaseAssistantTest):
    def setUp(self):
        super().setUp()
        frappe.set_user("Administrator")
        self.tag = frappe.generate_hash(length=6)
        self.company = frappe.get_all("Company", pluck="name", order_by="creation", limit=1)[0]
        self.registry = get_tool_registry()

    def _email(self, who):
        return f"{who}-{self.tag}@example.com"

    def _group(self, name="Sales Executive", roles=("Sales User",), people=("ravi",), limits=True):
        return {
            "name": f"{name} {self.tag}",
            "roles": list(roles),
            "user_permissions": [{"allow": "Company", "for_value": self.company}] if limits else [],
            "people": [{"full_name": f"{p.title()} Kumar", "email": self._email(p)} for p in people],
        }

    def _tool(self, name, arguments):
        return self.registry.get_tool(name)._safe_execute(arguments)

    def _save(self, groups, **answers):
        return self._tool(
            "setup_plan", {"action": "save", "company": self.company, "answers": {"staff": groups, **answers}}
        )

    def _run(self, groups, **answers):
        """Save the groups as a plan, then approve it as the card shows it. Returns the run's reply."""
        saved = self._save(groups, **answers)
        self.assertTrue(saved["success"], saved)
        plan = saved["result"]["plan"]
        figures = setup_plan.card_figures(frappe.get_doc(setup_plan.PLAN, plan))
        ran = self._tool("run_setup_plan", {"plan": plan, **figures})
        ran["plan"] = frappe.get_doc(setup_plan.PLAN, plan)
        return ran

    def test_one_approval_creates_role_profiles_users_and_restrictions(self):
        sales = self._group(people=("ravi", "meena"))
        stores = self._group("Storekeeper", roles=("Stock User",), people=("arun",), limits=False)
        ran = self._run([sales, stores])
        self.assertTrue(ran["success"], ran)
        self.assertEqual([i.state for i in ran["plan"].items], ["Done", "Done"], ran["plan"].as_dict())
        self.assertIn("2 people, limited to Company", ran["plan"].items[0].decision)
        # one person reads as one person: the model echoes the approval back and "corrects" bad
        # grammar, which then no longer matches the card and the run is refused (G2)
        self.assertIn("; 1 person: Arun Kumar", ran["plan"].items[1].decision)
        self.assertIn("Created 1 user with role profile Storekeeper", ran["plan"].items[1].message)

        self.assertEqual(
            set(
                frappe.get_all(
                    "Has Role", filters={"parent": sales["name"], "parenttype": "Role Profile"}, pluck="role"
                )
            ),
            {"Sales User"},
        )
        ravi = self._email("ravi")
        self.assertIn("Sales User", frappe.get_roles(ravi))
        self.assertNotIn("Stock User", frappe.get_roles(ravi))
        self.assertIn("Stock User", frappe.get_roles(self._email("arun")))
        self.assertEqual(frappe.db.get_value("User", ravi, ["first_name", "last_name"]), ("Ravi", "Kumar"))
        self.assertTrue(
            frappe.db.exists("User Permission", {"user": ravi, "allow": "Company", "for_value": self.company})
        )
        self.assertFalse(frappe.db.exists("User Permission", {"user": self._email("arun")}))
        if frappe.get_meta("User").has_field("assistant_enabled"):
            # a FAC seat is billed: never given by a staff list
            self.assertEqual(frappe.db.get_value("User", ravi, "assistant_enabled"), 0)

    def test_welcome_emails_go_only_when_the_plan_says_so(self):
        with patch.object(User, "send_welcome_mail_to_user", autospec=True) as welcome:
            self.assertTrue(self._run([self._group(people=("ravi",))])["success"])
            welcome.assert_not_called()
            buyers = self._group("Buyer", roles=("Purchase User",), people=("meena",))
            self.assertTrue(self._run([buyers], send_welcome_email=True)["success"])
            self.assertEqual(welcome.call_count, 1)

    def test_saving_lists_every_problem_and_leaves_the_group_out(self):
        bad = self._group(
            roles=("Sales User", "System Manager", f"No Such Role {self.tag}"), people=("ravi",)
        )
        bad["user_permissions"].append({"allow": "Company", "for_value": f"No Such Company {self.tag}"})
        bad["people"].append({"full_name": "Not An Email", "email": "not-an-email"})
        twice = self._group("Buyer", roles=("Purchase User",), people=("ravi", "meena"))
        good = self._group("Storekeeper", roles=("Stock User",), people=("arun",))
        saved = self._save([bad, twice, good])
        self.assertTrue(saved["success"], saved)
        problems = "\n".join(saved["result"]["problems"])
        for expected in (
            "System Manager",
            "No Such Role",
            "No Such Company",
            "not-an-email",
            "more than one group",
        ):
            self.assertIn(expected, problems)
        plan = frappe.get_doc(setup_plan.PLAN, saved["result"]["plan"])
        self.assertEqual([i.target_name for i in plan.items], [good["name"]])
        for who in ("ravi", "meena", "arun"):
            self.assertFalse(frappe.db.exists("User", self._email(who)))  # a Draft plan writes nothing

    def test_an_admin_role_in_another_case_is_refused_too(self):
        # The database finds "system manager" as System Manager, and saving stores that spelling
        for spelling in ("system manager", " SYSTEM MANAGER ", "administrator"):
            group = self._group(f"Admins {spelling.strip()}", roles=(spelling,), people=("ravi",))
            problems = "\n".join(staff_setup.check_group(1, group, set()))
            self.assertIn("is not given from a staff list", problems, spelling)

    def test_a_failure_midway_undoes_the_whole_group(self):
        made = staff_setup._user

        def second_fails(person, profile, welcome):
            if person["email"].startswith("meena"):
                raise frappe.ValidationError("refused by a hook on the site")
            return made(person, profile, welcome)

        group = self._group(people=("ravi", "meena"))
        with patch.object(staff_setup, "_user", side_effect=second_fails):
            ran = self._run([group])
        self.assertEqual(ran["plan"].items[0].state, "Failed", ran["plan"].as_dict())
        self.assertFalse(frappe.db.exists("User", self._email("ravi")))
        self.assertFalse(frappe.db.exists("Role Profile", group["name"]))
        self.assertFalse(frappe.db.exists("User Permission", {"user": self._email("ravi")}))

    def test_a_person_who_is_a_user_already_is_left_unchanged(self):
        existing = self.make_throwaway_user("existing", roles=("Accounts User",))
        group = self._group()
        group["people"].append({"full_name": "Existing Person", "email": existing})
        self.assertTrue(self._run([group])["success"])
        self.assertTrue(frappe.db.exists("User", self._email("ravi")))
        self.assertNotIn("Sales User", frappe.get_roles(existing))
        self.assertFalse(frappe.db.exists("User Permission", {"user": existing}))

    def test_an_existing_role_profile_with_other_roles_is_refused(self):
        group = self._group()
        frappe.get_doc(
            {"doctype": "Role Profile", "role_profile": group["name"], "roles": [{"role": "Stock User"}]}
        ).insert()
        saved = self._save([group, self._group("Storekeeper", roles=("Stock User",), people=("arun",))])
        self.assertIn("already exists", "\n".join(saved["result"]["problems"]))

    def test_changing_a_group_applies_to_everyone_in_it(self):
        saved = self._save([self._group(people=("ravi", "meena"))])
        plan = saved["result"]["plan"]
        changed = self._group(roles=("Sales User", "Stock User"), people=("ravi", "meena"))
        updated = self._tool(
            "setup_plan", {"action": "update", "plan": plan, "items": {"1": {"values": {"group": changed}}}}
        )
        self.assertTrue(updated["success"], updated)
        self.assertIn("Sales User, Stock User", frappe.get_doc(setup_plan.PLAN, plan).items[0].decision)

    def test_the_approval_card_names_everyone_who_gets_an_account(self):
        # the list came from a file: an extra address must be seen before it becomes a desk user
        saved = self._save([self._group(people=("ravi", "meena"))])
        decision = frappe.get_doc(setup_plan.PLAN, saved["result"]["plan"]).items[0].decision
        for who in ("ravi", "meena"):
            self.assertIn(self._email(who), decision)

    def test_a_group_may_be_limited_to_a_branch_the_same_plan_makes(self):
        branch = f"Chennai {self.tag}"
        group = self._group(limits=False)
        group["user_permissions"] = [{"allow": "Branch", "for_value": branch}]
        saved = self._save([group], branches=[branch])
        self.assertEqual(saved["result"]["problems"], [], saved)
        ran = self._run([group], branches=[branch])
        self.assertTrue(ran["success"], ran)
        self.assertTrue(
            frappe.db.exists(
                "User Permission", {"user": self._email("ravi"), "allow": "Branch", "for_value": branch}
            )
        )

    def test_a_warehouse_named_as_people_call_it_is_found(self):
        # the sheet says "Stores"; the record is "Stores - <abbr>"
        warehouse = frappe.get_doc(
            {"doctype": "Warehouse", "warehouse_name": f"Stores {self.tag}", "company": self.company}
        ).insert()
        group = self._group("Storekeeper", roles=("Stock User",), people=("arun",), limits=False)
        group["user_permissions"] = [{"allow": "Warehouse", "for_value": f"Stores {self.tag}"}]

        saved = self._save([group])

        self.assertEqual(saved["result"]["problems"], [], saved)
        item = frappe.get_doc(setup_plan.PLAN, saved["result"]["plan"]).items[0]
        limits = frappe.parse_json(item.values)["group"]["user_permissions"]
        self.assertEqual(limits[0]["for_value"], warehouse.name)
        self.assertNotEqual(warehouse.name, f"Stores {self.tag}")

    def test_a_person_added_in_chat_to_a_second_group_is_caught(self):
        saved = self._save([self._group(people=("ravi",))])
        twice = self._group("Buyer", roles=("Purchase User",), people=("ravi",))
        added = self._tool(
            "setup_plan",
            {"action": "update", "plan": saved["result"]["plan"], "add": [{"area": "staff", "group": twice}]},
        )
        self.assertFalse(added["success"])
        self.assertIn("more than one group", added["error"])

    def test_users_are_created_only_by_the_privileged_run(self):
        self.assertIsNone(self.registry.get_tool("set_up_staff"))
        self.assertEqual(detect_tool_category(self.registry.get_tool("run_setup_plan")), "privileged")
        self.assertEqual(detect_tool_category(self.registry.get_tool("setup_plan")), "read_only")

    def test_discovery_asks_about_users_and_roles(self):
        found = self._tool("setup_plan", {"action": "discover", "company": self.company})
        self.assertTrue(found["success"], found)
        self.assertIn("staff", [area["area"] for area in found["result"]["areas"]])

    def _opened_as_an_import(self):
        """The staff list uploaded with the files to import, opened as FACO opens each of them."""
        self.track_test_files()
        self.addCleanup(self.delete_test_files)
        people = "\n".join(
            f"{p.title()} Kumar,{self._email(p)},Sales,Makes quotations" for p in ("ravi", "meena")
        )
        content = f"Employee Name,Email ID,Department,What they do\n{people}\n".encode()
        upload = frappe.get_doc(
            {"doctype": "File", "file_name": f"staff-{self.tag}.csv", "content": content, "is_private": 1}
        ).insert()
        opened = self._tool("start_import_session", {"file_id": upload.name})
        self.assertTrue(opened["success"], opened)
        return upload, opened["result"]["session_id"]

    def test_a_staff_list_opened_as_an_import_is_handed_to_the_setup_plan(self):
        from frappe_assistant_core.plugins.data_science.tools.extract_file_content import ExtractFileContent
        from frappe_assistant_core.utils.import_session import holding_session

        upload, session = self._opened_as_an_import()
        # the import guard keeps the file reader off it (#34003)
        self.assertIn("being imported", ExtractFileContent().execute({"file_id": upload.name})["error"])

        found = self._tool(
            "setup_plan", {"action": "discover", "company": self.company, "staff_session": session}
        )

        self.assertTrue(found["success"], found)
        staff = found["result"]["staff_list"]
        self.assertEqual(staff["columns"][:2], ["Employee Name", "Email ID"])
        self.assertEqual([row[1] for row in staff["people"]], [self._email("ravi"), self._email("meena")])
        # a staff list isn't imported: its session ends and the file is free again
        self.assertEqual(frappe.db.get_value("FAC Import Session", session, "status"), "Cancelled")
        self.assertIsNone(holding_session(upload.name))

    def test_a_session_already_checked_is_not_taken_as_a_staff_list(self):
        _upload, session = self._opened_as_an_import()
        # as a dry run leaves it
        frappe.db.set_value("FAC Import Session", session, "status", "Ready to Import")

        found = self._tool(
            "setup_plan", {"action": "discover", "company": self.company, "staff_session": session}
        )

        self.assertFalse(found["success"])
        self.assertIn("Ready to Import", found["error"])
        self.assertEqual(frappe.db.get_value("FAC Import Session", session, "status"), "Ready to Import")

    # -- what can this person see and do -------------------------------------------------------

    def test_without_checks_test_as_user_says_what_the_user_can_do_and_where(self):
        self.assertTrue(self._run([self._group(people=("ravi",))])["success"])
        reply = self._tool("test_as_user", {"user": self._email("ravi")})
        self.assertTrue(reply["success"], reply)
        summary = reply["result"]["summary"]
        self.assertIn("Sales User", summary["roles"])
        self.assertEqual(summary["role_profiles"], [f"Sales Executive {self.tag}"])
        access = {doctype: what for module in summary["can"].values() for doctype, what in module.items()}
        # Frappe's own answer, not the role table re-read
        self.assertEqual(
            access["Quotation"] in ("create and submit", "create"),
            frappe.has_permission("Quotation", "create", user=self._email("ravi")),
        )
        self.assertNotIn("Purchase Order", {d for d, w in access.items() if w != "read only"})
        self.assertEqual(summary["limited_to"], {"Company": [self.company]})
        self.assertFalse(summary["is_system_manager"])

    def test_a_system_manager_is_summed_up_in_a_line(self):
        manager = self.make_throwaway_user("manager", roles=("System Manager",))
        summary = self._tool("test_as_user", {"user": manager})["result"]["summary"]
        self.assertTrue(summary["is_system_manager"])
        self.assertEqual(summary["can"], {})
        self.assertIn("almost everything", summary["note"])

    def test_with_checks_test_as_user_still_answers_each_check(self):
        reply = self._tool(
            "test_as_user", {"user": "Administrator", "checks": [{"ptype": "read", "doctype": "ToDo"}]}
        )
        self.assertEqual(reply["result"]["results"][0]["outcome"], "allowed")
