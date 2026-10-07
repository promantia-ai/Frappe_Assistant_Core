# How to Use set_column_mapping

## Overview

The `set_column_mapping` tool saves which column of the user's migration file goes to which ERPNext field, on the file's import session. The session keeps it across the conversation, so the mapping is still there on later turns. It is saved in Data Import's own format, so the import later uses it as it stands.

Get the fields from `get_import_schema` first.

## Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `session` | string | **Yes** | The import session id |
| `mapping` | object | **Yes** | Column → field. Only the columns to set or change. |
| `doctype` | string | No | The target DocType. Needed the first time; changing it clears the mapping. |

In `mapping`:
- **A column** is its header exactly as in the file (`"Cust Name"`), or its 0-based position (`"3"`) when two columns share a header.
- **A field** is a fieldname (`"customer_name"`), a child table field as `"<table fieldname>.<fieldname>"` (`"barcodes.barcode"`), or `"Don't Import"` to skip the column. An exact label also works.

## Response Format

```json
{
  "success": true,
  "session": "IMP-2026-00012",
  "doctype": "Item",
  "mapping": [
    {"column": "Code", "position": 0, "field": "item_code", "label": "Item Code"},
    {"column": "Group", "position": 1, "field": "item_group", "label": "Item Group"}
  ],
  "child_tables": {
    "Barcodes (barcodes)": [
      {"column": "EAN", "position": 3, "field": "barcodes.barcode", "label": "Barcode"}
    ]
  },
  "not_imported": ["Internal Ref"],
  "unmapped_columns": ["Old Code"],
  "missing_required": [],
  "summary": "3 columns mapped, 1 not imported, 1 unmapped."
}
```

If anything is wrong, **nothing is saved**. `problems` says what to fix, for example `'item_nme' is not an importable field of Item. Did you mean 'item_name' (Item Name)?`, and `columns` lists the file's headers. Fix those entries and call again.

## Best Practices

1. **Show the result as a table**: *Your column → ERPNext field*. Show each child table in its own table under its title. Below, mark the **unmapped columns** and the **required fields with no column** clearly.
2. **Don't guess.** When you aren't confident about a column, leave it out of the call and ask the user with a choice card (`ask_user`), offering the likely fields and "Don't import". Then save their answer.
3. **Corrections are one call.** When the user says "that's the billing city, not the shipping city", send just that column with its new field. The other columns keep their mapping. Confirm the change in a sentence.
4. **A required field with no column** needs a decision: map a column to it, rely on its default if it has one, or the user adds the column to the file.
5. **Changing the target DocType** clears the mapping, because the fields differ. Say so before you do it.
