# Guided Setup

## Overview

Sets up an ERPNext site through a conversation about the business, not the ERP. You ask business questions ("do you hold stock?", "more than one branch?"), keep the answers as a **setup plan** (a FAC Setup Plan the user can open and the client can be handed), show it by area, change it as the user wants, and apply it **once, on one approval**.

For a single change ("add one warehouse", "change the numbering") use `get_skill("setup-change-protocol")` instead: a plan is for setting a site up, not for one edit.

## When to use

- "Help me set up ERPNext", "we just installed this, what now?", "set up my company properly", "we're opening a new branch / warehouse".

**Not for** the first-run **setup wizard**. That has to be finished in the browser first, because it creates the first company, its chart of accounts and the first fiscal year. **Not for** importing customers, items or opening balances; that is `get_skill("migrate-data")`.

## Workflow

1. **Start with `setup_plan(action="discover")`.** It lists the areas in order with what the site already has. An area with `set: true` is already answered: tell the user in one line ("Financial year 2026-2027 is already set") and ask about it only if they want to change it. For the others, ask the `questions` with `ask_user`, two or three at a time, in plain words. Business questions first, ERP words after ("do your prices for the same item change often?" before "which valuation method?").
   - **Chart of accounts:** if the client has their own, that is a separate step (the chart import), not a plan item.
   - **GST** appears only where India Compliance is installed. Never hand-build GST templates or accounts: it creates them itself.
2. **Save the plan with `setup_plan(action="save")`** once the answers are in. It writes only the plan. Show the result by area, in business language, with item numbers, what the site already has (`already_set`), and the fit-gap list: what ERPNext doesn't cover as asked, with why and what to do. Link the plan. Read `problems`: those items were left out (a field or DocType this site doesn't have), say so.
3. **Change it as the user asks with `setup_plan(action="update")`** ("use FIFO, not moving average", "drop the second warehouse"). Nothing is written to the site while the plan is a Draft.
   **Users and roles** is an area too: when the client gives a staff list, follow `get_skill("staff-setup")` and put the groups in the plan as `staff` (with `send_welcome_email` only if asked). The plan's run creates them.
4. **Customisations are made first, one at a time.** A Custom Field, Property Setter, Workflow or Server Script in the plan is never created by the plan. Create each with `create_document` (its own approval card), then give its name to the plan as `created_record` (`setup_plan(action="update")`). The plan only checks it exists as planned.
5. **Apply it with `run_setup_plan`**: one approval card lists every record by area. If an item changed after the approval, the plan doesn't run and says why. When it ends, say what was applied and what wasn't; the plan stays as the setup record.
6. **Finish with a re-run** of `get_skill("site-health-check")`, and show before-and-after counts.

For something the plan can't hold (a workflow, a print format, a notification), hand off to the matching skill: approvals `set-up-approval-workflow`, numbering `configure-naming-series`, extra fields `add-custom-field`, server rules `server-script`, form behaviour `client-script`, printing `print-format`, reports `custom-report`, alerts `notification-setup`.

## Reference

ERPNext adds the company abbreviation to warehouse, cost centre and account names. With company `Acme Traders` and abbreviation `AT`:

```json
{"doctype": "Warehouse", "data": {"warehouse_name": "Chennai Store",
  "parent_warehouse": "All Warehouses - AT", "company": "Acme Traders", "is_group": 0}}
```
Saved as `Chennai Store - AT`. Group warehouses are for grouping only; stock sits in leaf warehouses.

```json
{"doctype": "Cost Center", "data": {"cost_center_name": "Chennai",
  "parent_cost_center": "Acme Traders - AT", "company": "Acme Traders", "is_group": 0}}
```
The root cost centre is named after the company (`Acme Traders - AT`); the default leaf is `Main - AT`.

```json
{"doctype": "Fiscal Year", "data": {"year": "2027-2028",
  "year_start_date": "2027-04-01", "year_end_date": "2028-03-31"}}
```
An empty `companies` table applies to every company.

```json
{"doctype": "Price List", "data": {"price_list_name": "Wholesale",
  "currency": "INR", "selling": 1, "enabled": 1}}
```

Tax templates **outside India** (`Sales Taxes and Charges Template`, and the Purchase equivalent):

```json
{"doctype": "Sales Taxes and Charges Template", "data": {"title": "VAT 5%",
  "company": "Acme Traders", "is_default": 1,
  "taxes": [{"charge_type": "On Net Total", "account_head": "VAT - AT",
             "rate": 5, "description": "VAT 5%"}]}}
```
`account_head` must be an existing leaf `Account` of that company, normally under Duties and Taxes. Create the account first if it's missing.

## Gotchas

- **India Compliance creates GST tax templates and GST accounts itself** when a company is saved. Never hand-build GST templates; send the user to **GST Settings** if they're missing.
- **A second company:** prefer `create_chart_of_accounts_based_on: "Existing Company"` with `existing_company` set, so it copies the first company's chart. A standard template's name depends on the country. If they want one, let them pick it in Desk, where the list is shown.
- **Decide the valuation method (FIFO or Moving Average) before the first stock transaction.** Once any stock transaction exists, ERPNext refuses to change it in Stock Settings (except for items that set their own).
- **Batch and serial-number tracking** is set per item, and can't be switched once the item has stock transactions.
- **Passwords and logos stay in Desk.** An Email Account's password, or a letterhead image, is something the user sets in the browser. Navigate them there with `browser_navigate_to`, and never ask for a password in chat.
- **Don't design a chart of accounts in chat.** Adding an account under the right parent is fine; restructuring the chart is a job for an accountant.

## Anti-patterns

- One giant plan covering every area. Nobody can review 60 rows.
- Asking ERP-shaped questions ("Which valuation method?") before business ones ("Do your prices for the same item change often?").
- Creating a Company per branch of one legal business.
