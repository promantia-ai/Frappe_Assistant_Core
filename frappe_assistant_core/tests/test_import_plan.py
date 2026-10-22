# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Migration plans (#33995): several import sessions ordered, checked and run as one."""

from unittest.mock import patch

import frappe

from frappe_assistant_core.core.tool_registry import get_tool_registry
from frappe_assistant_core.tests.base_test import BaseAssistantTest
from frappe_assistant_core.utils import import_opening_stock, import_plan

SESSION = "FAC Import Session"


class ImportPlanCase(BaseAssistantTest):
    """Sessions of small CSV files, mapped, for a plan's tests."""

    def setUp(self):
        super().setUp()
        frappe.set_user("Administrator")
        self.registry = get_tool_registry()
        self.files_before = set(frappe.get_all("File", pluck="name"))
        self.tag = frappe.generate_hash(length=6)

    def tearDown(self):
        frappe.set_user("Administrator")
        # The transaction rollback removes File rows but not the files on disk.
        for name in set(frappe.get_all("File", pluck="name")) - self.files_before:
            frappe.delete_doc("File", name, force=True, ignore_permissions=True)
        super().tearDown()

    def _run(self, tool_name, arguments):
        return self.registry.get_tool(tool_name)._safe_execute(arguments)

    def _session(self, file_name, header, rows, doctype, mapping):
        """Upload a CSV, open its session and map it, as the earlier steps do."""
        lines = [",".join(header)] + [",".join(row) for row in rows]
        file_doc = frappe.get_doc(
            {
                "doctype": "File",
                "file_name": file_name,
                "content": ("\n".join(lines) + "\n").encode(),
                "is_private": 1,
            }
        ).insert()
        started = self._run("start_import_session", {"file_url": file_doc.file_url})
        self.assertTrue(started["success"], started)
        session = started["result"]["session_id"]
        mapped = self._run("set_column_mapping", {"session": session, "mapping": mapping, "doctype": doctype})
        self.assertTrue(mapped["success"], mapped)
        return session

    def _desk_user(self):
        email = "fac-plan-test@example.com"
        if not frappe.db.exists("User", email):
            frappe.get_doc(
                {"doctype": "User", "email": email, "first_name": "Plan", "user_type": "System User"}
            ).insert(ignore_permissions=True)
        return email


def set_plan(plan_name, **fields):
    """Change the plan kept on its first session, as the run does between files."""
    plan = import_plan.get_plan(plan_name)
    for field, value in fields.items():
        setattr(plan, field, value)
    plan.save()


