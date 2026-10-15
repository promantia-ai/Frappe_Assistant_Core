# frappe_assistant_core/tests/test_import_opening_balances.py
"""
Opening account balances (#33998): the old system's closing trial balance becomes one opening
Journal Entry. This file covers the setup and its pre-checks; the row checks, the balance check,
creation and the report are added with their own tasks.
"""

import unittest
from unittest.mock import patch

import frappe
from frappe.utils import nowdate

from frappe_assistant_core.core.tool_registry import get_tool_registry
from frappe_assistant_core.plugins.core import import_dry_run as dry_run
from frappe_assistant_core.plugins.core import import_opening_balances as opening_balances
from frappe_assistant_core.plugins.core import import_opening_invoices as opening_invoices
from frappe_assistant_core.plugins.core import import_opening_stock as opening_stock
from frappe_assistant_core.tests.base_test import BaseAssistantTest

HEADER = "Account,Debit,Credit,Balance"
COLUMNS = {"Account": "account", "Debit": "debit", "Credit": "credit"}


@unittest.skipUnless("erpnext" in frappe.get_installed_apps(), "needs ERPNext's Journal Entry")
class OpeningBalancesCase(BaseAssistantTest):
    """A company with a Temporary Opening account and a few ledger accounts, made for each test."""

    def setUp(self):
        super().setUp()
        frappe.set_user("Administrator")
        self.registry = get_tool_registry()
        self.tag = frappe.generate_hash(length=6)
        self.company = frappe.get_all("Company", pluck="name", order_by="creation", limit=1)[0]
        self.abbr = frappe.get_cached_value("Company", self.company, "abbr")
        self.date = nowdate()
        if not opening_invoices.in_fiscal_year(self.date, self.company):
            self.skipTest("today is in no fiscal year of the test company")
        self.files_before = set(frappe.get_all("File", pluck="name"))
        self.made_accounts, self.made_entries = [], []
        self.temporary = self._temporary_account()
        self.bank = self._account("Bank", "Asset", "Bank")
        self.equity_group = self._equity_group()
        self.capital = self._account("Capital", "Equity")
        self.expense = self._account("Rent", "Expense")
        self.debtors = self._account("Debtors", "Asset", "Receivable")
        self.creditors = self._account("Creditors", "Liability", "Payable")

    def tearDown(self):
        frappe.set_user("Administrator")
        for name in self.made_entries:
            if frappe.db.get_value("Journal Entry", name, "docstatus") == 1:
                frappe.get_doc("Journal Entry", name).cancel()
            frappe.delete_doc("Journal Entry", name, force=True, ignore_permissions=True)
        for name in reversed(self.made_accounts):
            try:
                frappe.delete_doc("Account", name, force=True, ignore_permissions=True)
            except Exception:
                frappe.clear_messages()
        for name in set(frappe.get_all("File", pluck="name")) - self.files_before:
            frappe.delete_doc("File", name, force=True, ignore_permissions=True)
        super().tearDown()

    # -- fixtures ------------------------------------------------------------------------

    def _temporary_account(self):
        name = opening_invoices.temporary_account(self.company)
        if name:
            return name
        return self._account("Temporary Opening", "Asset", "Temporary")

    def _equity_group(self):
        """The company's Equity group; some charts have no Equity root, so one is made."""
        name = frappe.db.get_value(
            "Account", {"company": self.company, "root_type": "Equity", "is_group": 1}, order_by="lft"
        )
        if name:
            return name
        doc = frappe.get_doc(
            {
                "doctype": "Account",
                "account_name": f"Equity {self.tag}",
                "company": self.company,
                "is_group": 1,
                "root_type": "Equity",
                "report_type": "Balance Sheet",
            }
        ).insert(ignore_mandatory=True)
        self.made_accounts.append(doc.name)
        return doc.name

    def _account(self, title, root_type, account_type=None, **extra):
        """A ledger account of the test company: a new one named for this test run."""
        parent = frappe.db.get_value(
            "Account", {"company": self.company, "root_type": root_type, "is_group": 1}, order_by="lft"
        )
        doc = frappe.get_doc(
            {
                "doctype": "Account",
                "account_name": f"{title} {self.tag}",
                "company": self.company,
                "parent_account": parent,
                "account_type": account_type,
                "is_group": 0,
                **extra,
            }
        ).insert()
        self.made_accounts.append(doc.name)
        return doc.name

    def _run(self, tool_name, arguments):
        return self.registry.get_tool(tool_name)._safe_execute(arguments)

    def _file(self, lines, header=HEADER):
        content = ("\n".join([header, *lines]) + "\n").encode()
        return frappe.get_doc(
            {"doctype": "File", "file_name": f"balances-{self.tag}.csv", "content": content, "is_private": 1}
        ).insert()

    def _session(self, lines=None, header=HEADER):
        lines = lines if lines is not None else [f"{self.bank},100,,", f"{self.capital},,100,"]
        started = self._run("start_import_session", {"file_id": self._file(lines, header).name})
        self.assertTrue(started["success"], started)
        return started["result"]["session_id"]

    def _setup(self, session, **changes):
        arguments = {
            "session": session,
            "company": self.company,
            "cut_off": self.date,
            "columns": dict(COLUMNS),
            **changes,
        }
        return self._run("set_opening_balances", arguments)

    def _doc(self, session):
        return frappe.get_doc("FAC Import Session", session)


