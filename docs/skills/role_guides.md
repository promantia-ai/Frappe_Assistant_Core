# Role Guides

## Overview

A short guide per role ("Sales executive", "Storekeeper") for that role's daily tasks **as they work on this site**: the mandatory fields this site added, the approval workflow, the naming series and the print format. It is not the ERPNext manual. Each guide is kept as a FAC Skill of type **Role Guide**, shared with the role, so it survives chat retention and you follow it whenever a user of that role asks how to do one of those tasks.

## When to use

- The consultant asks for a guide or training notes for a role: "write a guide for the sales team", "training for storekeepers".
- A user asks how to do a task ("how do I make an invoice?") and a role guide covering it is listed among your skills: read it and follow it.
- A guide you open starts with **"This guide is out of date"**.

## Writing a guide

1. **The role and its tasks.** Ask which role and which daily tasks, if not said. Name the DocTypes ("Quotation, Sales Order, Sales Invoice"). Check the role can do them: `get_doctype_info` on each lists `permissions` per role. A task the role can't create or submit is not in its guide; say so.
2. **Read the setup**, never from memory: `get_doctype_info` with `doctype` and `"setup": true` for each DocType. `setup` gives:
   - `mandatory`: fields the user must fill in. `custom: 1` = this site added it. `mandatory_depends_on` / `depends_on` = only under a condition: say the condition in words ("when Customer Type is Company").
   - `tables`: mandatory fields of each child table row (the items).
   - `naming`: the series to choose and the default.
   - `workflow`: the states, who acts (`allowed`) and what each action does. Say who approves and what the user does while waiting.
   - `default_print_format` and `print_formats`: which one to print or email.
3. **Write it** in markdown, one section per task, numbered steps in the order the form asks for them. Start each task with the link to the new form: `[New Sales Invoice](/app/sales-invoice/new)`. Name fields by their label. Keep it to what this role does every day; leave out what the system fills in.
4. **Show it to the consultant** and change what they ask. Then save it with `create_document` on `FAC Skill`:
   - `skill_id`: `role-guide-<role>` in lowercase with hyphens, for example `role-guide-sales-executive`.
   - `title`: "<Role> guide".
   - `description`: who it is for and every task it covers, for example "Guide for Sales Users on this site: quotations, sales orders and sales invoices, with the fields, approvals and print formats this site uses". You find the guide later by this line, so name the tasks.
   - `skill_type`: `Role Guide`. `guide_doctypes`: the DocTypes, one per line. `content`: the guide.
   - `status`: `Published`, `visibility`: `Shared`, `shared_with_roles`: `[{"role": "<the role>"}]`.
   Saving records the setup the guide was written from.
5. **A copy to share or print:** `generate_document` with the guide's `content` and the title "<Role> guide".

## Using a guide

When a user asks how to do a task a guide covers, follow the guide's steps rather than general ERPNext knowledge, and give the link to the form. If they give you the values, you may fill a draft for them with `create_document` (it stays a draft for them to check and submit).

## When a guide is out of date

A guide opens with **"This guide is out of date"** and the list of what changed (a new mandatory field, a new workflow state, a new print format) when the setup changed after it was written. Tell the user what changed, then answer from the current setup (`get_doctype_info` with `setup`). Offer the consultant to update the guide: re-read the setup, change the affected steps, and save the guide with `update_document` on its FAC Skill (`content`). Saving records the new setup and the note goes away.

## Gotchas

- **Only fields from `setup`.** Never add a field because ERPNext usually has it.
- **A guide is for a role, not a person.** Share it with the role, never a user.
- **One guide per role.** Update the existing one (`list_documents` on `FAC Skill`, filter `skill_type = "Role Guide"`) rather than writing a second.
- Publishing and sharing a skill needs System Manager or Assistant Admin.
