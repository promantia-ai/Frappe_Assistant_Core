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


class TestReadingLines(OpeningBalancesCase):
    def test_balance_of(self):
        balance_of = opening_balances.balance_of
        self.assertEqual(balance_of({"balance": "1,250.50"}), (1250.5, 0))
        self.assertEqual(balance_of({"balance": "(12)"}), (0, 12))
        self.assertEqual(balance_of({"balance": "-5"}), (0, 5))
        self.assertEqual(balance_of({"debit": "1,000", "credit": ""}), (1000, 0))
        self.assertEqual(balance_of({"debit": "", "credit": "(7)"}), (0, -7))
        self.assertEqual(balance_of({"debit": "3", "credit": "4"}), (3, 4))  # the check reports it
        self.assertEqual(balance_of({"debit": "", "credit": "", "balance": ""}), (0, 0))
        self.assertEqual(balance_of({}), (0, 0))
        with self.assertRaises(ValueError):
            balance_of({"debit": "ten"})
        with self.assertRaises(ValueError):
            balance_of({"balance": "ten"})

    def test_totals_rows(self):
        totals = opening_balances.is_totals_row
        self.assertTrue(totals({"account": "Grand Total", "debit": "500"}))
        self.assertTrue(totals({"account": "Total", "balance": "500"}))
        self.assertTrue(totals({"account": "", "credit": "500"}))
        self.assertFalse(totals({"account": "Cash", "debit": "500"}))
        self.assertFalse(totals({"account": "Total", "debit": ""}))
        self.assertFalse(totals({"account": "", "debit": ""}))

    def test_lines_with_debit_and_credit_and_value_maps(self):
        session = self._session(['Old Bank,"1,250.50",,', ",,,", f'{self.capital},,"1,250.50",'])
        self._setup(session)
        doc = self._doc(session)
        doc.transformation_rules = frappe.as_json(
            {
                "value_maps": [
                    {
                        "parent": "Journal Entry Account",
                        "field": "account",
                        "label": "Account",
                        "from": "Old Bank",
                        "to": self.bank,
                    }
                ]
            }
        )
        doc.save()
        lines = opening_balances.read_lines(doc)
        self.assertEqual([line.row for line in lines], [2, 4])  # the blank row is skipped
        self.assertEqual(lines[0].cells["account"], self.bank)
        self.assertEqual(lines[0].cells["debit"], "1,250.50")
        self.assertEqual(lines[0].cells["credit"], "")
        self.assertEqual(lines[1].cells["credit"], "1,250.50")

    def test_lines_with_a_balance_column(self):
        session = self._session([f"{self.bank},,,100", f"{self.capital},,,(100)"])
        self._setup(session, columns={"Account": "account", "Balance": "balance"})
        lines = opening_balances.read_lines(self._doc(session))
        self.assertEqual([line.cells["balance"] for line in lines], ["100", "(100)"])
        self.assertEqual(lines[0].cells["account"], self.bank)
        self.assertNotIn("debit", lines[0].cells)

    def _find(self, text):
        return opening_balances.find_account(self.company, self.abbr, text)

    def test_find_by_exact_name(self):
        found = self._find(self.bank)
        self.assertEqual(found.name, self.bank)
        self.assertEqual(found.company, self.company)
        self.assertEqual(found.account_type, "Bank")
        self.assertEqual(found.root_type, "Asset")
        self.assertEqual(found.report_type, "Balance Sheet")
        self.assertFalse(found.is_group)
        self.assertFalse(found.disabled)
        self.assertTrue(found.account_currency)

    def test_find_by_name_without_the_abbreviation(self):
        self.assertEqual(self._find(f"Debtors {self.tag}").name, self.debtors)

    def test_find_by_account_number(self):
        number = f"9{self.tag}"
        account = self._account("Numbered", "Asset", account_number=number)
        self.assertEqual(self._find(number).name, account)

    def test_find_by_unique_account_name_when_a_number_is_in_the_name(self):
        account = self._account("Prepaid", "Asset", account_number=f"7{self.tag}")
        self.assertNotEqual(account, f"Prepaid {self.tag} - {self.abbr}")
        self.assertEqual(self._find(f"Prepaid {self.tag}").name, account)

    def test_ambiguous_account_name_is_not_found(self):
        self._account("Twin", "Asset", account_number=f"1{self.tag}")
        self._account("Twin", "Liability", account_number=f"2{self.tag}")
        self.assertIsNone(self._find(f"Twin {self.tag}"))

    def test_nothing_found(self):
        self.assertIsNone(self._find(f"No such account {self.tag}"))
        self.assertIsNone(self._find(""))