class TestThePlanOnItsFirstSession(ImportPlanCase):
    """A plan has no DocType of its own (#34351): its first file's session keeps it."""

    def _two(self):
        t = self.tag
        item = self._session(
            "items.csv",
            ["Code", "Group"],
            [[f"I {t}", f"G {t}"]],
            "Item",
            {"Code": "item_code", "Group": "item_group"},
        )
        group = self._session("groups.csv", ["Name"], [[f"G {t}"]], "Item Group", {"Name": "item_group_name"})
        return item, group

    def _plan(self, sessions):
        response = self._run("plan_migration", {"sessions": sessions})
        self.assertTrue(response["success"], response)
        return response["result"]["plan"]

    def test_the_plan_is_its_first_files_session(self):
        item, group = self._two()

        plan = self._plan([item, group])

        self.assertEqual(plan, item)
        self.assertEqual(frappe.get_meta(SESSION).get_field("migration_plan").options, SESSION)
        rows = {
            s: frappe.db.get_value(
                SESSION, s, ["migration_plan", "plan_position", "plan_reason"], as_dict=True
            )
            for s in (item, group)
        }
        self.assertEqual({s: r.migration_plan for s, r in rows.items()}, {item: plan, group: plan})
        self.assertEqual((rows[group].plan_position, rows[item].plan_position), (1, 2))
        self.assertIn("groups.csv", rows[item].plan_reason)
        self.assertIsNone(frappe.db.get_value(SESSION, group, "plan_settings"))

    def test_a_plan_is_the_users_own(self):
        item, _group = self._two()
        plan = self._plan([item])

        doc = frappe.get_doc(SESSION, plan)
        self.assertFalse(frappe.has_permission(SESSION, "read", doc=doc, user=self._desk_user()))
        self.assertTrue(frappe.has_permission(SESSION, "read", doc=doc, user="Administrator"))

    def test_the_first_files_session_shows_the_plan_in_fields_of_its_own(self):
        # Read in Desk as Madesh's FAC Migration Plan form read: its status, rule, cut-off, stop
        # and the file importing now, in a Migration Plan section of their own.
        item, group = self._two()
        plan = self._plan([item, group])
        set_plan(
            plan, failure_rule="stop", progress=frappe.as_json({"cut_off": "2026-09-30", "current": group})
        )
        import_plan.request_stop(plan)

        shown = frappe.db.get_value(
            SESSION,
            plan,
            ["plan_status", "plan_failure_rule", "plan_cut_off", "plan_stop_requested", "plan_importing_now"],
        )

        self.assertEqual(shown, ("Planning", "stop", frappe.utils.getdate("2026-09-30"), 1, group))
        self.assertFalse(frappe.db.get_value(SESSION, group, "plan_status"))  # only the first file's
        # the fields between the Migration Plan section and the next one
        fields, in_section = frappe.get_meta(SESSION).fields, []
        start = next(i for i, df in enumerate(fields) if df.fieldname == "plan_section")
        for df in fields[start + 1 :]:
            if df.fieldtype == "Section Break":
                break
            in_section.append(df.fieldname)
        for fieldname in ("migration_plan", "plan_position", "plan_reason", "plan_status", "plan_settings"):
            self.assertIn(fieldname, in_section)
        # what is the whole plan's is said so: it sits on the first file, but isn't that file's
        meta = frappe.get_meta(SESSION)
        for fieldname in ("plan_status", "plan_failure_rule", "plan_cut_off", "plan_stop_requested"):
            self.assertIn("(all files)", meta.get_field(fieldname).label)
        self.assertIn("all files", meta.get_field("whole_plan_heading").label)

    def test_a_plan_logs_its_steps(self):
        item, group = self._two()
        plan = import_plan.get_plan(self._plan([item, group]))

        self.assertEqual((plan.steps[-1].step, plan.steps[-1].message), ("Plan", "2 files in order."))
        self.assertEqual((plan.status, plan.failure_rule), ("Planning", "leave_out"))

    def test_an_older_copy_of_a_session_doesnt_write_the_plan_back(self):
        # The run saves a file's session it read before it moved the plan on (_next_file reads the
        # files, then records the current one). The plan's writes leave modified alone, so Frappe's
        # timestamp check doesn't stop that save: the plan's fields must survive it.
        item, group = self._two()
        plan = self._plan([item, group])
        older, older_first = frappe.get_doc(SESSION, group), frappe.get_doc(SESSION, plan)
        self._run("plan_migration", {"sessions": [], "plan": plan, "order": [item, group]})
        set_plan(plan, failure_rule="stop")

        older.save()
        older_first.save()

        self.assertEqual(frappe.db.get_value(SESSION, group, "plan_position"), 2)
        self.assertEqual(import_plan.get_plan(plan).failure_rule, "stop")

    def test_a_stop_asked_while_the_run_saves_the_plan_stays_asked(self):
        item, group = self._two()
        plan_name = self._plan([item, group])
        running = import_plan.get_plan(plan_name)  # the run's copy, read before the stop

        import_plan.request_stop(plan_name)
        running.status = "Running"
        running.save()

        self.assertEqual(import_plan.get_plan(plan_name).stop_requested, 1)

    def test_a_file_of_a_plan_is_not_a_plan(self):
        item, group = self._two()
        self._plan([item, group])

        response = self._run("plan_questions", {"plan": group})

        self.assertFalse(response["success"], response)
        self.assertIn("not found", response["result"]["error"])

    def test_a_session_that_kept_an_ended_plan_doesnt_keep_a_new_one(self):
        item, group = self._two()
        first = self._plan([item])
        set_plan(first, status="Stopped")

        second = self._plan([item, group])

        self.assertEqual(second, group)
        self.assertEqual(frappe.db.get_value(SESSION, item, "migration_plan"), group)
        self.assertEqual(import_plan.get_plan(second).status, "Planning")


