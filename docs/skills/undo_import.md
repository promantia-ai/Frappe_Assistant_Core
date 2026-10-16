# How to Use undo_import

## Overview

Deletes the records an import session created, in the background, after **one approval**. Submitted documents (opening invoices, opening stock, the opening Journal Entry) are cancelled first, then deleted, which also removes their own ledger entries (their GL, Payment Ledger and Stock Ledger entries): tell the user so before they approve. It is the step after `preview_import_undo`: only for records the user has seen previewed, and only when the user asks to undo the import.

The audit trail stays: the session, its steps, the Data Imports and their logs. Only the business records are removed. A session that was fully undone cannot be imported again; the user starts a new import session.

**Deleted records cannot be restored.** Say so before the user approves.

## Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `session` | string | **Yes** | The import session id |
| `records` | integer | **Yes** | The preview's `records` |
| `blocked` | integer | **Yes** | The preview's `blocked` |
| `mode` | string | **Yes** | `all`, or `unblocked` |

The approval card shows `records`, `blocked` and `mode`, so the user sees what goes. They must match what would be removed now, and the records must be those previewed, or nothing is deleted: preview again.

- `all`: remove everything. Refused when anything is blocked.
- `unblocked`: remove everything that can go, and keep the blocked records and the records they depend on. Use it only when the user chose it.

## Workflow

1. Call `preview_import_undo` and show the user what it says (see that skill).
2. Ask the user to confirm, and for the mode when something is blocked.
3. Call `undo_import` with the preview's `records` and `blocked` and the mode. The user approves the card.
4. **Follow it.** The undo runs in the background. Call `undo_import` again to see how far it got: while it runs it only reports, with any values, and never starts a second undo. The reply has `state`:
   - `running`: `deleted` of `total` removed so far. Tell the user, and check again in a minute or so.
   - `done`: the end. The session's `status` is **Undone** (nothing kept) or **Partially Undone** (something kept).
   - `stopped`: the job ended early, or did not start and has shown no progress for 10 minutes, or found that the records changed between the approval and its start (then it removed nothing); `reason` says why. What it removed stays removed. After a new preview the user can undo again for the rest.
5. **Report the end:** how many records were removed (`deleted`), how many are kept (`kept`), and the blocked records with their reasons (`blocked`). Say what the user can do about each reason (a linked document can be cancelled by them, then the undo run again after a new preview).

## Reply

```json
{
  "success": true,
  "session": "IMP-2026-00012",
  "status": "Partially Undone",
  "state": "done",
  "deleted": 247,
  "already_removed": 1,
  "total": 248,
  "kept": 1,
  "blocked": [{"record": "Customer Acme Ltd", "reason": "linked", "detail": "Sales Invoice SINV-0042 links to it."}],
  "blocked_count": 1,
  "removed": ["Customer Beta Traders", "Customer Gamma Stores", "(245 more)"],
  "already_removed_records": ["Customer Delta Mart"],
  "reason": null,
  "message": "Removed 247 records; 1 kept. 1 can't be removed. 1 were already removed by hand."
}
```

The `blocked` list shows the first 50 records and ends with one entry whose `record` is "(N more)" (its `reason` and `detail` are null) when longer; `blocked_count` is the full number.

`removed` names the records the undo removed ("DocType name"), the first 50 and then "(N more)". The session keeps the list after the undo, so a later call still says what went; when a Partially Undone session is undone again, the list keeps what the earlier passes removed and adds this pass's, while `deleted` counts this pass only. `already_removed` counts records of the import that were deleted by hand before the undo reached them (they are not an error), and `already_removed_records` names them, capped the same way.

## When it can't start

- **The figures don't match, or the records changed since the preview:** call `preview_import_undo` again, show the user the new figures, and pass those.
- **Never previewed:** call `preview_import_undo` first.
- **Mode `all` with blocked records:** ask the user, and use `unblocked` if they want the rest removed.
- **Nothing can be removed** (every record is blocked, or it is an update import): tell the user why.
- **Still importing, past the time window** (`fac_import_undo_days` in site config, default 30, counted from the last import step), **or already undone:** the error says which.
- **No permission** on the session, or on a DocType: the records the user may not delete are blocked (`no_permission`), and the job runs as the user who approved it.

## Anti-patterns

- Calling it without a preview, or with figures the user has not seen.
- Choosing `unblocked` for the user.
- Calling `delete_document` per record instead.
- Starting a new import session to "redo" an import before the old one is undone: the records would be duplicated.
