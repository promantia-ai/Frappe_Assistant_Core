# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""The generic parts of a plan that runs in the background (#34421 P3.1, S-D2)."""

import frappe

from frappe_assistant_core.tests.base_test import BaseAssistantTest
from frappe_assistant_core.utils import plan_runner


def nodes(*names):
    return [frappe._dict(name=n) for n in names]


class TestTopological(BaseAssistantTest):
    def test_a_node_comes_after_the_nodes_it_needs(self):
        a, b, c = nodes("a", "b", "c")

        ordered, cycles = plan_runner.topological([a, b, c], {"a": [c], "b": [a], "c": []})

        self.assertEqual([n.name for n, _before in ordered], ["c", "a", "b"])
        self.assertEqual(cycles, [])

    def test_each_node_comes_with_what_it_waited_for(self):
        a, b = nodes("a", "b")

        ordered, _cycles = plan_runner.topological([a, b], {"a": [b], "b": []})

        self.assertEqual(
            [(n.name, [o.name for o in before]) for n, before in ordered], [("b", []), ("a", ["b"])]
        )

    def test_nodes_free_to_go_keep_the_order_given(self):
        a, b, c = nodes("a", "b", "c")

        ordered, _cycles = plan_runner.topological([c, a, b], {"a": [], "b": [], "c": []})

        self.assertEqual([n.name for n, _b in ordered], ["c", "a", "b"])

    def test_a_rank_breaks_the_tie_among_free_nodes_and_never_a_need(self):
        a, b, c = nodes("a", "b", "c")
        rank = {"a": 3, "b": 1, "c": 2}

        ordered, _cycles = plan_runner.topological(
            [a, b, c], {"a": [], "b": [a], "c": []}, rank=lambda n: rank[n.name]
        )

        # b has the lowest rank but needs a
        self.assertEqual([n.name for n, _b in ordered], ["c", "a", "b"])

    def test_nodes_that_wait_on_each_other_are_reported_and_still_placed(self):
        a, b = nodes("a", "b")

        ordered, cycles = plan_runner.topological([a, b], {"a": [b], "b": [a]})

        self.assertEqual([n.name for n, _b in ordered], ["a", "b"])
        self.assertEqual([(x.name, y.name) for x, y in cycles], [("a", "b")])

    def test_no_nodes_is_an_empty_order(self):
        self.assertEqual(plan_runner.topological([], {}), ([], []))


class TestGuarded(BaseAssistantTest):
    SAVEPOINT = "fac_test_guarded"

    def test_a_step_that_works_is_left_alone(self):
        failures = []
        plan_runner.guarded(lambda: None, self.SAVEPOINT, "t", failures.append)

        self.assertEqual(failures, [])

    def test_a_step_that_fails_is_rolled_back_and_handed_to_on_failure(self):
        failures = []
        title = f"Guarded {frappe.generate_hash(length=6)}"

        def work():
            frappe.get_doc({"doctype": "Note", "title": title, "public": 0}).insert(ignore_permissions=True)
            raise ValueError("boom")

        plan_runner.guarded(work, self.SAVEPOINT, "FAC guarded test", failures.append)

        self.assertEqual([str(e) for e in failures], ["boom"])
        self.assertFalse(frappe.db.exists("Note", {"title": title}))

    def test_a_failure_is_logged(self):
        before = frappe.db.count("Error Log", {"method": "FAC guarded log test"})

        def work():
            raise ValueError("boom")

        plan_runner.guarded(work, self.SAVEPOINT, "FAC guarded log test", lambda e: None)

        self.assertEqual(frappe.db.count("Error Log", {"method": "FAC guarded log test"}), before + 1)


class TestReadProgress(BaseAssistantTest):
    def test_progress_is_read_from_its_json_text(self):
        self.assertEqual(plan_runner.read_progress('{"done": ["a"]}'), {"done": ["a"]})

    def test_no_progress_is_an_empty_one(self):
        self.assertEqual(plan_runner.read_progress(None), {})
        self.assertEqual(plan_runner.read_progress(""), {})