class TestTheOrder(ImportPlanCase):
    def _sessions(self):
        t = self.tag
        price = self._session(
            "prices.csv",
            ["Item", "Price List", "Rate"],
            [[f"I {t}", "Standard Selling", "10"]],
            "Item Price",
            {"Item": "item_code", "Price List": "price_list", "Rate": "price_list_rate"},
        )
        item = self._session(
            "items.csv",
            ["Code", "Group"],
            [[f"I {t}", f"G {t}"]],
            "Item",
            {"Code": "item_code", "Group": "item_group"},
        )
        group = self._session("groups.csv", ["Name"], [[f"G {t}"]], "Item Group", {"Name": "item_group_name"})
        return [frappe.get_doc(SESSION, name) for name in (price, item, group)]

    def test_files_are_ordered_masters_first(self):
        price, item, group = self._sessions()

        ordered, problems = import_plan.order([price, item, group])

        self.assertEqual([o["session"] for o in ordered], [group.name, item.name, price.name])
        self.assertEqual([o["position"] for o in ordered], [1, 2, 3])
        self.assertIn("groups.csv", ordered[1]["reason"])
        self.assertIn("No other file is needed", ordered[0]["reason"])
        self.assertEqual(problems, [])

    def test_opening_files_come_last(self):
        price, item, group = self._sessions()
        real = import_plan.dry_run.special_engine
        stock = import_opening_stock

        with patch.object(
            import_plan.dry_run,
            "special_engine",
            side_effect=lambda s: stock if s.name == group.name else real(s),
        ):
            ordered, _problems = import_plan.order([group, item])

        self.assertEqual([o["session"] for o in ordered], [item.name, group.name])

    def test_a_cycle_is_reported_not_guessed(self):
        price, item, group = self._sessions()
        links = {"Item": {"Item Group"}, "Item Group": {"Item"}}

        with patch.object(
            import_plan, "_links", side_effect=lambda session: links.get(session.target_doctype, set())
        ):
            ordered, problems = import_plan.order([item, group])

        self.assertEqual([o["session"] for o in ordered], [item.name, group.name])
        self.assertEqual(len(problems), 1)
        self.assertIn("items.csv", problems[0])
        self.assertIn("groups.csv", problems[0])


class TestPlanMigration(ImportPlanCase):
    def _two(self):
        t = self.tag
        item = self._session(
            "items.csv",
            ["Code", "Group"],
            [[f"I {t}", f"G {t}"]],
            "Item",
            {"Code": "item_code", "Group": "item_group"},
        )
        group = self._session("groups.csv", ["Name"], [[f"G {t}"]], "Item Group", {"Name": "item_group_name"})
        return item, group

    def test_a_plan_is_made_from_sessions_and_ordered(self):
        item, group = self._two()

        response = self._run("plan_migration", {"sessions": [item, group]})

        self.assertTrue(response["success"], response)
        result = response["result"]
        self.assertEqual([f["doctype"] for f in result["files"]], ["Item Group", "Item"])
        self.assertEqual(result["status"], "Planning")
        for session in (item, group):
            self.assertEqual(frappe.db.get_value(SESSION, session, "migration_plan"), result["plan"])

    def test_more_files_join_an_existing_plan(self):
        item, group = self._two()
        plan = self._run("plan_migration", {"sessions": [item]})["result"]["plan"]

        result = self._run("plan_migration", {"sessions": [group], "plan": plan})["result"]

        self.assertEqual([f["session"] for f in result["files"]], [group, item])

    def test_a_session_in_another_plan_is_refused(self):
        item, group = self._two()
        first = self._run("plan_migration", {"sessions": [item]})["result"]["plan"]

        response = self._run("plan_migration", {"sessions": [group, item]})

        self.assertFalse(response["success"], response)
        self.assertIn(first, " ".join(response["result"]["problems"]))
        self.assertIsNone(frappe.db.get_value(SESSION, group, "migration_plan"))

    def test_an_imported_session_cant_join(self):
        item, _group = self._two()
        frappe.db.set_value(SESSION, item, "status", "Completed")

        response = self._run("plan_migration", {"sessions": [item]})

        self.assertFalse(response["success"], response)
        self.assertIn("Completed", response["result"]["problems"][0])

    def test_another_users_plan_is_refused(self):
        item, group = self._two()
        plan = self._run("plan_migration", {"sessions": [item]})["result"]["plan"]
        frappe.db.set_value(SESSION, plan, "user", self._desk_user())
        frappe.set_user(self._desk_user())
        try:
            response = self._run("plan_migration", {"sessions": [], "plan": plan})
        finally:
            frappe.set_user("Administrator")

        self.assertFalse(response["success"], response)

    def test_the_tool_is_registered_as_write(self):
        from frappe_assistant_core.utils.tool_category_detector import WRITE_TOOLS

        self.assertIsNotNone(self.registry.get_tool("plan_migration"))
        self.assertIn("plan_migration", WRITE_TOOLS)


