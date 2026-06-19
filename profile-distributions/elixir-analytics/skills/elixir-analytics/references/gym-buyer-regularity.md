# Gym buyer regularity drilldown

Use this for Slack follow-ups like "check all the gym buyers in the last 30 days and tell me how many were previously regular users".

## Default interpretation

- `gym buyers` = distinct non-deleted users with successful placed `ELIXIR-GYM` marketplace orders.
- Use the successful placed-order filter:
  - `marketplace_order.partner_code = 'ELIXIR-GYM'`
  - `payment_status in ('SUCCESS', 'CONFIRMED')`
  - `coalesce(order_status::text, 'CONFIRMED') not in ('CANCELLED','REFUND','REFUNDED','FAILED','PENDING')`
  - join `profiles` and require `coalesce(profiles.is_deleted, false) = false`
- For "last 30 days", use the last completed 30 Asia/Kolkata calendar days unless the thread context clearly uses a different parent window.
- `previously regular` = before the user's first gym purchase in the window, they had either:
  - at least 3 prior successful card-spend transactions, or
  - at least 1 prior successful/confirmed marketplace order.
- `some prior card usage` = 1-2 prior successful card-spend transactions and no prior successful marketplace order.
- `no prior spend history found` = neither prior criterion above.

## SQL shape

Use a Supabase ad hoc runner request, not manual output. Keep the result as a compact `breakdown` so the runner emits a dashboard link.

Core CTE pattern:

```sql
WITH bounds AS (...),
gym_orders AS (
  SELECT mo.user_id,
         min(mo.created_at) AS first_gym_order_at,
         count(*) AS gym_orders,
         sum(coalesce(mo.total_amount,0)) AS gym_gmv,
         max(mo.updated_at) AS gym_freshness
  FROM marketplace_order mo
  CROSS JOIN bounds b
  JOIN profiles p ON p.id = mo.user_id
   AND coalesce(p.is_deleted,false) = false
  WHERE mo.created_at >= b.start_ts
    AND mo.created_at < b.end_ts
    AND mo.partner_code = 'ELIXIR-GYM'
    AND mo.payment_status IN ('SUCCESS','CONFIRMED')
    AND coalesce(mo.order_status::text,'CONFIRMED') NOT IN
      ('CANCELLED','REFUND','REFUNDED','FAILED','PENDING')
  GROUP BY mo.user_id
),
prior_card AS (
  SELECT go.user_id,
         count(*) AS prior_card_txns,
         sum(coalesce(t.debit_amount,0)) AS prior_card_spend,
         max(t.transaction_timestamp) AS latest_prior_card_txn_at
  FROM gym_orders go
  JOIN transactions t ON t.user_id = go.user_id
  LEFT JOIN marketplace_order mo_recon
    ON t.txn_id LIKE '%_RECON_%'
   AND mo_recon.id::text = split_part(t.txn_id, '_RECON_', 1)
  WHERE t.transaction_timestamp < go.first_gym_order_at
    AND coalesce(t.debit_amount,0) > 0
    AND t.transaction_type != 'B2C'
    AND t.status = 'PAYMENT_SUCCESS'
    AND mo_recon.id IS NULL
  GROUP BY go.user_id
),
prior_marketplace AS (
  SELECT go.user_id,
         count(*) AS prior_marketplace_orders,
         sum(coalesce(mo.total_amount,0)) AS prior_marketplace_gmv,
         max(mo.created_at) AS latest_prior_marketplace_order_at
  FROM gym_orders go
  JOIN marketplace_order mo ON mo.user_id = go.user_id
  WHERE mo.created_at < go.first_gym_order_at
    AND mo.payment_status IN ('SUCCESS','CONFIRMED')
    AND coalesce(mo.order_status::text,'CONFIRMED') NOT IN
      ('CANCELLED','REFUND','REFUNDED','FAILED','PENDING')
  GROUP BY go.user_id
)
SELECT prior_user_status,
       count(*) AS buyers,
       round(100.0 * count(*) / nullif(sum(count(*)) over (),0), 1) AS buyer_share_pct,
       sum(gym_orders) AS gym_orders,
       round(sum(gym_gmv)::numeric,0) AS gym_gmv_inr,
       sum(prior_card_txns) AS prior_card_txns,
       round(sum(prior_card_spend)::numeric,0) AS prior_card_spend_inr,
       sum(prior_marketplace_orders) AS prior_marketplace_orders,
       round(sum(prior_marketplace_gmv)::numeric,0) AS prior_marketplace_gmv_inr,
       max(freshness) AS freshness
FROM (...buyers classification...) buyers
GROUP BY prior_user_status;
```

## Slack answer format

Lead with the answer and denominator: `N of M gym buyers were previously regular users — P%`.

Then use a compact fenced code-block table, not a wide Markdown table:

```text
Prior status                  Buyers  Share   Gym orders  Gym GMV
previously regular                 7   31.8%           9  ₹146,400
some prior card usage              3   13.6%           3   ₹42,300
no prior spend history found      12   54.5%          13  ₹173,100
```

Always include the runner dashboard link and metadata: metric contracts (`marketplace_orders`, `marketplace_users`, `gmv`), source tables, date window, timezone, freshness, assumptions, and caveats.

## Pitfalls

- Do not treat active milestone users as gym buyers unless the user asks for active gym benefit/milestone users.
- Do not use app activity to classify regularity unless explicitly requested.
- Do not finalize from manual SQL or `execute_code` output alone; use `scripts/run-ad-hoc-query.ts` so Slack gets a dashboard link.
- If the runner gives a small table with no dashboard link, rerun as `resultType: "breakdown"` with compact metadata.
