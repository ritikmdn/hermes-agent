# Order status lookup pattern

Use this reference when a Slack user asks for the status of a specific order id / order UUID.

## Route

1. Run the standard first-pass analytics question runner with the exact Slack question.
2. If it falls through to `requires_model_request`, run the planner. These questions typically route to `supabase_ad_hoc`.
3. Use the ad hoc runner, not manual SQL-only output, so the final Slack answer has the structured metadata contract and a dashboard link.

## Default interpretation

Treat the supplied id as any of:

- `marketplace_order.id`
- `marketplace_order.partner_order_id`
- `customer_vouchers.id`
- `customer_vouchers.order_id`
- `customer_vouchers.payment_transaction_id`

This keeps marketplace and voucher/gym-style order references covered without asking a clarification question first.

## SQL shape

```sql
WITH input AS (
  SELECT '<order_id>'::text AS order_id
),
marketplace_matches AS (
  SELECT
    'marketplace_order'::text AS source_table,
    mo.id::text AS matched_record_id,
    mo.partner_order_id::text AS partner_order_id,
    mo.user_id::text AS user_id,
    concat_ws(' ', p.first_name, p.last_name) AS user_name,
    mo.payment_status::text AS payment_status,
    mo.order_status::text AS order_status,
    NULL::text AS voucher_status,
    mo.partner_code::text AS partner_code,
    mo.total_amount::numeric AS total_amount,
    mo.refund_amount::numeric AS refund_amount,
    mo.created_at AS created_at_utc,
    mo.updated_at AS updated_at_utc
  FROM marketplace_order mo
  LEFT JOIN profiles p ON p.id = mo.user_id
  CROSS JOIN input i
  WHERE mo.id::text = i.order_id
     OR mo.partner_order_id::text = i.order_id
),
voucher_matches AS (
  SELECT
    'customer_vouchers'::text AS source_table,
    cv.id::text AS matched_record_id,
    cv.order_id::text AS partner_order_id,
    cv.customer_id::text AS user_id,
    concat_ws(' ', p.first_name, p.last_name) AS user_name,
    cv.payment_status::text AS payment_status,
    NULL::text AS order_status,
    cv.status::text AS voucher_status,
    NULL::text AS partner_code,
    cv.purchase_amount::numeric AS total_amount,
    cv.refund_amount::numeric AS refund_amount,
    cv.purchased_at AS created_at_utc,
    cv.updated_at AS updated_at_utc
  FROM customer_vouchers cv
  LEFT JOIN profiles p ON p.id = cv.customer_id
  CROSS JOIN input i
  WHERE cv.id::text = i.order_id
     OR cv.order_id::text = i.order_id
     OR cv.payment_transaction_id::text = i.order_id
)
SELECT * FROM marketplace_matches
UNION ALL
SELECT * FROM voucher_matches
ORDER BY source_table, created_at_utc DESC NULLS LAST
LIMIT 25;
```

## Metadata defaults

- `resultType`: start with `table`. If the runner returns rows but no dashboard link because it is an exact small table, rerun as `breakdown` with equivalent SQL and compact metadata.
- `metricIds`: `[]` / metric contract `none`; this is an operational lookup, not a business metric.
- `sources`: `marketplace_order`, `customer_vouchers`, `profiles`.
- `dateWindow`: all-time point lookup for the supplied id.
- `timezone`: UTC timestamps returned unless converting for display.
- Caveat: if the id belongs to an order system outside these tables, the query returns no rows.

## Slack answer shape

Lead with the operational status, then a compact code-block table. Include the dashboard link and fine print.

Example:

```text
Order <id> is **DELIVERED**.

field             value
source_table      marketplace_order
partner_order_id  <partner_order_id>
payment_status    SUCCESS
order_status      DELIVERED
refund_amount     <amount>
updated_at_utc    <timestamp>
```