class TestRecordsEarlierFilesCreate(ImportPlanCase):
    def _plan(self, item_group_value):
        t = self.tag
        self.group_name = f"G {t}"
        group = self._session(
            "groups.csv",
            ["Name", "Parent", "Is Group"],
            [[self.group_name, "All Item Groups", "0"]],
            "Item Group",
            {"Name": "item_group_name", "Parent": "parent_item_group", "Is Group": "is_group"},
        )
        item = self._session(
            "items.csv",
            ["Code", "Name", "Group", "UOM"],
            [[f"I {t}", f"Item {t}", item_group_value, "Nos"]],
            "Item",
            {"Code": "item_code", "Name": "item_name", "Group": "item_group", "UOM": "stock_uom"},
        )
        plan = self._run("plan_migration", {"sessions": [item, group]})["result"]["plan"]
        self.assertTrue(self._run("dry_run_import", {"session": group})["result"]["ready"] == 1)
        return plan, group, item

    def _check(self, session):
        response = self._run("dry_run_import", {"session": session})
        self.assertTrue(response["success"], response)
        return response["result"]

    def test_a_file_that_needs_an_earlier_files_records_is_ready(self):
        _plan, _group, item = self._plan(f"G {self.tag}")

        result = self._check(item)

        self.assertEqual((result["ready"], result["failed"]), (1, 0), result.get("groups"))
        self.assertFalse(frappe.db.exists("Item Group", self.group_name))  # nothing was written

    def test_a_name_that_differs_only_in_case_is_the_same_record(self):
        _plan, _group, item = self._plan(f"g {self.tag}".upper())

        result = self._check(item)

        self.assertEqual((result["ready"], result["failed"]), (1, 0), result.get("groups"))

    def test_a_record_from_a_later_file_is_an_order_problem(self):
        plan, group, item = self._plan(f"G {self.tag}")
        frappe.db.set_value(SESSION, item, "plan_position", 1)
        frappe.db.set_value(SESSION, group, "plan_position", 2)

        result = self._check(item)

        self.assertEqual(result["failed"], 1)
        group_ = result["groups"][0]
        self.assertEqual(group_["cause"], "Comes from a later file")
        self.assertIn("groups.csv", group_["message"])

    def test_a_record_nothing_creates_is_still_missing(self):
        _plan, _group, item = self._plan(f"H {self.tag}")

        result = self._check(item)

        self.assertEqual(result["groups"][0]["cause"], "Missing link")


