# How to Use go_live_test_run

## Overview

The `go_live_test_run` tool runs the client's main flows end to end before go-live, on the client's own setup, and reports each step that fails with ERPNext's reason. It finds the setup gaps before the client does: a missing default account, no tax template, a numbering clash, an approval workflow, a permission.

**Nothing is kept.** The whole run is rolled back at the end, and what a submit would send out (emails, background jobs) is held back. There is nothing to delete afterwards: never offer to clean up, and if the user asks, say nothing was kept.

## The flows

Buying runs first, so the stock it receives is there to sell:

1. **Buy:** Material Request → Purchase Order → Purchase Receipt → Purchase Invoice → Payment Entry
2. **Sell:** Quotation → Sales Order → Delivery Note → Sales Invoice → Payment Entry

Each document is made from the one before with ERPNext's own "Make" functions and submitted as the user. A step after a failed one is `not run`.

## Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `company` | string | No | The company to test. Default: the user's default company (asked when the site has several) |
| `item` | string | No | Item code. Default: the first stock item that is both bought and sold, without batches or serial numbers |
| `customer` | string | No | Default: the first enabled customer |
| `supplier` | string | No | Default: the first enabled supplier not on hold |
| `warehouse` | string | No | Default: Stock Settings' default warehouse, else the company's first warehouse |
| `qty` | number | No | Default 1 |
| `rate` | number | No | Default: the price list's; 100 when it has none |
| `flows` | array | No | `["buy"]`, `["sell"]` or both (the default) |

## Reading the result

- `ready`: every step passed.
- `used`: the company, item, customer, supplier and warehouse the run took. Say which, so the user knows what was tested.
- `steps`: per step `flow`, `step` (the DocType), `status` (`passed`, `failed`, `not run`) and, for a failure, `message`. A passed step has the `amount` and the number of `taxes` rows it got: a sale with no taxes on a taxed business is worth pointing out.
- `notes`: things the run had to fill in, such as an item with no price.

## Workflow

1. Run the Go-Live Readiness report first (`get_skill("site-health-check")`, Go-live section) and fix its Fail items: a missing fiscal year or default account fails every step here too.
2. Run `go_live_test_run`. Show one table: Flow | Step | Result | Why.
3. For each failure, explain it in business words and offer the fix through `get_skill("setup-change-protocol")`. Then run the test again.
4. A step blocked by an approval workflow is not a setup gap if the client wants that approval: say so.
