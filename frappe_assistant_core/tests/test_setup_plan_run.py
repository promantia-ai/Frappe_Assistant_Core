# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Running a setup plan on one approval (#34421 P3.4 and P3.5, SP-4 to SP-8, S-D3, S-D6)."""

import contextlib
import json
import unittest

import frappe

from frappe_assistant_core.tests.ddl_fixtures import committing_ddl
from frappe_assistant_core.tests.test_import_run import ImportRunCase
from frappe_assistant_core.utils import setup_plan

PLAN = "FAC Setup Plan"
ONBOARDING = "FAC Onboarding Document"


@unittest.skipUnless("erpnext" in frappe.get_installed_apps(), "needs ERPNext's Company and Warehouse")
class TestRunSetupPlan(ImportRunCase):
    def setUp(self):
        super().setUp()
        self.company = frappe.get_all("Company", pluck="name", limit=1)[0]
        self.abbr = frappe.db.get_value("Company", self.company, "abbr")
        self.uom = f"Bolt {self.tag}"
        self.group = f"Fabric {self.tag}"
        self.sub_group = f"Silk {self.tag}"
        self.warehouse = f"Chennai {self.tag}"
        self.price_list = f"Wholesale {self.tag}"

    # -- helpers -----------------------------------------------------------------------------------

    def _plan(self, answers=None, **arguments):
        saved = self._run(
            "setup_plan",
            {
                "action": "save",
                "company": self.company,
                "answers": answers
                or {
                    "uoms": [self.uom],
                    "item_groups": [{"name": self.group}],
                    "warehouses": [{"name": self.warehouse}],
                },
                **arguments,
            },
        )
        self.assertTrue(saved["success"], saved)
        return saved["result"]

    def _approve(self, plan, **override):
        figures = setup_plan.card_figures(frappe.get_doc(PLAN, plan["plan"]))
        return self._run("run_setup_plan", {"plan": plan["plan"], **figures, **override})

    def _doc(self, plan):
        return frappe.get_doc(PLAN, plan["plan"] if isinstance(plan, dict) else plan)

    def _states(self, plan):
        return {i.decision: i.state for i in self._doc(plan).items}

    # -- a plan that runs --------------------------------------------------------------------------

    def test_one_approval_creates_every_record_and_the_plan_completes(self):
        plan = self._plan()

        ran = self._approve(plan)

        self.assertTrue(ran["success"], ran)
        self.assertTrue(frappe.db.exists("UOM", self.uom))
        self.assertTrue(frappe.db.exists("Item Group", self.group))
        self.assertTrue(frappe.db.exists("Warehouse", f"{self.warehouse} - {self.abbr}"))
        doc = self._doc(plan)
        self.assertEqual(doc.status, "Completed")
        self.assertEqual({i.state for i in doc.items}, {"Done"})
        self.assertEqual((doc.applied_by, bool(doc.applied_on)), ("Administrator", True))
        self.assertEqual(doc.approved_digest, setup_plan.digest(doc))

    def test_each_item_keeps_the_name_of_the_record_it_made(self):
        plan = self._plan()
        self._approve(plan)

        made = {i.target_doctype: i.created_record for i in self._doc(plan).items}

        self.assertEqual(made["Warehouse"], f"{self.warehouse} - {self.abbr}")
        self.assertEqual(made["UOM"], self.uom)

    def test_a_setting_is_updated_through_its_single_doctype(self):
        current = frappe.db.get_single_value("Stock Settings", "allow_negative_stock")
        plan = self._plan({"allow_negative_stock": 0 if current else 1})

        self._approve(plan)

        self.assertEqual(
            frappe.db.get_single_value("Stock Settings", "allow_negative_stock"), 0 if current else 1
        )
        self.assertEqual(self._doc(plan).status, "Completed")

    def test_an_item_the_site_already_has_is_skipped_and_the_plan_still_completes(self):
        frappe.get_doc({"doctype": "UOM", "uom_name": self.uom}).insert()
        plan = self._plan()

        self._approve(plan)

        states = self._states(plan)
        self.assertEqual(states[f"Unit {self.uom}"], "Skipped")
        self.assertEqual(self._doc(plan).status, "Completed")

    def test_an_item_that_needs_another_comes_after_it_whatever_the_order_given(self):
        plan = self._plan(
            {
                "item_groups": [{"name": self.sub_group, "parent": self.group}, {"name": self.group}],
            }
        )

        self._approve(plan)

        self.assertEqual(self._doc(plan).status, "Completed", self._doc(plan).log)
        self.assertEqual(frappe.db.get_value("Item Group", self.sub_group, "parent_item_group"), self.group)

    def test_depends_on_orders_items_that_do_not_link(self):
        plan = self._plan(
            {
                "items": [
                    {
                        "area": "items",
                        "decision": "B",
                        "target_doctype": "UOM",
                        "target_name": f"B{self.tag}",
                        "values": {"uom_name": f"B{self.tag}"},
                        "depends_on": [2],
                    },
                    {
                        "area": "items",
                        "decision": "A",
                        "target_doctype": "UOM",
                        "target_name": f"A{self.tag}",
                        "values": {"uom_name": f"A{self.tag}"},
                    },
                ]
            }
        )

        order = [i.decision for i in setup_plan.ordered_items(self._doc(plan))]

        self.assertEqual(order, ["A", "B"])

    def test_items_waiting_for_each_other_are_refused_before_anything_is_written(self):
        plan = self._plan(
            {
                "items": [
                    {
                        "area": "items",
                        "decision": "A",
                        "target_doctype": "UOM",
                        "target_name": f"A{self.tag}",
                        "values": {"uom_name": f"A{self.tag}"},
                        "depends_on": [2],
                    },
                    {
                        "area": "items",
                        "decision": "B",
                        "target_doctype": "UOM",
                        "target_name": f"B{self.tag}",
                        "values": {"uom_name": f"B{self.tag}"},
                        "depends_on": [1],
                    },
                ]
            }
        )

        ran = self._approve(plan)

        self.assertFalse(ran["success"])
        self.assertIn("wait for each other", ran["error"])
        self.assertFalse(frappe.db.exists("UOM", f"A{self.tag}"))
        self.assertEqual(self._doc(plan).status, "Draft")

    # -- failures ----------------------------------------------------------------------------------

    def _plan_with_a_bad_second_item(self, **arguments):
        return self._plan(
            {
                "uoms": [self.uom],
                "warehouses": [{"name": self.warehouse, "parent": f"No Such Parent {self.tag}"}],
                # pricing comes after stock in the interview, so it is the item the stop doesn't reach
                "price_lists": [{"name": self.price_list}],
            },
            **arguments,
        )

    def test_the_plan_stops_at_the_item_that_fails_and_what_was_done_stays(self):
        plan = self._plan_with_a_bad_second_item()

        self._approve(plan)

        doc = self._doc(plan)
        states = {i.target_doctype: i.state for i in doc.items}
        self.assertEqual(doc.status, "Partially Completed")
        self.assertEqual(states["Warehouse"], "Failed")
        self.assertEqual(states["UOM"], "Done")
        self.assertEqual(states["Price List"], "Pending")  # not reached
        self.assertTrue(frappe.db.exists("UOM", self.uom))
        self.assertFalse(frappe.db.exists("Price List", self.price_list))
        self.assertIn("Failed at", json.loads(doc.log)[-1]["message"])

    def test_a_failed_item_leaves_nothing_of_its_own_behind(self):
        plan = self._plan_with_a_bad_second_item()

        self._approve(plan)

        self.assertFalse(frappe.db.exists("Warehouse", {"warehouse_name": self.warehouse}))
        self.assertTrue(next(i for i in self._doc(plan).items if i.state == "Failed").message)

    def test_with_the_continue_rule_the_other_items_still_run(self):
        plan = self._plan_with_a_bad_second_item(failure_rule="continue")

        self._approve(plan)

        doc = self._doc(plan)
        self.assertEqual(doc.status, "Partially Completed")
        self.assertTrue(frappe.db.exists("Price List", self.price_list))
        self.assertEqual(sorted(i.state for i in doc.items), ["Done", "Done", "Failed"])

    def test_a_plan_where_the_first_item_fails_ends_failed(self):
        plan = self._plan({"warehouses": [{"name": self.warehouse, "parent": f"No Such Parent {self.tag}"}]})

        self._approve(plan)

        self.assertEqual(self._doc(plan).status, "Failed")

    def test_a_plan_is_never_left_running(self):
        for rule in ("stop", "continue"):
            plan = self._plan_with_a_bad_second_item(failure_rule=rule)
            self._approve(plan)
            self.assertNotIn(self._doc(plan).status, ("Running", "Approved", "Draft"))

    def test_a_stop_asked_for_ends_the_plan_stopped_with_the_rest_pending(self):
        plan = self._plan()
        frappe.db.set_value(PLAN, plan["plan"], {"status": "Running", "stop_requested": 1})

        setup_plan._apply_plan(plan["plan"])

        doc = self._doc(plan)
        self.assertEqual(doc.status, "Stopped")
        self.assertEqual({i.state for i in doc.items}, {"Pending"})

    # -- customisations ----------------------------------------------------------------------------

    @contextlib.contextmanager
    def _custom_field(self, fieldtype="Data"):
        """A Custom Field on Note made before the plan, as create_document would make it (its own card)."""
        fieldname = f"width_{self.tag}"
        with committing_ddl():
            frappe.get_doc(
                {
                    "doctype": "Custom Field",
                    "dt": "Note",
                    "fieldname": fieldname,
                    "label": "Width",
                    "fieldtype": fieldtype,
                }
            ).insert()
        try:
            yield f"Note-{fieldname}"
        finally:
            with committing_ddl():
                name = frappe.db.get_value("Custom Field", {"dt": "Note", "fieldname": fieldname})
                if name:
                    frappe.delete_doc("Custom Field", name, force=True)
                if frappe.db.has_column("Note", fieldname):
                    frappe.db.sql_ddl(f"alter table `tabNote` drop column `{fieldname}`")
            frappe.clear_cache(doctype="Note")

    def _custom_item(self, created_record=None, fieldtype="Data"):
        return {
            "area": "items",
            "decision": "A field for the fabric width",
            "target_doctype": "Custom Field",
            "target_name": f"Note-width_{self.tag}",
            "values": {"dt": "Note", "fieldname": f"width_{self.tag}", "fieldtype": fieldtype},
            "created_record": created_record,
        }

    def test_a_customisation_that_was_created_first_is_only_checked(self):
        with self._custom_field() as name:
            plan = self._plan({"uoms": [self.uom], "items": [self._custom_item(name)]})
            before = frappe.db.count("Custom Field")

            self._approve(plan)

            self.assertEqual(self._doc(plan).status, "Completed")
            self.assertEqual(frappe.db.count("Custom Field"), before)  # the plan made none
            item = next(i for i in self._doc(plan).items if i.target_doctype == "Custom Field")
            self.assertEqual((item.state, item.created_record), ("Done", name))

    def test_a_customisation_not_created_stops_the_plan_before_anything_is_written(self):
        plan = self._plan({"uoms": [self.uom], "items": [self._custom_item(None)]})

        ran = self._approve(plan)

        self.assertFalse(ran["success"])
        self.assertIn("hasn't been created", ran["error"])
        self.assertFalse(frappe.db.exists("UOM", self.uom))
        self.assertEqual(self._doc(plan).status, "Draft")

    def test_a_customisation_that_differs_from_the_plan_stops_it_before_anything_is_written(self):
        with self._custom_field(fieldtype="Int") as name:
            plan = self._plan({"uoms": [self.uom], "items": [self._custom_item(name, fieldtype="Data")]})

            ran = self._approve(plan)

            self.assertFalse(ran["success"])
            self.assertIn("differs from the plan in: fieldtype", ran["error"])
            self.assertFalse(frappe.db.exists("UOM", self.uom))

    def test_the_runner_never_creates_a_customisation_even_if_the_item_is_not_flagged(self):
        plan = self._plan({"uoms": [self.uom], "items": [self._custom_item(None)]})
        frappe.db.set_value(
            "FAC Setup Plan Item",
            {"parent": plan["plan"], "target_doctype": "Custom Field"},
            "is_customisation",
            0,
        )
        before = frappe.db.count("Custom Field")

        ran = self._approve(plan)

        self.assertFalse(ran["success"])
        self.assertEqual(frappe.db.count("Custom Field"), before)

    def test_a_customisation_changed_after_the_approval_stops_the_job_with_nothing_written(self):
        with self._custom_field() as name:
            plan = self._plan({"uoms": [self.uom], "items": [self._custom_item(name)]})
            frappe.db.set_value(PLAN, plan["plan"], "status", "Running")
            frappe.db.set_value("Custom Field", name, "fieldtype", "Int")

            setup_plan._apply_plan(plan["plan"])

            self.assertFalse(frappe.db.exists("UOM", self.uom))
            self.assertEqual(self._doc(plan).status, "Failed")

    # -- the approval ------------------------------------------------------------------------------

    def test_the_approval_lists_every_item_by_area_and_how_it_stands(self):
        frappe.get_doc({"doctype": "UOM", "uom_name": self.uom}).insert()
        plan = self._plan()

        lines = plan["approval"]["decisions"]

        self.assertEqual(len(lines), 3)
        self.assertTrue(any("(already set)" in line for line in lines), lines)
        self.assertTrue(any("(new)" in line for line in lines), lines)
        self.assertTrue(lines[0].startswith("What they sell and buy"), lines)

    def test_a_plan_changed_after_the_approval_card_is_refused_and_says_why(self):
        plan = self._plan()
        figures = setup_plan.card_figures(self._doc(plan))
        frappe.db.set_value(
            "FAC Setup Plan Item",
            {"parent": plan["plan"], "target_doctype": "UOM"},
            "values",
            json.dumps({"uom_name": f"Other {self.tag}"}),
        )

        ran = self._run("run_setup_plan", {"plan": plan["plan"], **figures})

        self.assertFalse(ran["success"])
        self.assertIn("may have changed", ran["error"])
        self.assertFalse(frappe.db.exists("UOM", self.uom))
        self.assertEqual(self._doc(plan).status, "Draft")

    def test_figures_that_are_not_the_plans_are_refused(self):
        plan = self._plan()

        ran = self._approve(plan, items=99)

        self.assertFalse(ran["success"])
        self.assertEqual(ran["result"]["approval"]["items"], 3)

    def test_decisions_that_are_not_the_plans_are_refused(self):
        plan = self._plan()

        ran = self._approve(plan, decisions=["Create a million records"])

        self.assertFalse(ran["success"])

    def test_a_second_approval_of_a_plan_that_ran_only_reports(self):
        plan = self._plan()
        self._approve(plan)
        before = frappe.db.count("UOM")

        again = self._approve(plan)

        self.assertTrue(again["success"])
        self.assertEqual(again["result"]["already"], "Completed")
        self.assertEqual(frappe.db.count("UOM"), before)

    def test_a_running_plan_is_not_started_again(self):
        plan = self._plan()
        frappe.db.set_value(PLAN, plan["plan"], "status", "Running")

        again = self._approve(plan)

        self.assertEqual(again["result"]["already"], "Running")
        self.assertIn("already running", again["result"]["message"])

    def test_a_plan_with_no_items_is_refused(self):
        doc = frappe.get_doc({"doctype": PLAN, "title": "Empty", "company": self.company}).insert()

        ran = self._run(
            "run_setup_plan",
            {"plan": doc.name, "items": 0, "digest": setup_plan.digest(doc), "decisions": []},
        )

        self.assertFalse(ran["success"])
        self.assertIn("no items", ran["error"])

    # -- who may run it ----------------------------------------------------------------------------

    def test_a_user_who_cannot_write_the_plan_cannot_run_it(self):
        plan = self._plan()
        figures = setup_plan.card_figures(self._doc(plan))
        frappe.set_user(self.make_desk_user("norunsetup"))

        ran = self._run("run_setup_plan", {"plan": plan["plan"], **figures})

        self.assertFalse(ran["success"])
        self.assertFalse(frappe.db.exists("UOM", self.uom))

    def test_an_item_the_running_user_may_not_make_fails_on_their_rights_not_the_systems(self):
        plan = self._plan()
        user = self.make_desk_user("limitedsetup")
        frappe.get_doc("User", user).add_roles("System Manager")
        frappe.set_user(user)

        self._run("run_setup_plan", {"plan": plan["plan"], **setup_plan.card_figures(self._doc(plan))})

        # a System Manager may make UOMs but a user with no rights on Warehouse would fail: the run is as the user
        self.assertEqual(self._doc(plan).applied_by, user)

    def test_the_run_tool_is_a_privileged_one(self):
        from frappe_assistant_core.utils.tool_category_detector import PRIVILEGED_TOOLS

        self.assertIn("run_setup_plan", PRIVILEGED_TOOLS)

    # -- a plan whose worker died --------------------------------------------------------------------

    def test_a_running_plan_nothing_is_running_is_stopped_by_the_sweep(self):
        plan = self._plan()
        frappe.db.set_value(
            PLAN, plan["plan"], {"status": "Running", "applied_on": frappe.utils.add_to_date(None, hours=-2)}
        )

        setup_plan.sweep_dead_plans()

        self.assertEqual(self._doc(plan).status, "Stopped")

    def test_a_plan_that_just_started_is_left_to_its_job(self):
        plan = self._plan()
        frappe.db.set_value(
            PLAN, plan["plan"], {"status": "Running", "applied_on": frappe.utils.now_datetime()}
        )

        setup_plan.sweep_dead_plans()

        self.assertEqual(self._doc(plan).status, "Running")

    # -- the setup record --------------------------------------------------------------------------

    def test_a_finished_plan_leaves_a_setup_record_with_what_was_applied_by_whom_and_when(self):
        plan = self._plan(
            {
                "uoms": [self.uom],
                "fit_gap": [{"requirement": "Three-step PO approval", "suggestion": "A workflow"}],
            }
        )
        self._approve(plan)

        doc = self._doc(plan)
        name = json.loads(doc.progress)["record"]
        record = frappe.get_doc(ONBOARDING, name)

        self.assertEqual((record.document_type, record.company), ("Setup Record", self.company))
        self.assertIn(f"Unit {self.uom}", record.content)
        self.assertIn("Applied by Administrator", record.content)
        self.assertIn("Three-step PO approval", record.content)
        self.assertEqual(setup_plan.describe(doc)["record"], name)

    def test_a_plan_that_failed_still_leaves_its_record_saying_so(self):
        plan = self._plan_with_a_bad_second_item()
        self._approve(plan)

        record = frappe.get_doc(ONBOARDING, json.loads(self._doc(plan).progress)["record"])

        self.assertIn("Failed", record.content)
        self.assertIn("Partially Completed", record.content)