class TestPlannedMasters(ImportPlanCase):
    def _customers(self):
        self.group = f"Wholesale {self.tag}"
        session = self._session(
            "customers.csv",
            ["Name", "Group", "Type"],
            [[f"C {self.tag}", self.group, "Company"]],
            "Customer",
            {"Name": "customer_name", "Group": "customer_group", "Type": "customer_type"},
        )
        return session

    def _missing_group(self, session):
        result = self._run("dry_run_import", {"session": session})["result"]
        return next(g for g in result["groups"] if g["cause"] == "Missing link")

    def test_a_master_is_planned_not_created(self):
        session = self._customers()
        plan = self._run("plan_migration", {"sessions": [session]})["result"]["plan"]
        group = self._missing_group(session)
        self.assertNotIn("use", next(f for f in group["fixes"] if f["action"] == "create"))

        response = self._run(
            "dry_run_import", {"session": session, "fixes": [{"group": group["id"], "action": "create"}]}
        )

        self.assertTrue(response["success"], response)
        self.assertEqual((response["result"]["ready"], response["result"]["failed"]), (1, 0))
        self.assertFalse(frappe.db.exists("Customer Group", self.group))
        masters = frappe.parse_json(import_plan.get_plan(plan).masters)
        self.assertEqual(
            [(m["doctype"], m["name"], m["session"]) for m in masters],
            [("Customer Group", self.group, session)],
        )

    def test_create_is_refused_outside_a_plan(self):
        session = self._customers()
        group = self._missing_group(session)

        response = self._run(
            "dry_run_import", {"session": session, "fixes": [{"group": group["id"], "action": "create"}]}
        )

        self.assertFalse(response["success"], response)
        self.assertIn("migration plan", str(response))

    def test_planned_masters_are_created_and_linked_to_their_session(self):
        session = self._customers()
        plan = self._run("plan_migration", {"sessions": [session]})["result"]["plan"]
        group = self._missing_group(session)
        self._run(
            "dry_run_import", {"session": session, "fixes": [{"group": group["id"], "action": "create"}]}
        )

        made = import_plan.create_planned(import_plan.get_plan(plan))

        self.assertEqual([m["name"] for m in made], [self.group])
        self.assertTrue(frappe.db.exists("Customer Group", self.group))
        created = frappe.get_doc(SESSION, session).created_records
        self.assertEqual(
            [(r.record_doctype, r.record_name) for r in created], [("Customer Group", self.group)]
        )


class TestPlanQuestions(ImportPlanCase):
    def _plan_with_a_missing_group(self, unmapped=False):
        self.group = f"Retail {self.tag}"
        customers = self._session(
            "customers.csv",
            ["Name", "Group", "Type"],
            [[f"C {self.tag}", self.group, "Company"]],
            "Customer",
            {"Name": "customer_name", "Group": "customer_group", "Type": "customer_type"},
        )
        sessions = [customers]
        if unmapped:
            file_doc = frappe.get_doc(
                {"doctype": "File", "file_name": "notes.csv", "content": b"A,B\n1,2\n", "is_private": 1}
            ).insert()
            sessions.append(
                self._run("start_import_session", {"file_url": file_doc.file_url})["result"]["session_id"]
            )
        plan = self._run("plan_migration", {"sessions": sessions})["result"]["plan"]
        return plan, customers

    def test_every_question_of_the_plan_comes_in_one_list(self):
        plan, _customers = self._plan_with_a_missing_group(unmapped=True)

        result = self._run("plan_questions", {"plan": plan})["result"]

        self.assertEqual(
            sorted({q["kind"] for q in result["questions"]}), ["failure_rule", "fix", "structure"]
        )
        fix = next(q for q in result["questions"] if q["kind"] == "fix")
        self.assertEqual(fix["fixes"][0]["action"], "map")
        self.assertIn("create", [f["action"] for f in fix["fixes"]])
        self.assertFalse(result["ready"])
        self.assertEqual(import_plan.get_plan(plan).status, "Planning")

    def test_answers_are_applied_and_the_plan_becomes_ready_to_run(self):
        plan, _customers = self._plan_with_a_missing_group()
        fix = next(
            q
            for q in self._run("plan_questions", {"plan": plan})["result"]["questions"]
            if q["kind"] == "fix"
        )

        response = self._run(
            "plan_questions",
            {
                "plan": plan,
                "answers": [
                    {"id": fix["id"], "action": "create"},
                    {"id": "plan:failure_rule", "rule": "leave_out"},
                ],
            },
        )

        self.assertTrue(response["success"], response)
        result = response["result"]
        self.assertEqual(result["questions"], [])
        self.assertTrue(result["ready"])
        self.assertEqual(import_plan.get_plan(plan).status, "Ready to Run")
        self.assertFalse(frappe.db.exists("Customer Group", self.group))

    def test_a_wrong_answer_changes_nothing(self):
        plan, _customers = self._plan_with_a_missing_group()
        self._run("plan_questions", {"plan": plan})

        response = self._run(
            "plan_questions",
            {
                "plan": plan,
                "answers": [{"id": "plan:failure_rule", "rule": "stop"}, {"id": "IMP-0:G1", "action": "map"}],
            },
        )

        self.assertFalse(response["success"], response)
        self.assertEqual(import_plan.get_plan(plan).failure_rule, "leave_out")
        self.assertFalse(
            (frappe.parse_json(import_plan.get_plan(plan).progress or "{}") or {}).get("rule_confirmed")
        )


