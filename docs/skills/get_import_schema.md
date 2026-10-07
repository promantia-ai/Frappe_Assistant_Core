# How to Use get_import_schema

## Overview

The `get_import_schema` tool lists the fields a data import can fill on a DocType. Use it when a user wants to move a file from their old system into ERPNext: it tells you what each column can map to. Save the mapping with `set_column_mapping`.

Use this, not `get_doctype_info`, for imports. `get_doctype_info` returns a DocType's entire metadata, which for a large DocType (Delivery Note, Sales Invoice) is too long to read in full, so fields get missed. This returns only importable fields: no layout, hidden or read-only fields.

## Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `doctype` | string | **Yes** | The DocType the file's rows become, e.g. `"Customer"`, `"Item"` |

## Response Format

```json
{
  "success": true,
  "doctype": "Item",
  "naming": {"rule": "from the item_code field", "id_field": "name", "id_note": "Map a column to 'name' to update existing records."},
  "fields": [
    {"fieldname": "item_code", "label": "Item Code", "type": "Data", "required": true},
    {"fieldname": "item_group", "label": "Item Group", "type": "Link", "link": "Item Group", "required": true},
    {"fieldname": "stock_uom", "label": "Default Unit of Measure", "type": "Link", "link": "UOM", "required": true}
  ],
  "child_tables": [
    {
      "fieldname": "barcodes", "label": "Barcodes", "doctype": "Item Barcode",
      "fields": [
        {"fieldname": "barcode", "label": "Barcode", "type": "Data", "required": true},
        {"fieldname": "barcode_type", "label": "Barcode Type", "type": "Select", "options": ["EAN", "UPC-A", "..."]}
      ]
    }
  ],
  "map_as": "Map a column to a field by its fieldname, to a child table field as '<table fieldname>.<fieldname>', or to \"Don't Import\"."
}
```

- **`required`** with a **`default`**: ERPNext fills it in if no column maps to it, so it isn't missing (e.g. Customer's `customer_type` defaults to `Company`).
- **`link`**: the value must be an existing record of that DocType (e.g. an Item Group that exists).
- **`options`**: a Select field accepts only these values. Very long lists are cut to 25, with `options_total`.
- **`child_tables`**: rows inside the record, such as an Item's barcodes. Map their columns as `barcodes.barcode`.

## When it fails

It fails up front, with the reason, when the import can't happen. Tell the user and stop:
- the user lacks **Create** or **Import** permission on the DocType (`error_type: "permission_error"`)
- the DocType is a child table (the error names the parent to import instead), a settings DocType, or has imports turned off
- the DocType doesn't exist (`suggestions` lists close names, e.g. "Custmer" → "Customer")

## Best Practices

1. **Work out the DocType from the file first.** Look at the headers and sample rows of the attached file. Tell the user what you think it is and why, e.g. "These look like **Customers**: there's a name, a GSTIN and a customer group." Then call this tool.
2. **Match columns by meaning, not exact label.** "Cust Name" → `customer_name`, "Party" → the customer or supplier name, "GSTIN No." → the GSTIN field. Use labels, types and options to decide.
3. **Check the child tables.** A column like "Barcode" on an item list belongs in `barcodes`, not on the Item itself.
4. **Some data is its own DocType in ERPNext.** A customer's addresses and contacts are separate **Address** and **Contact** records, not fields on Customer. Say so, and import them as a separate step.

## Related

- `set_column_mapping` saves the mapping on the import session and shows what's still missing.
