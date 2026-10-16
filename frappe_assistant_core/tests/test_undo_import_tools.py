# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
The two tools that undo an import (#34002): preview_import_undo (read-only) and undo_import
(write, one approval). Both are thin wrappers over import_undo, whose engine is tested in
test_import_undo.py: these tests cover what the tools say and refuse.

The run tests need the bench's Redis queue up: import_undo.start enqueues with deduplicate=True,
and Frappe asks the queue for the job before it honours now=True.
"""

import unittest
from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, now_datetime, nowdate

from frappe_assistant_core.core.tool_registry import get_tool_registry
from frappe_assistant_core.plugins.core import import_undo
from frappe_assistant_core.plugins.core.plugin import CorePlugin
from frappe_assistant_core.tests.test_import_run import MAPPING
from frappe_assistant_core.tests.test_import_undo import BlockerCase
from frappe_assistant_core.utils.tool_category_detector import detect_tool_category


@unittest.skipUnless("erpnext" in frappe.get_installed_apps(), "needs ERPNext's Customer")
class UndoToolsCase(BlockerCase):
    def setUp(self):
        super().setUp()
        self.registry = get_tool_registry()

    def _preview(self, session):
        reply = self._run("preview_import_undo", {"session": session})
        self.assertTrue(reply["success"], reply)
        return reply["result"]

    def _undo(self, session, records, blocked, mode):
        return self._run(
            "undo_import", {"session": session, "records": records, "blocked": blocked, "mode": mode}
        )

    def _approved(self, session, mode):
        """Preview the session, then undo it with the figures the preview gave."""
        preview = self._preview(session)
        return self._undo(session, preview["records"], preview["blocked"], mode)

    def _exists(self, doctype, name):
        return bool(frappe.db.exists(doctype, name))

    def _undo_option(self, session):
        options = frappe.parse_json(self._doc(session).template_options or "{}") or {}
        return options.get("undo") or {}

    def _stored_fingerprint(self, session):
        options = frappe.parse_json(self._doc(session).template_options or "{}") or {}
        return (options.get("undo") or {}).get("fingerprint")

    def _a_kept_group_session(self):
        """Three Customers and their Customer Group; alpha was changed by someone else."""
        session = self._session_with_customers()
        self._add(session, "Customer Group", self.group, "All Customer Groups")
        self._edited_by_someone_else("Customer", self._names()["alpha"])
        return session


class TestPreview(UndoToolsCase):
    def test_preview_lists_counts_by_doctype_and_the_blockers(self):
        session = self._a_kept_group_session()
        alpha = self._names()["alpha"]

        preview = self._preview(session)

        self.assertEqual((preview["session"], preview["status"]), (session, self._doc(session).status))
        self.assertEqual((preview["can_undo"], preview["refused"]), (True, None))
        self.assertEqual((preview["records"], preview["blocked"], preview["kept"]), (4, 1, 1))
        self.assertEqual(
            {(d["doctype"], d["count"]) for d in preview["by_doctype"]},
            {("Customer", 3), ("Customer Group", 1)},
        )
        customers = next(d for d in preview["by_doctype"] if d["doctype"] == "Customer")
        self.assertEqual(set(customers["sample"]), set(self._names().values()) - {self._names()["beta"]})
        self.assertEqual(len(preview["blockers"]), 1)
        blockers = preview["blockers"][0]
        self.assertEqual((blockers["reason"], blockers["count"]), ("changed", 1))
        self.assertEqual(blockers["doctypes"], ["Customer"])
        self.assertEqual(blockers["sample"][0]["record"], f"Customer {alpha}")
        self.assertEqual(
            preview["kept_records"],
            [
                {
                    "record": f"Customer Group {self.group}",
                    "kept_because": preview["kept_records"][0]["kept_because"],
                }
            ],
        )
        self.assertIn(alpha, preview["kept_records"][0]["kept_because"])
        self.assertEqual(preview["already_removed"], 0)
        self.assertTrue(preview["next"])

    def test_preview_names_what_each_blocker_is_blocked_by(self):
        session, names = self._session_with_groups(2)
        outside = self._outside_customer(names[0])

        preview = self._preview(session)

        self.assertEqual((preview["records"], preview["blocked"], preview["kept"]), (2, 1, 0))
        blockers = preview["blockers"][0]
        self.assertEqual((blockers["reason"], blockers["count"]), ("linked", 1))
        self.assertEqual(
            {k: blockers["sample"][0][k] for k in ("record", "blocked_by")},
            {"record": f"Customer Group {names[0]}", "blocked_by": f"Customer {outside}"},
        )
        self.assertEqual(preview["window"]["days"], 30)
        self.assertEqual(
            preview["window"]["ends_on"], str(import_undo.window_ends(self._doc(session)).date())
        )

    def test_preview_stores_the_fingerprint_on_the_session(self):
        session, _names = self._session_with_groups(2)
        before = self._doc(session).status

        self._preview(session)

        self.assertEqual(self._stored_fingerprint(session), self._plan(session).fingerprint)
        self.assertEqual(self._doc(session).status, before)

    def test_preview_is_capped_for_a_long_list(self):
        session, names = self._session_with_groups(60)
        for name in names:
            self._edited_by_someone_else("Customer Group", name)

        preview = self._preview(session)

        self.assertEqual((preview["records"], preview["blocked"]), (60, 60))
        sample = preview["by_doctype"][0]["sample"]
        self.assertEqual(len(sample), import_undo.MAX_NAMED + 1)
        self.assertEqual(sample[-1], "(50 more)")
        blockers = preview["blockers"][0]
        self.assertEqual(blockers["count"], 60)
        self.assertEqual(len(blockers["sample"]), import_undo.MAX_NAMED + 1)
        self.assertEqual(blockers["sample"][-1]["record"], "(50 more)")

    def test_preview_caps_the_doctype_and_kept_lists(self):
        session = self._a_kept_group_session()  # Customers, their group kept for alpha
        top, child = f"Undo Top {self.tag}", f"Undo Child {self.tag}"
        for name, parent, is_group in ((top, "All Customer Groups", 1), (child, top, 0)):
            frappe.get_doc(
                {
                    "doctype": "Customer Group",
                    "customer_group_name": name,
                    "parent_customer_group": parent,
                    "is_group": is_group,
                }
            ).insert()
            self._add(session, "Customer Group", name, parent)
        self._edited_by_someone_else("Customer Group", child)  # blocked, so its parent is kept

        with patch.object(import_undo, "MAX_LISTED", 1):
            preview = self._preview(session)

        self.assertEqual(preview["kept"], 2)
        self.assertEqual(len(preview["by_doctype"]), 2)
        self.assertEqual(preview["by_doctype"][-1]["doctype"], "(1 more)")
        self.assertEqual(preview["by_doctype"][-1]["count"], 3)
        self.assertEqual(len(preview["kept_records"]), 2)
        self.assertEqual(preview["kept_records"][-1], {"record": "(1 more)", "kept_because": None})
        self.assertEqual(preview["by_doctype"][-1]["sample"], [])

    def test_preview_refuses_a_session_past_the_window_with_the_reason(self):
        session = self._session_with_customers()
        self._set_step_time(session, add_to_date(now_datetime(), hours=-1))

        with patch.dict(frappe.conf, {"fac_import_undo_days": 0}):
            preview = self._preview(session)

        self.assertFalse(preview["can_undo"])
        self.assertIn("0 days", preview["refused"])
        self.assertEqual(preview["window"]["days"], 0)
        self.assertIsNone(self._stored_fingerprint(session))
        # the refusal has the shape of every preview, with empty values
        self.assertEqual(
            {key: preview[key] for key in ("records", "blocked", "kept", "already_removed")},
            {"records": 0, "blocked": 0, "kept": 0, "already_removed": 0},
        )
        self.assertEqual(
            (
                preview["by_doctype"],
                preview["blockers"],
                preview["kept_records"],
                preview["import_type_note"],
            ),
            ([], [], [], None),
        )

    def test_preview_refuses_without_write_permission_on_the_session(self):
        session, _names = self._session_with_groups(1)
        frappe.set_user(self.make_throwaway_user("undo", roles=("Sales User",)))

        reply = self._run("preview_import_undo", {"session": session})

        self.assertFalse(reply["success"])
        self.assertEqual(reply["result"]["error_type"], "permission_error")

    def test_preview_does_not_change_the_session_status(self):
        session = self._a_kept_group_session()
        before = self._doc(session)

        self._preview(session)
        after = self._doc(session)

        self.assertEqual(after.status, before.status)
        self.assertEqual(len(after.steps), len(before.steps))
        self.assertTrue(self._exists("Customer", self._names()["alpha"]))

    def test_preview_notes_an_update_session(self):
        session = self._session_with_customers()
        doc = self._doc(session)
        frappe.db.set_value(
            "Data Import", doc.data_imports[-1].data_import, "import_type", "Update Existing Records"
        )
        doc.db_set("import_type", "Update Existing Records")

        preview = self._preview(session)

        self.assertFalse(preview["can_undo"])
        self.assertEqual(preview["records"], 0)
        self.assertIn("never deleted", preview["import_type_note"])
        self.assertIn("never deleted", preview["refused"])

    def test_preview_of_a_session_that_is_all_blocked_cannot_undo(self):
        session, names = self._session_with_groups(1)
        self._edited_by_someone_else("Customer Group", names[0])

        preview = self._preview(session)

        self.assertFalse(preview["can_undo"])
        self.assertEqual((preview["records"], preview["blocked"]), (1, 1))
        self.assertTrue(preview["refused"])

    def test_preview_says_submitted_documents_go_with_their_ledger_entries(self):
        session, _names = self._session_with_groups(1)
        plain = self._preview(session)
        self._journal_entry(session, nowdate())  # submitted

        with self._books_open():
            preview = self._preview(session)

        self.assertNotIn("ledger", plain["next"])
        self.assertEqual((preview["records"], preview["blocked"]), (2, 0))
        self.assertIn("1 of them", preview["next"])
        self.assertIn("cancelled, then deleted", preview["next"])
        self.assertIn("ledger entries", preview["next"])

    def test_preview_counts_records_already_removed(self):
        session, names = self._session_with_groups(2)
        frappe.db.delete("Customer Group", {"name": names[0]})

        preview = self._preview(session)

        self.assertEqual((preview["records"], preview["already_removed"]), (1, 1))
        self.assertTrue(preview["can_undo"])


class TestUndo(UndoToolsCase):
    def test_undo_refuses_figures_that_do_not_match_the_preview(self):
        session, names = self._session_with_groups(2)
        before = self._doc(session).status
        self._preview(session)

        for records, blocked in ((99, 0), (2, 1)):
            reply = self._undo(session, records, blocked, "all")

            self.assertFalse(reply["success"])
            self.assertIn("preview_import_undo", reply["error"])
        self.assertTrue(all(self._exists("Customer Group", n) for n in names))
        self.assertEqual(self._doc(session).status, before)

    def test_undo_refuses_when_the_records_changed_since_the_preview(self):
        session, names = self._session_with_groups(2)
        preview = self._preview(session)
        # Same count, different records: one removed by hand, another made and listed.
        frappe.db.delete("Customer Group", {"name": names[0]})
        other = frappe.get_doc(
            {
                "doctype": "Customer Group",
                "customer_group_name": f"Other {self.tag}",
                "parent_customer_group": "All Customer Groups",
            }
        ).insert()
        self._add(session, "Customer Group", other.name, "All Customer Groups")
        self.assertEqual(len(self._plan(session).records), preview["records"])

        reply = self._undo(session, preview["records"], preview["blocked"], "all")

        self.assertFalse(reply["success"])
        self.assertIn("changed since", reply["error"])
        self.assertIn("preview_import_undo", reply["error"])
        self.assertTrue(self._exists("Customer Group", names[1]))

    def test_undo_refuses_a_session_that_was_never_previewed(self):
        session, names = self._session_with_groups(2)

        reply = self._undo(session, 2, 0, "all")

        self.assertFalse(reply["success"])
        self.assertIn("preview_import_undo", reply["error"])
        self.assertTrue(all(self._exists("Customer Group", n) for n in names))

    def test_mode_all_with_blockers_refuses_and_names_unblocked(self):
        session, names = self._session_with_groups(2)
        self._outside_customer(names[0])
        before = self._doc(session).status

        reply = self._approved(session, "all")

        self.assertFalse(reply["success"])
        self.assertIn("unblocked", reply["error"])
        self.assertTrue(all(self._exists("Customer Group", n) for n in names))
        self.assertEqual(self._doc(session).status, before)

    def test_mode_unblocked_runs_the_job(self):
        session, names = self._session_with_groups(2)
        self._outside_customer(names[0])

        reply = self._approved(session, "unblocked")

        self.assertTrue(reply["success"], reply)
        result = reply["result"]
        self.assertEqual(
            (result["state"], result["deleted"], result["status"]), ("done", 1, "Partially Undone")
        )
        self.assertEqual(result["blocked"][0]["record"], f"Customer Group {names[0]}")
        self.assertEqual(result["blocked"][0]["reason"], "linked")
        self.assertTrue(self._exists("Customer Group", names[0]))
        self.assertFalse(self._exists("Customer Group", names[1]))
        self.assertEqual(self._doc(session).status, "Partially Undone")

    def test_mode_all_without_blockers_removes_everything(self):
        session, names = self._session_with_groups(2)

        reply = self._approved(session, "all")

        self.assertTrue(reply["success"], reply)
        self.assertEqual((reply["result"]["state"], reply["result"]["deleted"]), ("done", 2))
        self.assertEqual(reply["result"]["status"], "Undone")
        self.assertFalse(any(self._exists("Customer Group", n) for n in names))

    def test_the_job_deletes_nothing_when_the_records_changed_since_the_approval(self):
        session, names = self._session_with_groups(2)
        outside = self._outside_customer(names[0])
        before = self._doc(session).status
        preview = self._preview(session)
        with patch.object(import_undo.frappe, "enqueue") as enqueue:  # approved; the job has not run
            reply = self._undo(session, preview["records"], preview["blocked"], "unblocked")
        self.assertTrue(reply["success"], reply)
        self.assertEqual(enqueue.call_count, 1)
        # Before the job runs, the user cancels the linking document: the blocker is gone.
        frappe.db.set_value("Customer", outside, "docstatus", 2, update_modified=False)
        self.assertEqual(self._plan(session).blocked, [])

        kwargs = enqueue.call_args.kwargs
        import_undo.undo_job(kwargs["session_name"], kwargs["user"], kwargs["mode"])

        self.assertTrue(all(self._exists("Customer Group", n) for n in names))
        doc = self._doc(session)
        self.assertEqual(doc.status, before)
        self.assertEqual((doc.steps[-1].step, doc.steps[-1].outcome), ("Undo", "Failed"))
        self.assertIn("changed since the approval", doc.steps[-1].message)
        report = import_undo.report(doc)
        self.assertEqual(report["state"], "stopped")
        self.assertIn("preview again", report["reason"])

    def test_undo_reports_progress_on_a_second_call_and_does_not_run_twice(self):
        session, names = self._session_with_groups(2)
        preview = self._preview(session)

        with patch.object(import_undo, "_job_running", return_value=True), patch.object(
            import_undo.frappe, "enqueue"
        ) as first:
            started = self._undo(session, preview["records"], preview["blocked"], "all")
        self.assertEqual(first.call_count, 1)
        self.assertTrue(started["success"], started)
        self.assertEqual(started["result"]["state"], "running")
        self.assertEqual(self._doc(session).status, "Undoing")

        with patch.object(import_undo, "_job_running", return_value=True), patch.object(
            import_undo.frappe, "enqueue"
        ) as again:
            second = self._undo(session, preview["records"], preview["blocked"], "all")
            third = self._undo(session, 0, 0, "unblocked")  # whatever it echoes: it only reports

        again.assert_not_called()
        for reply in (second, third):
            self.assertTrue(reply["success"], reply)
            self.assertEqual((reply["result"]["state"], reply["result"]["status"]), ("running", "Undoing"))
        self.assertTrue(all(self._exists("Customer Group", n) for n in names))

    def test_a_second_call_racing_the_first_reports_the_progress(self):
        session, names = self._session_with_groups(2)
        preview = self._preview(session)
        real_start = import_undo.start

        def raced(doc, mode, fingerprint=None):
            # Another undo_import call started the undo after this one loaded the session.
            frappe.db.set_value(
                "FAC Import Session",
                doc.name,
                {"status": "Undoing", "progress": import_undo._progress("queued")},
            )
            return real_start(doc, mode, fingerprint=fingerprint)

        with patch.object(import_undo, "start", side_effect=raced), patch.object(
            import_undo, "_job_running", return_value=True
        ), patch.object(import_undo.frappe, "enqueue") as enqueue:
            reply = self._undo(session, preview["records"], preview["blocked"], "all")

        enqueue.assert_not_called()
        self.assertTrue(reply["success"], reply)
        self.assertEqual((reply["result"]["state"], reply["result"]["status"]), ("running", "Undoing"))
        self.assertTrue(all(self._exists("Customer Group", n) for n in names))

    def test_undo_after_the_job_reports_the_result(self):
        session, names = self._session_with_groups(2)
        preview = self._preview(session)
        self.assertTrue(self._undo(session, preview["records"], preview["blocked"], "all")["success"])
        self.assertEqual(self._doc(session).status, "Undone")

        with patch.object(import_undo.frappe, "enqueue") as enqueue:
            reply = self._undo(session, preview["records"], preview["blocked"], "all")

        enqueue.assert_not_called()
        self.assertTrue(reply["success"], reply)
        self.assertEqual(
            (reply["result"]["state"], reply["result"]["status"], reply["result"]["deleted"]),
            ("done", "Undone", 2),
        )

    def test_a_partially_undone_session_reports_the_result_for_the_old_figures(self):
        session, names = self._session_with_groups(2)
        self._outside_customer(names[0])
        preview = self._preview(session)
        self.assertTrue(self._undo(session, preview["records"], preview["blocked"], "unblocked")["success"])

        with patch.object(import_undo.frappe, "enqueue") as enqueue:
            reply = self._undo(session, preview["records"], preview["blocked"], "unblocked")

        enqueue.assert_not_called()
        self.assertTrue(reply["success"], reply)
        self.assertEqual((reply["result"]["state"], reply["result"]["deleted"]), ("done", 1))

    def test_a_stopped_job_is_reported_and_undo_can_be_run_again(self):
        session, names = self._session_with_groups(2)
        preview = self._preview(session)
        with patch.object(import_undo, "_job_running", return_value=True), patch.object(
            import_undo.frappe, "enqueue"
        ):
            self._undo(session, preview["records"], preview["blocked"], "all")

        self._silent_for(session, import_undo.STALL_SECONDS + 60)

        with patch.object(import_undo, "_job_running", return_value=False):
            reply = self._undo(session, preview["records"], preview["blocked"], "all")

        self.assertTrue(reply["success"], reply)
        self.assertEqual(reply["result"]["state"], "stopped")
        self.assertTrue(reply["result"]["reason"])
        self.assertNotEqual(self._doc(session).status, "Undoing")
        self.assertTrue(self._undo(session, preview["records"], preview["blocked"], "all")["success"])
        self.assertFalse(any(self._exists("Customer Group", n) for n in names))

    def _silent_for(self, session, seconds):
        doc = self._doc(session)
        progress = frappe.parse_json(doc.progress)
        progress["updated_on"] = str(add_to_date(now_datetime(), seconds=-seconds))
        doc.db_set("progress", frappe.as_json(progress), update_modified=False)

    def _queue_holds_no_job(self):
        """The queue answers that it has no job (as in production between the enqueue and the
        commit), whether or not Redis is up: _job_running's own code runs."""
        return patch("frappe.utils.background_jobs.is_job_enqueued", return_value=False)

    def test_a_queued_job_is_reported_running_though_the_queue_does_not_hold_it_yet(self):
        # Neither inline nor _job_running patched: the queue has no job.
        session, names = self._session_with_groups(2)
        before = self._doc(session).status
        preview = self._preview(session)

        with patch.object(import_undo.frappe, "enqueue") as enqueue, self._queue_holds_no_job():
            reply = self._undo(session, preview["records"], preview["blocked"], "all")
            followed = self._undo(session, preview["records"], preview["blocked"], "all")

        self.assertEqual(enqueue.call_count, 1)
        for answer in (reply, followed):
            self.assertTrue(answer["success"], answer)
            self.assertEqual((answer["result"]["state"], answer["result"]["status"]), ("running", "Undoing"))
        doc = self._doc(session)
        self.assertEqual(doc.status, "Undoing")
        self.assertNotEqual(doc.status, before)
        self.assertNotIn("Undo", [step.step for step in doc.steps])  # no Failed "stopped" step
        self.assertNotIn("stopped", self._undo_option(session))
        self.assertTrue(all(self._exists("Customer Group", n) for n in names))

    def test_a_progress_call_long_after_a_job_that_never_ran_reports_stopped_and_restores(self):
        session, names = self._session_with_groups(2)
        before = self._doc(session).status
        preview = self._preview(session)
        with patch.object(import_undo.frappe, "enqueue"):
            self._undo(session, preview["records"], preview["blocked"], "all")
        self._silent_for(session, import_undo.STALL_SECONDS + 60)

        with patch.object(import_undo.frappe, "enqueue") as enqueue, self._queue_holds_no_job():
            reply = self._undo(session, 0, 0, "unblocked")

        enqueue.assert_not_called()
        self.assertTrue(reply["success"], reply)
        self.assertEqual((reply["result"]["state"], reply["result"]["status"]), ("stopped", before))
        self.assertIn("stopped", reply["result"]["reason"])
        self.assertNotIn("already undone", reply["result"]["message"])
        self.assertEqual(self._doc(session).status, before)
        self.assertTrue(all(self._exists("Customer Group", n) for n in names))

    def test_a_dead_rerun_of_a_partially_undone_session_is_stopped_not_already_undone(self):
        session, names = self._session_with_groups(2)
        outside = self._outside_customer(names[0])
        first = self._approved(session, "unblocked")
        self.assertEqual(first["result"]["status"], "Partially Undone")
        frappe.delete_doc("Customer", outside, force=True, ignore_permissions=True)  # the blocker is gone
        preview = self._preview(session)  # what is left, previewed again
        self.assertEqual((preview["records"], preview["blocked"]), (1, 0))
        with patch.object(import_undo.frappe, "enqueue"):
            rerun = self._undo(session, preview["records"], preview["blocked"], "unblocked")
        self.assertEqual(rerun["result"]["state"], "running")
        self._silent_for(session, import_undo.STALL_SECONDS + 60)

        with self._queue_holds_no_job():
            reply = self._undo(session, preview["records"], preview["blocked"], "unblocked")

        self.assertTrue(reply["success"], reply)
        self.assertEqual(reply["result"]["state"], "stopped")
        self.assertNotIn("already undone", reply["result"]["message"])
        self.assertEqual(reply["result"]["status"], "Partially Undone")

    def test_another_tools_rewrite_of_the_options_keeps_the_fingerprint(self):
        session, names = self._session_with_groups(2)
        preview = self._preview(session)
        fingerprint = self._stored_fingerprint(session)
        # A mapping save rewrites template_options (and moves the status to Mapped).
        mapped = self._run(
            "set_column_mapping", {"session": session, "mapping": MAPPING, "doctype": "Customer"}
        )
        self.assertTrue(mapped["success"], mapped)
        self.assertEqual(self._stored_fingerprint(session), fingerprint)
        self.assertTrue(fingerprint)

        reply = self._undo(session, preview["records"], preview["blocked"], "all")

        self.assertTrue(reply["success"], reply)
        self.assertEqual(reply["result"]["state"], "done")
        self.assertFalse(any(self._exists("Customer Group", n) for n in names))

    def test_undo_refuses_an_undone_session(self):
        session, names = self._session_with_groups(2)
        preview = self._preview(session)
        self._doc(session).db_set("status", "Undone")  # no result stored: nothing to report

        reply = self._undo(session, preview["records"], preview["blocked"], "all")

        self.assertFalse(reply["success"])
        self.assertIn("already undone", reply["error"])
        self.assertTrue(all(self._exists("Customer Group", n) for n in names))

    def test_undo_refuses_a_session_past_the_window(self):
        session = self._session_with_customers()
        preview = self._preview(session)
        self._set_step_time(session, add_to_date(now_datetime(), hours=-1))

        with patch.dict(frappe.conf, {"fac_import_undo_days": 0}):
            reply = self._undo(session, preview["records"], preview["blocked"], "all")

        self.assertFalse(reply["success"])
        self.assertIn("0 days", reply["error"])

    def test_undo_refuses_without_write_permission_on_the_session(self):
        session, names = self._session_with_groups(1)
        frappe.set_user(self.make_throwaway_user("undo", roles=("Sales User",)))

        reply = self._undo(session, 1, 0, "all")

        self.assertFalse(reply["success"])
        self.assertEqual(reply["result"]["error_type"], "permission_error")
        frappe.set_user("Administrator")
        self.assertTrue(self._exists("Customer Group", names[0]))

    def test_undo_refuses_when_every_record_is_blocked(self):
        session, names = self._session_with_groups(1)
        self._edited_by_someone_else("Customer Group", names[0])
        before = self._doc(session).status

        reply = self._approved(session, "unblocked")

        self.assertFalse(reply["success"])
        self.assertIn("nothing", reply["error"].lower())
        self.assertEqual(self._doc(session).status, before)
        self.assertTrue(self._exists("Customer Group", names[0]))

    def test_the_reply_lists_are_capped(self):
        session, names = self._session_with_groups(3)
        for name in names:
            self._edited_by_someone_else("Customer Group", name)
        frappe.get_doc(
            {
                "doctype": "Customer Group",
                "customer_group_name": f"Spare {self.tag}",
                "parent_customer_group": "All Customer Groups",
            }
        ).insert()
        self._add(session, "Customer Group", f"Spare {self.tag}", "All Customer Groups")

        with patch.object(import_undo, "MAX_LISTED", 2):
            reply = self._approved(session, "unblocked")

        self.assertTrue(reply["success"], reply)
        blocked = reply["result"]["blocked"]
        self.assertEqual(len(blocked), 3)  # 2 and "(1 more)"
        self.assertEqual(blocked[-1], {"record": "(1 more)", "reason": None, "detail": None})


class TestRegistration(UndoToolsCase):
    def test_tools_are_registered(self):
        self.assertIn("preview_import_undo", CorePlugin().get_tools())
        self.assertIn("undo_import", CorePlugin().get_tools())
        for name in ("preview_import_undo", "undo_import"):
            tool = self.registry.get_tool(name)
            self.assertIsNotNone(tool, name)
            self.assertIsNone(tool.requires_permission)

    def test_undo_import_is_a_write_tool_and_the_preview_is_not(self):
        self.assertEqual(detect_tool_category(self.registry.get_tool("undo_import")), "write")
        self.assertEqual(detect_tool_category(self.registry.get_tool("preview_import_undo")), "read_only")

    def test_the_descriptions_carry_the_guidance(self):
        preview = self.registry.get_tool("preview_import_undo").description
        undo = self.registry.get_tool("undo_import").description
        self.assertIn("undo_import", preview)
        self.assertIn("blocked", preview)
        for word in (
            "preview_import_undo",
            "records",
            "blocked",
            "mode",
            "unblocked",
            "background",
            "restored",
            "ledger entries",
        ):
            self.assertIn(word, undo)


# What an Undone session is told by each path that imports. The opening engines' sessions and
# start_import only say the sheet "isn't ready" and point to dry_run_import, which then says the
# session is Undone: true, but it does not name the status.
NAMES_THE_STATUS = {
    "dry_run_import": True,
    "start_import": False,
    "set_opening_stock": True,
    "set_opening_invoices": True,
    "set_opening_balances": True,
    "import_opening_stock": False,
    "import_opening_invoices": False,
    "import_opening_balances": False,
    "set_column_mapping": True,
}


class TestUndoneSession(UndoToolsCase):
    """Every path that imports refuses an Undone session. The importing paths needed no change:
    each already allows only the statuses it can work from. set_column_mapping, which would set
    the session back to Mapped, refuses an Undoing or Undone session."""

    def _engine(self, session, key, **settings):
        doc = self._doc(session)
        options = frappe.parse_json(doc.template_options or "{}") or {}
        options[key] = {"company": self.company, "columns": {}, **settings}
        doc.db_set("template_options", frappe.as_json(options), update_modified=False)

    def _calls(self):
        today = nowdate()
        company = self.company
        return {
            "dry_run_import": (None, {}),
            "start_import": (None, {"doctype": "Customer", "rows": 3, "import_type": "Insert New Records"}),
            "set_opening_stock": (
                None,
                {"company": company, "posting_date": today, "columns": {"Item": "item_code"}},
            ),
            "set_opening_invoices": (
                None,
                {
                    "company": company,
                    "invoice_type": "Sales",
                    "cut_off": today,
                    "columns": {"Invoice": "invoice_no"},
                },
            ),
            "set_opening_balances": (
                None,
                {"company": company, "cut_off": today, "columns": {"Account": "account"}},
            ),
            "import_opening_stock": (
                ("opening_stock", {"posting_date": today}),
                {"company": company, "posting_date": today, "rows": 1, "total_qty": 1, "total_value": 1},
            ),
            "import_opening_invoices": (
                ("opening_invoices", {"invoice_type": "Sales", "cut_off": today}),
                {"company": company, "invoice_type": "Sales", "rows": 1, "total_amount": 1},
            ),
            "import_opening_balances": (
                ("opening_balances", {"cut_off": today}),
                {"company": company, "cut_off": today, "rows": 1, "total_debit": 1, "total_credit": 1},
            ),
            "set_column_mapping": (None, {"mapping": MAPPING, "doctype": "Customer"}),
        }

    def test_an_undone_session_cannot_be_imported_again(self):
        for tool, (engine, arguments) in self._calls().items():
            with self.subTest(tool=tool):
                session = self._session_with_customers()
                self._approved(session, "all")
                self.assertEqual(self._doc(session).status, "Undone")
                if engine:
                    self._engine(session, engine[0], **engine[1])

                reply = self._run(tool, {"session": session, **arguments})

                self.assertFalse(reply["success"], reply)
                self.assertTrue(reply["error"])
                self.assertEqual("Undone" in reply["error"], NAMES_THE_STATUS[tool], reply["error"])
                self.assertEqual(self._doc(session).status, "Undone")
                self.assertEqual(self._imported(), set())

    def test_an_undoing_session_cannot_be_remapped(self):
        session, _names = self._session_with_groups(1)
        self._doc(session).db_set("status", "Undoing")
        before = self._doc(session).template_options

        reply = self._run(
            "set_column_mapping", {"session": session, "mapping": MAPPING, "doctype": "Customer"}
        )

        self.assertFalse(reply["success"], reply)
        self.assertIn("Undoing", reply["error"])
        self.assertIn("new import session", reply["error"])
        self.assertEqual(reply["result"]["session"], session)
        doc = self._doc(session)
        self.assertEqual((doc.status, doc.template_options), ("Undoing", before))
