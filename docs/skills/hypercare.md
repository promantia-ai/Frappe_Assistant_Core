# Hypercare

## Overview

For the first weeks after go-live, a weekly summary of what is going wrong on the site, so the consultant finds problems before the client calls. A **FAC Hypercare** record switches it on: a consultant, a start date and a length (30 days by default). Every week a summary goes to the consultant by email and in the notification bell, with a link that opens it here. When the period is over, hypercare stops on its own.

The summary holds counts and grouped messages only (error titles, failure messages, DocType names, user ids), never a document's data.

## When to use

- "Switch on hypercare", "watch the site for the first month after go-live".
- "What went wrong this week?", "open the hypercare summary", or the consultant arrives from the summary's FAC Chat link.

## Switching it on

System Manager only. `create_document` on `FAC Hypercare` with `consultant` (the consultant's user id; a System Manager), `start_date` (usually the go-live day), `days` (default 30) and, if asked, `stuck_after_days` (default 3: approvals waiting longer are listed). Tell the user when it ends (`end_date`) and that the first summary arrives a week after the start date. To stop early or lengthen it, `update_document` with a new `days`.

## Reading a summary

`generate_report` with `report_name: "Hypercare Summary"` and `filters: {"from_date": ..., "to_date": ...}` (default: the last 7 days). Each row is an area, what happened, how many times, how many users, and a suggested fix:

| Area | What it is |
|---|---|
| Errors | Error Log grouped by title: the same error again and again is one row |
| Failures users hit | What users tried through FAC and was refused, grouped by message. Several users with the same failure = a **training point** |
| Permission refusals | DocTypes users were refused through FAC |
| Drafts never submitted | Day-one documents made in the period and untouched for 3 days |
| Approvals stuck | Workflow approvals waiting longer than the limit, by document type and state |
| Users not logging in | Desk users with no login in the period |
| Failed scheduled jobs / emails | Scheduled Job Log failures by job; emails in error |

Show it as a table, the biggest first, then offer to act: a missing default or setting through `get_skill("setup-change-protocol")`; a permission with `test_as_user` and the staff setup; a training point by updating the role's guide (`get_skill("role-guides")`). For a copy, run it with `"document": 1` and pass the message to `generate_document` (title "Hypercare summary").

## Gotchas

- **A form saved in Desk that fails validation is not logged anywhere**, so "failures users hit" covers what went through FAC only. Say so if the user asks why a Desk failure is missing.
- The summary is for System Managers; the consultant of a hypercare must be one.
- An empty summary is good news: say "nothing to look at", don't search for problems.
