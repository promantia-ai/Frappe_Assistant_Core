# How to Use create_document

## Overview

The `create_document` tool creates new Frappe documents (records). It handles field validation, mandatory field checks, and permission verification automatically.

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `doctype` | string | **Yes**, for one record | — | Exact DocType name |
| `data` | object | **Yes**, for one record | — | Field values as key-value pairs |
| `submit` | boolean | No | `false` | Submit after creation (for submittable DocTypes). One record only. Under an active Workflow the document is created as a draft instead |
| `validate_only` | boolean | No | `false` | Validate without saving — use to test data format |
| `documents` | array | **Yes**, for several records | — | Instead of `doctype` and `data`: a list of `{doctype, data}`, up to 100. See **Several records at once** |
| `import_session` | string | No | — | With `documents`: the import session whose missing masters these are. See **Missing masters of an import** |

## Response Format

```json
{
  "success": true,
  "result": {
    "success": true,
    "doctype": "ToDo",
    "name": "TODO-00001",
    "data": { ... full document ... },
    "message": "ToDo 'TODO-00001' created successfully"
  }
}
```

## Best Practices

1. **Check required fields first** — use `get_doctype_info` to see mandatory fields before creating.
2. **Use `validate_only: true` first** — test your data structure without actually creating the document.
3. **Link fields expect the `name` (ID)** — not the display title. Use `search_documents` with `purpose: "link_value"` to find valid values.
4. **Don't set auto-generated fields** — `name`, `creation`, `modified`, `owner` are set automatically.
5. **Handle naming series** — DocTypes with naming series auto-generate names; don't pass `name` unless it uses manual naming.
6. **Use `submit: true` carefully** — only when explicitly requested. Creates and submits in one step. If the submit is refused (a validation, a closed period), the document is kept as a clean draft and `submit_error` says why. If the DocType has an active Workflow, it is created as a draft and not submitted: continue with `run_workflow`, which applies the workflow's approval rules.

## Common Patterns

### Simple document
```json
{
  "doctype": "ToDo",
  "data": {
    "description": "Follow up with client",
    "priority": "Medium",
    "date": "2024-06-15"
  }
}
```

### Document with child table
```json
{
  "doctype": "Sales Invoice",
  "data": {
    "customer": "Grant Plastics Ltd.",
    "items": [
      {
        "item_code": "ITEM-001",
        "qty": 5,
        "rate": 100
      }
    ]
  }
}
```

### Document with a Table MultiSelect field
A Table MultiSelect takes an array of its link values, as Desk sends it. Each value fills the field `get_doctype_info` reports as that table's `link_field`. Objects work too: `[{"user": "jane@example.com"}]`.
```json
{
  "doctype": "User Group",
  "data": {
    "name": "Reviewers",
    "user_group_members": ["jane@example.com", "raj@example.com"]
  }
}
```

### Validate before creating
```json
{
  "doctype": "Customer",
  "data": {
    "customer_name": "New Corp",
    "customer_type": "Company"
  },
  "validate_only": true
}
```

### Create and submit in one step
```json
{
  "doctype": "Journal Entry",
  "data": { ... },
  "submit": true
}
```

## Several records at once

Pass `documents` instead of `doctype` and `data`: one call, so the user approves **once** for all of them.

```json
{
  "documents": [
    {"doctype": "Customer Group", "data": {"customer_group_name": "Hospitals", "parent_customer_group": "All Customer Groups"}},
    {"doctype": "Territory", "data": {"territory_name": "Karnataka", "parent_territory": "All Territories"}},
    {"doctype": "UOM", "data": {"uom_name": "Dozen"}}
  ]
}
```

- **Any DocTypes, up to 100 records.** More than that: split them into several calls.
- **In order:** put a parent before the records under it.
- **All or nothing.** Every record is checked first (permission, restricted fields); then they are created one by one with the same checks as a single record. If one fails, none is kept, and the error names it: "Document 2 (Territory) couldn't be created: … Nothing was created." Fix it, or take it out, and call again. What a save sends out at once can't be taken back, as with one record: a Notification's Slack or SMS message, say.
- **Show the user the list as a table before calling.** The approval card then lists each record with every field it sets (the first 10, the rest on request).
- `validate_only` validates each record on its own and creates none. `submit` is for one record at a time: create a list as drafts, then submit each.