class TestThePlanSkills(ImportPlanCase):
    def test_the_plan_skills_are_registered(self):
        import json
        import os

        import frappe_assistant_core

        app = os.path.dirname(frappe_assistant_core.__file__)
        manifest = json.load(open(os.path.join(app, "data", "system_skills.json")))
        linked = {s["skill_id"]: s.get("linked_tool") for s in manifest}
        self.assertEqual(linked.get("plan-migration-usage"), "plan_migration")
        self.assertEqual(linked.get("plan-questions-usage"), "plan_questions")
        self.assertEqual(linked.get("run-migration-plan-usage"), "run_migration_plan")
        for skill_id in ("plan-migration-usage", "plan-questions-usage", "run-migration-plan-usage"):
            entry = next(s for s in manifest if s["skill_id"] == skill_id)
            self.assertTrue(os.path.exists(os.path.join(app, "..", "docs", "skills", entry["content_file"])))


class TestWhatTheReviewFound(ImportPlanCase):
    """Checks and questions of a plan that the review found wrong (#33995)."""

    def _group_file(self, *names):
        rows = [[name, "All Item Groups", "0"] for name in names]
        return self._session(
            "groups.csv",
            ["Name", "Parent", "Is Group"],
            rows,
            "Item Group",
            {"Name": "item_group_name", "Parent": "parent_item_group", "Is Group": "is_group"},
        )

    def _item_file(self, group, extra=None, file_name="items.csv"):
        header = ["Code", "Name", "Group", "UOM"] + list((extra or {}).keys())
        mapping = {"Code": "item_code", "Name": "item_name", "Group": "item_group", "UOM": "stock_uom"}
        row = [f"I {self.tag}", f"Item {self.tag}", group, "Nos"]
        for column, (field, value) in (extra or {}).items():
            mapping[column] = field
            row.append(value)
        return self._session(file_name, header, [row], "Item", mapping)

    def _questions(self, plan, answers=None):
        response = self._run("plan_questions", {"plan": plan, **({"answers": answers} if answers else {})})
        self.assertTrue(response["success"], response)
        return response["result"]

    def test_a_child_row_linking_to_an_earlier_files_record_is_ready(self):
        box = f"Box {self.tag}"
        uoms = self._session("uoms.csv", ["UOM"], [[box]], "UOM", {"UOM": "uom_name"})
        group = f"G {self.tag}"
        frappe.get_doc(
            {"doctype": "Item Group", "item_group_name": group, "parent_item_group": "All Item Groups"}
        ).insert()
        items = self._item_file(
            group, {"Pack": ("uoms.uom", box), "Per Pack": ("uoms.conversion_factor", "12")}
        )
        self._run("plan_migration", {"sessions": [items, uoms]})
        self._run("dry_run_import", {"session": uoms})

        result = self._run("dry_run_import", {"session": items})["result"]

        self.assertEqual((result["ready"], result["failed"]), (1, 0), result.get("groups"))

    def test_a_ready_file_is_checked_again_when_an_earlier_file_changes(self):
        g, h = f"G {self.tag}", f"H {self.tag}"
        groups = self._group_file(g, h)
        items = self._item_file(h)
        plan = self._run("plan_migration", {"sessions": [items, groups]})["result"]["plan"]
        ready = self._questions(plan, [{"id": "plan:failure_rule", "rule": "leave_out"}])
        self.assertTrue(ready["ready"], ready)

        frappe.db.set_value(SESSION, groups, "excluded_rows", "[3]")  # H's row left out
        result = self._questions(plan)

        self.assertFalse(result["ready"])
        fix = next(q for q in result["questions"] if q["kind"] == "fix")
        self.assertEqual((fix["session"], fix["cause"]), (items, "Missing link"))

    def test_only_mapped_columns_decide_the_order(self):
        # Items made first, so among files free to go they go first: no Group column is mapped, so
        # nothing makes them wait for the groups file, though Item links to Item Group.
        items = self._session(
            "items.csv",
            ["Code", "Name", "UOM"],
            [[f"I {self.tag}", f"Item {self.tag}", "Nos"]],
            "Item",
            {"Code": "item_code", "Name": "item_name", "UOM": "stock_uom"},
        )
        groups = self._group_file(f"G {self.tag}")

        result = self._run("plan_migration", {"sessions": [items, groups]})["result"]

        self.assertEqual([f["session"] for f in result["files"]], [items, groups])

    def test_an_order_the_user_sets_is_kept(self):
        groups = self._group_file(f"G {self.tag}")
        items = self._item_file(f"G {self.tag}")
        plan = self._run("plan_migration", {"sessions": [groups, items]})["result"]["plan"]

        result = self._run("plan_migration", {"sessions": [], "plan": plan, "order": [items, groups]})[
            "result"
        ]

        self.assertEqual([f["session"] for f in result["files"]], [items, groups])
        self.assertEqual(result["problems"], [])

    def test_later_files_wait_for_an_earlier_file_that_isnt_checked(self):
        groups = self._group_file(f"G {self.tag}")
        items = self._item_file(f"G {self.tag}")
        plan = self._run("plan_migration", {"sessions": [items, groups]})["result"]["plan"]

        with patch.object(import_plan.dry_run, "is_running", side_effect=lambda s: s.name == groups):
            result = self._questions(plan)

        kinds = {q["session"]: q["kind"] for q in result["questions"] if q.get("session")}
        self.assertEqual(kinds, {groups: "checking", items: "waiting"})

    def test_a_master_an_earlier_file_creates_isnt_planned_again(self):
        g = f"G {self.tag}"
        groups = self._group_file(g)
        items = self._item_file(g)
        self._run("plan_migration", {"sessions": [items, groups]})
        stale = self._run("dry_run_import", {"session": items})["result"]  # before the groups file's check
        self._run("dry_run_import", {"session": groups})
        group = next(gr for gr in stale["groups"] if gr["cause"] == "Missing link")

        response = self._run(
            "dry_run_import", {"session": items, "fixes": [{"group": group["id"], "action": "create"}]}
        )

        self.assertFalse(response["success"], response)
        self.assertIn("groups.csv", str(response))

    def test_an_earlier_files_failing_row_still_counts_as_coming(self):
        # The items' brand is missing, so their rows fail for now; the prices file still finds them:
        # the user is asked about the brand, not about the items the items file brings.
        group = f"G {self.tag}"
        frappe.get_doc(
            {"doctype": "Item Group", "item_group_name": group, "parent_item_group": "All Item Groups"}
        ).insert()
        items = self._item_file(group, {"Brand": ("brand", f"B {self.tag}")})
        prices = self._session(
            "prices.csv",
            ["Item", "Price List", "Rate"],
            [[f"I {self.tag}", "Standard Selling", "10"]],
            "Item Price",
            {"Item": "item_code", "Price List": "price_list", "Rate": "price_list_rate"},
        )
        plan = self._run("plan_migration", {"sessions": [prices, items]})["result"]["plan"]

        result = self._questions(plan)

        fixes = [(q["file_name"], q["message"]) for q in result["questions"] if q["kind"] == "fix"]
        self.assertEqual(fixes, [("items.csv", f"Brand 'B {self.tag}' doesn't exist.")])