class TestSetup(OpeningBalancesCase):
    def test_maps_columns_and_stores_settings(self):
        session = self._session()
        reply = self._setup(session)
        self.assertTrue(reply["success"], reply)
        doc = self._doc(session)
        self.assertEqual(doc.status, "Mapped")
        self.assertEqual(doc.target_doctype, "Journal Entry")
        stored = opening_balances.settings(doc)
        self.assertEqual(stored["company"], self.company)
        self.assertEqual(stored["cut_off"], self.date)
        self.assertEqual(stored["temporary_opening_account"], self.temporary)
        self.assertEqual(
            stored["currency"], frappe.get_cached_value("Company", self.company, "default_currency")
        )
        self.assertEqual(stored["profit_and_loss"], "stop")
        self.assertEqual(stored["columns"], {})
        mapping = reply["result"]["column_to_field_map"]
        self.assertEqual(mapping["0"], "accounts.account")
        self.assertEqual(mapping["1"], "accounts.debit_in_account_currency")
        self.assertEqual(mapping["2"], "accounts.credit_in_account_currency")
        self.assertEqual(mapping["3"], "Don't Import")
        self.assertTrue(opening_balances.is_opening_balances(doc))

    def test_signed_balance_is_kept_in_settings_not_mapped(self):
        session = self._session([f"{self.bank},,,100"])
        reply = self._setup(session, columns={"Account": "account", "Balance": "balance"})
        self.assertTrue(reply["success"], reply)
        self.assertEqual(opening_balances.settings(self._doc(session))["columns"], {"balance": 3})
        mapping = reply["result"]["column_to_field_map"]
        self.assertEqual(mapping["0"], "accounts.account")
        self.assertEqual(mapping["3"], "Don't Import")

    def test_every_wrong_column_is_listed_at_once(self):
        session = self._session()
        reply = self._setup(session, columns={"Nope": "account", "Debit": "bogus"})
        self.assertFalse(reply["success"])
        error = frappe.as_json(reply)
        self.assertIn("Nope", error)
        self.assertIn("bogus", error)
        self.assertIn("each of: account", error)

    def test_needs_an_amount_column(self):
        session = self._session()
        reply = self._setup(session, columns={"Account": "account"})
        self.assertFalse(reply["success"])
        self.assertIn("balance", frappe.as_json(reply))

    def test_refuses_wrong_settings(self):
        session = self._session()
        for changes, words in (
            ({"profit_and_loss": "ignore"}, "retained_earnings"),
            ({"cut_off": "soon"}, "isn't a date"),
            ({"cut_off": "1990-01-31"}, "fiscal year"),
            ({"company": "No Such Company"}, "doesn't exist"),
            ({"profit_and_loss": "retained_earnings"}, "Equity"),
            ({"profit_and_loss": "retained_earnings", "retained_earnings_account": "No Such"}, "Equity"),
            ({"profit_and_loss": "retained_earnings", "retained_earnings_account": self.expense}, "Equity"),
        ):
            reply = self._setup(session, **changes)
            self.assertFalse(reply["success"], changes)
            self.assertIn(words, frappe.as_json(reply))

    def test_retained_earnings_must_be_a_ledger_account(self):
        reply = self._setup(
            self._session(),
            profit_and_loss="retained_earnings",
            retained_earnings_account=self.equity_group,
        )
        self.assertFalse(reply["success"])
        self.assertIn("Equity", frappe.as_json(reply))

    def test_retained_earnings_account_is_stored(self):
        session = self._session()
        reply = self._setup(
            session, profit_and_loss="retained_earnings", retained_earnings_account=self.capital
        )
        self.assertTrue(reply["success"], reply)
        stored = opening_balances.settings(self._doc(session))
        self.assertEqual(stored["profit_and_loss"], "retained_earnings")
        self.assertEqual(stored["retained_earnings_account"], self.capital)

    def test_refuses_a_company_without_a_temporary_account(self):
        session = self._session()
        with patch.object(opening_balances, "temporary_account", return_value=None):
            reply = self._setup(session)
        self.assertFalse(reply["success"])
        self.assertIn("Temporary", frappe.as_json(reply))

    def test_refuses_without_create_and_submit_permission(self):
        session = self._session()
        real = frappe.has_permission

        def no_submit(doctype=None, ptype="read", *args, **kwargs):
            if doctype == "Journal Entry" and ptype == "submit":
                return False
            return real(doctype, ptype, *args, **kwargs)

        with patch.object(opening_balances.frappe, "has_permission", side_effect=no_submit):
            reply = self._setup(session)
        self.assertFalse(reply["success"])
        self.assertIn("Accounts Manager", frappe.as_json(reply))

    def test_refuses_a_company_with_a_submitted_period_closing_voucher(self):
        session = self._session()
        real = frappe.db.exists

        def closed(doctype=None, *args, **kwargs):
            if doctype == "Period Closing Voucher":
                return "PCV-0001"
            return real(doctype, *args, **kwargs)

        with patch.object(opening_balances.frappe.db, "exists", side_effect=closed):
            reply = self._setup(session)
        self.assertFalse(reply["success"])
        self.assertIn("Period Closing Voucher", frappe.as_json(reply))

    def _opening_entry(self):
        entry = frappe.get_doc(
            {
                "doctype": "Journal Entry",
                "voucher_type": "Opening Entry",
                "company": self.company,
                "posting_date": self.date,
                "accounts": [
                    {"account": self.bank, "debit_in_account_currency": 5},
                    {"account": self.capital, "credit_in_account_currency": 5},
                ],
            }
        )
        entry.insert()
        entry.submit()
        self.made_entries.append(entry.name)
        return entry

    def test_refuses_a_date_with_an_opening_entry_already(self):
        entry = self._opening_entry()
        reply = self._setup(self._session())
        self.assertFalse(reply["success"])
        error = frappe.as_json(reply)
        self.assertIn("already", error)
        self.assertIn(entry.name, error)

    def test_this_sessions_own_entry_is_not_a_conflict(self):
        entry = self._opening_entry()
        session = self._session()
        self.assertFalse(self._setup(session)["success"])
        self.assertIsNone(
            opening_balances.setup_problem(
                self.company, self.date, "stop", None, session=self._with_created(session, entry.name)
            )
        )

    def _with_created(self, session, entry_name):
        doc = self._doc(session)
        options = frappe.parse_json(doc.template_options or "{}") or {}
        options["opening_balances"] = {"created": {entry_name: {}}}
        doc.template_options = frappe.as_json(options)
        return doc

    def test_dry_run_goes_to_this_engine(self):
        session = self._session()
        self._setup(session)
        self.assertIs(dry_run.special_engine(self._doc(session)), opening_balances)

    def test_start_import_refuses_naming_the_right_tool(self):
        session = self._session()
        self._setup(session)
        reply = self._run(
            "start_import",
            {"session": session, "doctype": "Journal Entry", "rows": 2, "import_type": "Insert New Records"},
        )
        self.assertFalse(reply["success"])
        self.assertIn("import_opening_balances", frappe.as_json(reply))

    def test_engines_name_their_own_import_tools(self):
        self.assertEqual(opening_stock.IMPORT_TOOL, "import_opening_stock")
        self.assertEqual(opening_invoices.IMPORT_TOOL, "import_opening_invoices")
        self.assertEqual(opening_balances.IMPORT_TOOL, "import_opening_balances")

    def test_setup_again_on_a_mapped_session(self):
        session = self._session()
        self.assertTrue(self._setup(session)["success"])
        again = self._setup(
            session, profit_and_loss="retained_earnings", retained_earnings_account=self.capital
        )
        self.assertTrue(again["success"], again)
        self.assertEqual(
            opening_balances.settings(self._doc(session))["profit_and_loss"], "retained_earnings"
        )

    def test_refuses_a_company_or_cut_off_change_once_created(self):
        session = self._session()
        self.assertTrue(self._setup(session)["success"])
        doc = self._with_created(session, "ACC-JV-NONE")
        stored = opening_balances.settings(self._doc(session))
        stored["created"] = {"ACC-JV-NONE": {}}
        options = frappe.parse_json(doc.template_options)
        options["opening_balances"] = stored
        doc.template_options = frappe.as_json(options)
        doc.save()
        other_date = frappe.utils.add_days(self.date, -1)
        reply = self._setup(session, cut_off=other_date)
        self.assertFalse(reply["success"])
        self.assertIn("already", frappe.as_json(reply))
        reply = self._setup(session, company="No Such Company")
        self.assertFalse(reply["success"])

    def test_refuses_a_session_that_made_invoices_or_stock(self):
        for key in ("opening_stock", "opening_invoices"):
            session = self._session()
            doc = self._doc(session)
            options = frappe.parse_json(doc.template_options or "{}") or {}
            options[key] = {"created": {"X": 1}}
            doc.template_options = frappe.as_json(options)
            doc.save()
            reply = self._setup(session)
            self.assertFalse(reply["success"], key)
            self.assertIn("new session", frappe.as_json(reply))