class TestCheck(OpeningBalancesCase):
    def setUp(self):
        super().setUp()
        self.stock = self._account("Stock", "Asset", "Stock")
        self.retained = self._account("Retained", "Equity")

    # -- helpers -------------------------------------------------------------------------

    def _check(self, session):
        reply = self._run("dry_run_import", {"session": session})
        self.assertTrue(reply["success"], reply)
        return reply["result"]

    def _ready(self, lines, **setup):
        session = self._session(lines)
        self.assertTrue(self._setup(session, **setup)["success"])
        return session, self._check(session)

    def _groups(self, result):
        return {g["cause"]: g for g in result["groups"]}

    def _counts(self):
        return frappe.db.count("Journal Entry"), frappe.db.count("GL Entry")

    def _ready_lines(self, session):
        """The Ready rows the engine finds, for entry_lines."""
        checker = opening_balances._AccountChecker()
        return opening_balances._check_lines(self._doc(session), checker)

    def _lines_of(self, session):
        return opening_balances.entry_lines(
            opening_balances.settings(self._doc(session)), self._ready_lines(session)
        )

    def _simple(self, line):
        """A file with one row in question plus a capital row."""
        return [line, f"{self.capital},,100,"]

    # -- ready rows ----------------------------------------------------------------------

    def test_ready_rows_and_totals_write_nothing(self):
        before = self._counts()
        session, result = self._ready([f"{self.bank},100,,", f"{self.capital},,100,"])
        self.assertEqual((result["ready"], result["failed"]), (2, 0), result)
        totals = result["opening_balances"]
        self.assertEqual(totals["cut_off"], self.date)
        self.assertEqual((totals["rows"], totals["lines"], totals["covered"]), (2, 2, 0))
        self.assertEqual((totals["total_debit"], totals["total_credit"]), (100, 100))
        self.assertIsNone(totals["balancing"])
        self.assertEqual(totals["profit_and_loss"], "stop")
        self.assertEqual(result["balance"], {"debit": 100, "credit": 100, "difference": 0})
        self.assertEqual(self._doc(session).status, "Ready to Import")
        self.assertEqual(self._counts(), before)

    def test_account_found_without_the_company_suffix(self):
        short = self.bank[: -len(f" - {self.abbr}")]
        _, result = self._ready([f"{short},100,,", f"{self.capital},,100,"])
        self.assertEqual(result["ready"], 2, result)

    def test_balance_column_rows(self):
        session = self._session([f"{self.bank},,,100", f"{self.capital},,,(100)"])
        self.assertTrue(self._setup(session, columns={"Account": "account", "Balance": "balance"})["success"])
        self.assertEqual(self._check(session)["ready"], 2)

    def test_an_all_zero_row_is_ready_and_adds_no_line(self):
        session, result = self._ready(
            [f"{self.bank},100,,", f"{self.capital},,100,", f"{self.retained},0,0,"]
        )
        self.assertEqual((result["ready"], result["failed"]), (3, 0), result)
        self.assertEqual(result["opening_balances"]["lines"], 2)
        self.assertEqual(len(self._lines_of(session)), 2)

    # -- each cause is one group -----------------------------------------------------------

    def _one_group(self, cause, line, **setup):
        _, result = self._ready(self._simple(line), **setup)
        groups = self._groups(result)
        self.assertIn(cause, groups, result)
        self.assertEqual(groups[cause]["count"], 1)
        self.assertEqual(groups[cause]["examples"][0]["row"], 2)
        return result, groups[cause]

    def test_missing_account(self):
        result, group = self._one_group(dry_run.CAUSE_MISSING_LINK, f"No such {self.tag},100,,")
        self.assertEqual(group["value"], f"No such {self.tag}")
        self.assertEqual(group["parent"], "Journal Entry Account")
        self.assertEqual(group["field"], "account")

    def test_account_of_another_company(self):
        real = opening_balances.find_account

        def find(company, abbr, text):
            found = real(company, abbr, text)
            if found and found.name == self.bank:
                found["company"] = "Another Company"
            return found

        with patch.object(opening_balances, "find_account", side_effect=find):
            self._one_group(opening_balances.CAUSE_NOT_THIS_COMPANY, f"{self.bank},100,,")

    def test_group_account(self):
        self._one_group(opening_balances.CAUSE_GROUP_ACCOUNT, f"{self.equity_group},100,,")

    def test_disabled_account(self):
        frappe.db.set_value("Account", self.bank, "disabled", 1)
        self._one_group(opening_balances.CAUSE_DISABLED_ACCOUNT, f"{self.bank},100,,")

    def test_two_disabled_accounts_are_two_groups(self):
        frappe.db.set_value("Account", self.bank, "disabled", 1)
        frappe.db.set_value("Account", self.expense, "disabled", 1)
        _, result = self._ready([f"{self.bank},100,,", f"{self.expense},50,,", f"{self.capital},,150,"])
        disabled = [g for g in result["groups"] if g["cause"] == opening_balances.CAUSE_DISABLED_ACCOUNT]
        self.assertEqual(sorted(g["value"] for g in disabled), sorted([self.bank, self.expense]))

    def test_temporary_opening_account(self):
        self._one_group(opening_balances.CAUSE_TEMPORARY, f"{self.temporary},100,,")

    def test_foreign_currency_account(self):
        company_currency = frappe.get_cached_value("Company", self.company, "default_currency")
        other = "USD" if company_currency != "USD" else "EUR"
        name = self._account("Foreign", "Asset", "Bank", account_currency=other)
        self._one_group(opening_balances.CAUSE_FOREIGN_CURRENCY, f"{name},100,,")

    def test_debit_and_credit_both(self):
        self._one_group(opening_balances.CAUSE_BOTH_SIDES, f"{self.bank},100,30,")

    def test_negative_amount(self):
        self._one_group(opening_balances.CAUSE_NEGATIVE, f"{self.bank},-100,,")

    def test_not_a_number(self):
        _, group = self._one_group(dry_run.CAUSE_BAD_VALUE, f"{self.bank},ten,,")
        self.assertIn("ten", group["message"])

    def test_totals_row(self):
        self._one_group(opening_stock.CAUSE_TOTALS_ROW, "Total,100,100,")

    def test_account_twice_names_the_earlier_row(self):
        _, result = self._ready([f"{self.bank},60,,", f"{self.bank},40,,", f"{self.capital},,100,"])
        group = self._groups(result)[opening_balances.CAUSE_ACCOUNT_TWICE]
        self.assertEqual([e["row"] for e in group["examples"]], [3])
        self.assertIn("2", group["message"])
        self.assertEqual(result["ready"], 2)

    def test_profit_and_loss_stops_by_default(self):
        _, result = self._ready([f"{self.bank},100,,", f"{self.expense},30,,", f"{self.capital},,130,"])
        group = self._groups(result)[opening_balances.CAUSE_PROFIT_AND_LOSS]
        self.assertEqual(group["count"], 1)

    def test_profit_and_loss_folds_into_retained_earnings(self):
        session, result = self._ready(
            [f"{self.bank},100,,", f"{self.expense},30,,", f"{self.capital},,130,"],
            profit_and_loss="retained_earnings",
            retained_earnings_account=self.retained,
        )
        self.assertEqual((result["ready"], result["failed"]), (3, 0), result)
        self.assertEqual(result["opening_balances"]["lines"], 3)  # bank, capital, the folded line
        lines = self._lines_of(session)
        folded = [line for line in lines if line["account"] == self.retained]
        self.assertEqual(len(folded), 1)
        self.assertEqual(folded[0]["debit_in_account_currency"], 30)
        self.assertNotIn(self.expense, [line["account"] for line in lines])

    # -- covered rows and the balancing line ---------------------------------------------

    def test_covered_rows_are_ready_counted_and_not_in_the_entry(self):
        session, result = self._ready(
            [f"{self.bank},100,,", f"{self.debtors},50,,", f"{self.stock},20,,", f"{self.capital},,170,"]
        )
        self.assertEqual((result["ready"], result["failed"]), (4, 0), result)
        self.assertEqual(result["opening_balances"]["covered"], 2)
        accounts = [line["account"] for line in self._lines_of(session)]
        self.assertNotIn(self.debtors, accounts)
        self.assertNotIn(self.stock, accounts)

    def test_balancing_line_for_receivable_and_stock(self):
        session, result = self._ready(
            [f"{self.bank},100,,", f"{self.debtors},50,,", f"{self.stock},20,,", f"{self.capital},,170,"]
        )
        # the entry has debit 100 and credit 170: Temporary Opening takes the 70 debit
        last = self._lines_of(session)[-1]
        self.assertEqual(last["account"], self.temporary)
        self.assertEqual(last["debit_in_account_currency"], 70)
        self.assertEqual(last["credit_in_account_currency"], 0)
        self.assertEqual(
            result["opening_balances"]["balancing"], {"account": self.temporary, "debit": 70, "credit": 0}
        )
        self.assertEqual(result["opening_balances"]["lines"], 3)

    def test_balancing_line_for_payable_is_a_credit(self):
        session, result = self._ready(
            [f"{self.bank},100,,", f"{self.creditors},,40,", f"{self.capital},,60,"]
        )
        last = self._lines_of(session)[-1]
        self.assertEqual(last["account"], self.temporary)
        self.assertEqual(last["debit_in_account_currency"], 0)
        self.assertEqual(last["credit_in_account_currency"], 40)
        self.assertEqual(result["ready"], 3, result)

    def test_balancing_line_mixed(self):
        _, result = self._ready(
            [f"{self.bank},100,,", f"{self.debtors},80,,", f"{self.creditors},,30,", f"{self.capital},,150,"]
        )
        # the entry has debit 100 and credit 150; the covered net is a debit of 50
        self.assertEqual(
            result["opening_balances"]["balancing"], {"account": self.temporary, "debit": 50, "credit": 0}
        )

    def test_only_covered_rows_means_no_lines_and_no_erpnext_call(self):
        with patch.object(opening_balances, "_entry_error") as entry_error:
            _, result = self._ready([f"{self.debtors},50,,", f"{self.creditors},,50,"])
        entry_error.assert_not_called()
        self.assertEqual((result["ready"], result["failed"]), (2, 0), result)
        self.assertEqual(result["opening_balances"]["lines"], 0)
        self.assertEqual(result["opening_balances"]["covered"], 2)

    def test_build_entry(self):
        session, _ = self._ready([f"{self.bank},100,,", f"{self.capital},,100,"])
        entry = opening_balances.build_entry(
            opening_balances.settings(self._doc(session)), self._ready_lines(session)
        )
        self.assertEqual(entry.doctype, "Journal Entry")
        self.assertEqual(entry.voucher_type, "Opening Entry")
        self.assertEqual(entry.is_opening, "Yes")
        self.assertEqual(entry.company, self.company)
        self.assertEqual(str(entry.posting_date), self.date)
        self.assertIn(self.date, entry.user_remark)
        self.assertEqual(len(entry.accounts), 2)
        self.assertFalse(entry.name)

    # -- the balance check ---------------------------------------------------------------

    def test_a_file_that_does_not_balance_stops_everything(self):
        before = self._counts()
        session, result = self._ready([f"{self.bank},100,,", f"{self.capital},,90,"])
        self.assertEqual(result["ready"], 0, result)
        self.assertEqual(result["balance"], {"debit": 100, "credit": 90, "difference": 10})
        group = self._groups(result)[opening_balances.CAUSE_NOT_BALANCED]
        self.assertEqual(group["count"], 2)
        self.assertIn("10", group["message"])
        self.assertEqual(
            len([g for g in result["groups"] if g["cause"] == opening_balances.CAUSE_NOT_BALANCED]), 1
        )
        self.assertEqual(self._doc(session).status, "Dry Run Failed")
        self.assertEqual(self._counts(), before)

    def test_balance_counts_rows_that_fail_for_another_reason(self):
        _, result = self._ready([f"{self.bank},100,,", f"Nowhere {self.tag},,100,"])
        self.assertEqual(result["balance"]["difference"], 0)
        self.assertNotIn(opening_balances.CAUSE_NOT_BALANCED, self._groups(result))

    def test_balance_includes_covered_rows(self):
        _, result = self._ready([f"{self.debtors},100,,", f"{self.capital},,100,"])
        self.assertEqual(result["balance"]["difference"], 0)
        self.assertEqual(result["ready"], 2)

    def test_tolerance_passes_a_rounding_difference(self):
        _, result = self._ready([f"{self.bank},100,,", f"{self.capital},,99.996,"])
        self.assertEqual(result["ready"], 2, result)
        self.assertNotIn(opening_balances.CAUSE_NOT_BALANCED, self._groups(result))

    def test_file_balance_ignores_totals_rows(self):
        session = self._session([f"{self.bank},100,,", f"{self.capital},,100,", "Total,100,100,"])
        self._setup(session)
        self.assertEqual(
            opening_balances.file_balance(self._doc(session)), {"debit": 100, "credit": 100, "difference": 0}
        )

    def test_file_balance_skips_unreadable_numbers(self):
        session = self._session([f"{self.bank},100,,", f"{self.capital},ten,,"])
        self._setup(session)
        self.assertEqual(opening_balances.file_balance(self._doc(session))["debit"], 100)

    # -- the validation ERPNext does itself ------------------------------------------------

    def test_rejected_by_erpnext_fails_every_row_together_and_rolls_back(self):
        period = frappe.get_doc(
            {
                "doctype": "Accounting Period",
                "period_name": f"OB Closed {self.tag}",
                "company": self.company,
                "start_date": self.date,
                "end_date": self.date,
                "closed_documents": [{"document_type": "Journal Entry", "closed": 1}],
            }
        ).insert()
        self.addCleanup(frappe.delete_doc, "Accounting Period", period.name, force=True)
        before = self._counts()
        _, result = self._ready([f"{self.bank},100,,", f"{self.capital},,100,"])
        rejected = self._groups(result)[dry_run.CAUSE_REJECTED]
        self.assertEqual(rejected["count"], 2)
        self.assertEqual(result["ready"], 0)
        self.assertEqual(self._counts(), before)

    def test_a_rejection_patched_in(self):
        error = frappe.ValidationError("No way")
        before = self._counts()
        with patch.object(opening_balances, "_entry_error", return_value=error):
            _, result = self._ready([f"{self.bank},100,,", f"{self.capital},,100,"])
        rejected = self._groups(result)[dry_run.CAUSE_REJECTED]
        self.assertEqual(rejected["count"], 2)
        self.assertIn("No way", rejected["message"])
        self.assertEqual(self._counts(), before)

    def test_erpnext_accepts_the_entry_and_it_is_rolled_back(self):
        session, _ = self._ready([f"{self.bank},100,,", f"{self.capital},,100,"])
        before = self._counts()
        error = opening_balances._entry_error(
            opening_balances.settings(self._doc(session)), self._ready_lines(session)
        )
        self.assertIsNone(error)
        self.assertEqual(self._counts(), before)
