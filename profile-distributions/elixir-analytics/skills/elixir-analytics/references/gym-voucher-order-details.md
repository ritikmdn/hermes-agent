# Gym + voucher marketplace order details

Use this when Slack asks for gym/voucher orders with order value, card amount, rewards redeemed, and rewards earned.

## Route

1. Start with the standard `answer_question` runner. If it falls through, use the planner and then `scripts/run-ad-hoc-query.ts` with a Supabase `AdHocQueryRequest`.
2. If the wording says "gym and voucher orders" and does not name partners, run a small partner-code probe over successful marketplace orders in the same date window before final detail. In June 2026 data, the relevant partner codes were:
   - Gym: `ELIXIR-GYM`
   - Vouchers: `ELIXIR-GYFTER` / `GYFTER`
3. If the user asks for **all** gym orders in a window, do not use the normal Slack list cap of 25. Run the ad hoc runner with a high bounded cap such as `--max-rows 5000`, keep the SQL ordered deterministically, and still create/use the temporary dashboard link. In Slack, show a compact table of all returned rows only when the row count is small enough to stay readable; otherwise summarize and point to the dashboard/export.
4. Return order-level rows plus a compact summary and dashboard link.

## Default semantics

- Date window: for "last week", use the last completed 7 Asia/Kolkata business days unless the user asks for calendar week or week-to-date.
- Date window: for bare month names inside an ongoing month thread, default to the current conversation year and the full Asia/Kolkata calendar month; state the year explicitly in the answer.
- Successful placed orders:
  - `marketplace_order.payment_status IN ('SUCCESS', 'CONFIRMED')`
  - `coalesce(order_status::text, 'CONFIRMED') NOT IN ('CANCELLED','REFUND','REFUNDED','FAILED','PENDING')`
  - Exclude deleted profiles: `coalesce(profiles.is_deleted, false) = false`
- Order value: `marketplace_order.total_amount`.
- Amount paid via card: `greatest(coalesce(total_amount, 0) - coalesce(reward_redemption, 0), 0)`.
- Rewards redeemed: `marketplace_order.reward_redemption` for the order.
- Rewards earned: `sum(rewards.credit_amount)` where `rewards.marketplace_order_id = marketplace_order.id`.
- Caveat: reward credits not linked by `marketplace_order_id` will not appear as earned for the order.
- Gross/net: this is a gross successful-order view; show refund amount separately and do not net refunds unless asked.

## SQL shape

Use a bounded read-only query with:

```sql
WITH bounds AS (...),
rewards_by_order AS (
  SELECT marketplace_order_id,
         sum(coalesce(credit_amount, 0)) AS rewards_earned,
         sum(coalesce(debit_amount, 0)) AS rewards_redeemed_ledger,
         max(updated_at) AS rewards_freshness
  FROM rewards
  WHERE marketplace_order_id IS NOT NULL
  GROUP BY marketplace_order_id
),
base AS (
  SELECT
    CASE
      WHEN mo.partner_code = 'ELIXIR-GYM' THEN 'gym'
      WHEN mo.partner_code IN ('ELIXIR-GYFTER','GYFTER') THEN 'voucher'
      ELSE lower(coalesce(mo.partner_code, 'unknown'))
    END AS order_type,
    mo.id AS order_id,
    mo.partner_order_id,
    mo.partner_code,
    mo.created_at AT TIME ZONE 'Asia/Kolkata' AS order_time_ist,
    trim(concat_ws(' ', p.first_name, p.last_name)) AS customer,
    CASE
      WHEN mo.partner_code = 'ELIXIR-GYM' THEN
        nullif(concat_ws(' - ', mo.order_details #>> '{gym_providers,name}', mo.order_details ->> 'variant_label'), ' - ')
      WHEN mo.partner_code IN ('ELIXIR-GYFTER','GYFTER') THEN
        coalesce(
          mo.order_details #>> '{vouchers,0,PullVouchers,0,VoucherName}',
          mo.order_details #>> '{vouchers,0,ProductName}',
          mo.order_details #>> '{gyfter_brand,brand_name}',
          mo.order_details #>> '{itemsRequested,0,name}',
          'voucher'
        )
      ELSE mo.partner_code
    END AS product,
    round(mo.total_amount::numeric, 2) AS order_value,
    round(greatest(coalesce(mo.total_amount,0) - coalesce(mo.reward_redemption,0), 0)::numeric, 2) AS card_paid,
    round(coalesce(mo.reward_redemption,0)::numeric, 2) AS rewards_redeemed,
    round(coalesce(rbo.rewards_earned,0)::numeric, 2) AS rewards_earned,
    mo.payment_status,
    mo.order_status::text AS order_status,
    round(coalesce(mo.refund_amount,0)::numeric, 2) AS refund_amount,
    greatest(mo.updated_at, coalesce(rbo.rewards_freshness, mo.updated_at)) AS freshness
  FROM marketplace_order mo
  CROSS JOIN bounds b
  JOIN profiles p ON p.id = mo.user_id AND coalesce(p.is_deleted, false) = false
  LEFT JOIN rewards_by_order rbo ON rbo.marketplace_order_id = mo.id
  WHERE mo.created_at >= b.start_ts
    AND mo.created_at < b.end_ts
    AND mo.partner_code IN ('ELIXIR-GYM','ELIXIR-GYFTER','GYFTER')
    AND mo.payment_status IN ('SUCCESS','CONFIRMED')
    AND coalesce(mo.order_status::text, 'CONFIRMED') NOT IN ('CANCELLED','REFUND','REFUNDED','FAILED','PENDING')
)
SELECT ...
FROM base
ORDER BY order_time_ist DESC, order_type, order_value DESC;
```

## Slack answer format

- Lead with a compact summary table by `order_type`.
- Then provide order-level detail in a fenced code-block table; include customer and product but keep raw order IDs in the dashboard unless explicitly needed.
- Include metric contracts: `gmv`, `marketplace_orders`, `rewards_redeemed`, `rewards_credited`.
- Include sources: `marketplace_order`, `profiles`, `rewards`.
- Include date window, timezone, freshness, assumptions, caveats, and dashboard URL.
