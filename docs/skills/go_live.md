# Go Live

## Overview

Taking a company live goes through six steps, the **go-live checklist**: discovery, the setup plan, the chart of accounts, the data migration, the go-live test run and hypercare. The checklist on FAC Admin and the Desk banner show which are done; they read the site's own records, so a step counts as done once its work is done, not when you say so.

The user usually arrives from the banner's **Start** or **Continue**, with a message naming the step ("Let's do the chart of accounts for Acme"). Do that step, start it with the tool below, and stay on it.

## When to use

- "Take Acme live", "continue where we stopped", "what's the next go-live step?".
- A message from the checklist naming a step: "Let's start with discovery…", "Let's do the data migration for…".

## The steps and where each starts

| Step | Start with | Done when |
|---|---|---|
| Discovery | `setup_plan` with action discover, then `get_skill("guided-setup")` | a setup plan is saved |
| Setup plan | show the plan, then `run_setup_plan` once the user approves | the plan ends Completed |
| Chart of accounts | `get_skill("import-chart-of-accounts-usage")`: the chart's check with `dry_run_import`, then `import_chart_of_accounts`; or the user keeps ERPNext's chart | the chart is on the company |
| Data migration | `get_skill("migrate-data")`: `plan_migration`, then `run_migration_plan` once the user approves | the plan ends Completed or Partially Completed |
| Go-live test run | `get_skill("go-live-test-run-usage")`: `go_live_test_run`, then the Go-Live Readiness report | the test run passes and no readiness check fails |
| Hypercare | `get_skill("hypercare")` | a FAC Hypercare is active |

Steps don't have to be done in order: the user may start any of them. If the step it builds on isn't done, say so in one line and carry on.

## Staying on the step

- **Do the step the user named, and only that one.** The Go-Live Readiness report belongs to the go-live test run: don't run it for the other steps, and don't work through its warnings (tax templates, letterhead, email) unless the user asks.
- **When the step is done, say which step is next** on the checklist and offer to start it. Don't start it unasked.
- With no step named, start with the first step that isn't done: discovery when the company has no setup plan.