Reply: `{"success": true, "created": [{"doctype": "Customer Group", "name": "Hospitals"}, …], "message": "Created 3 records."}`. When nothing was created: `success: false`, the `error`, and `problems` (each with the record's number `document` and why).

## Missing masters of an import

`dry_run_import` lists the masters a migration file needs that don't exist yet and can be created from their name (`masters_plan`): Customer Groups, Territories, UOMs, Brands and the like. Create them with this tool, `documents` and **`import_session`**:

1. **Show the plan as one table**, with every record: DocType, name, parent, rows it fixes. Let the user change a parent; it must be an existing group of the same tree (e.g. a Customer Group with "Is Group" ticked). Ask once: "Create these 4 records?"
2. **Call once** with each plan entry's `doctype` and `data`, and the session:

   ```json
   {
     "import_session": "IMP-2026-00012",
     "documents": [
       {"doctype": "Customer Group", "data": {"customer_group_name": "Hospitals", "parent_customer_group": "All Customer Groups"}},
       {"doctype": "Market Segment", "data": {"market_segment": "Healthcare"}}
     ]
   }
   ```

3. **Show the change log**: the same table with a Result column. Then call `dry_run_import` again without fixes: the rows that needed them now pass.

With `import_session`, each record must be one of that session's missing masters as its **latest** check planned it: the same DocType and name, only the name and the parent. **Never invent one**; to use another name, map the rows instead. Each record created is listed on the session (**Created Records**), so the session can undo it. Refused, with nothing created, when the file or its mapping changed since the check (check again first), or a record already exists or can't be created here.

A missing record that can't be created has no `create` fix in the check; its group has `cannot_create` saying why. For example: ERPNext names it itself (Customer, Supplier, Warehouse, Account), it needs more than a name (Item, Price List), it is a system record (Currency, User), it is a Company, or the file imports that DocType itself. Offer **map** to an existing record, or **leave out** the rows.

## Edge Cases

- **Submittable DocTypes** are created in Draft state (`docstatus=0`) by default — use `submit: true` or `document_action` tool separately.
- **Mandatory fields** that are missing cause a validation error — check with `get_doctype_info` first.
- **Unique constraints** — if a field has `unique=1`, duplicate values will fail.
- **Permission errors** — the current user must have "create" permission on the DocType. A refusal returns `error_type: "permission_error"` with Frappe's own reason in `error`; it is not a field problem, so do not retry with different values.
- **ToDo** — Frappe decides who may create a ToDo. Before Frappe v16.32.0 / v15.119.0, a user without a role that grants ToDo create (such as System Manager) may create only a ToDo that names them (`allocated_to` or `assigned_by`), so one that names nobody is allocated to them, and one naming someone else is refused; from those releases a user may create any ToDo. Omitting `allocated_to`, as the example above does, therefore works on every release. If Frappe refuses, `error` gives its reason.
- **Default values** — fields with defaults are auto-populated if not specified.
- **Child table rows** — pass as arrays of objects under the child table fieldname. A Table MultiSelect also accepts an array of its link values (see the example above). A plain Table does not, because no single field says what a bare value means; that returns `error_type: "child_table_handling_error"` with the `field`.
- **Restricted fields in a child row** — each row is checked against its child doctype's restricted fields, as on the parent. When you copy rows from another document, leave out system fields such as `owner`, `creation`, `docstatus` and `idx`.
- **Child-table doctypes** — `create_document` with a child doctype such as `Sales Invoice Item` is refused with `error_type: "child_table_doctype"` and the `parent_doctypes` it belongs to. A row exists only inside its parent, so add it with `update_document` on the parent document.

## In a migration plan

When the session is in a migration plan (its `migration_plan` is set), don't ask the user here:
`plan_questions` gathers every question of the plan into one list. Missing masters of a planned session aren't created with `create_document`: answer their question with the `create` fix, and the plan creates them when it runs.
