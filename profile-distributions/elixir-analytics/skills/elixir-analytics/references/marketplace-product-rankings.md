# Marketplace product rankings

Use this when a Slack question asks for top marketplace products/items bought.

## Source of truth

- Table: `marketplace_order` joined to `profiles`.
- For generic product rankings ("top products bought/placed"), use a stricter
  placed-order filter:
  - `payment_status in ('SUCCESS', 'CONFIRMED')`
  - `coalesce(order_status::text, 'CONFIRMED') not in ('CANCELLED', 'REFUND',
    'REFUNDED', 'FAILED', 'PENDING')`
  - `coalesce(refund_amount, 0) = 0`
- Exclude deleted users: `coalesce(profiles.is_deleted, false) = false`.
- Product details are not normalized; extract from
  `marketplace_order.order_details::jsonb` using partner-specific JSON paths.
- If the user explicitly asks for gross marketplace payments/GMV, use the metric
  contract's gross successful-payment filter and state that cancelled/refunded
  rows may be included unless net/refund filters are applied.

## Product extraction pattern

Build a `base` CTE from successful marketplace orders, then union
partner-specific line-item CTEs:

- `TATA_1MG`: expand
  `jsonb_array_elements(coalesce(details->'sku_details', '[]'::jsonb))`.
  - Product: `sku->>'name'`
  - Quantity: `sku->>'quantity'`, default `1`
  - Item GMV: `(sku->>'price' or sku->>'mrp') * quantity`
- `ELIXIR-GYFTER` / `GYFTER`: expand `details->'vouchers'`.
  - Product: `v #>> '{PullVouchers,0,VoucherName}'`, then `ProductName`, then
    `gyfter_brand.brand_name`.
  - Quantity: voucher quantity, else `details #>> '{itemsRequested,0,quantity}'`,
    else `1`.
  - Item GMV: voucher denomination, else requested denomination, else order
    total.
- `ELIXIR-GYM`: one line per order.
  - Product: `concat_ws(' - ', details #>> '{gym_providers,name}',
    details->>'variant_label')`.
  - Quantity: `1`
  - Item GMV: order `total_amount`.
- Other partners such as Playo: fallback to product/name fields or
  `partner_code`; one line per order.

## Ranking columns

Return `product_name`, normalized `vendor`, `orders`, `units_bought`,
`gross_gmv_inr`, `latest_purchase_at`, and freshness columns such as
`max_marketplace_order_updated_at` and `max_marketplace_order_created_at`.

Default ranking for generic "top products bought" questions is distinct orders
containing the product: `orders desc, units_bought desc, gross_gmv_inr desc,
product_name`. Use `units_bought desc` only when the user explicitly asks for
top products by units/quantity. For follow-ups framed around unusually high GMV
or "what caused this high GMV", preserve the parent answer's date window and
rank by `gross_gmv_inr desc` first; lead with the concentration summary (e.g.,
top vendor/product share) before the item table.

## Follow-up drilldowns

- For "who purchased these?" after a marketplace product/item breakdown, preserve
  the parent date window, gross/net treatment, and partner extraction logic.
  Return profile display names plus item/vendor/GMV, but omit phone, email, and
  raw IDs unless explicitly requested.
- For "were they regular users?" after a marketplace purchase drilldown, state
  the working definition before querying. If the user does not specify app
  activity or retention, default to prior spend history before the purchase:
  `regular / existing user` = at least 3 prior successful card-spend transactions
  or at least 1 prior successful/confirmed marketplace order; `some prior usage`
  = 1-2 prior successful card-spend transactions; otherwise `no prior spend
  history found`. Include card age when available.
- When checking prior card spend, inline transaction semantics; do not query a
  physical `classified_transactions` table. Exclude marketplace reward
  reconciliation rows and use successful debit/card-spend filters.

## Pitfalls

- 1mg `sku_details.quantity` can create bulk-order outliers. For generic "top
  bought products," rank by distinct orders and show units as a supporting
  column.
- If using window functions on aggregated rows, aggregate first:
  `max(max(updated_at)) over ()`, not `max(updated_at) over ()` inside a grouped
  query.
- State that refunds are not netted unless the successful status changed out of
  `SUCCESS/CONFIRMED`.
- Product names can be partner payload labels, not a canonical product catalog.
- Always answer via the deterministic runner and include the dashboard URL for
  Slack-facing results.
