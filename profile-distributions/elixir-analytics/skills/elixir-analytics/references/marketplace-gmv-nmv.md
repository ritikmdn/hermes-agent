# Marketplace GMV + NMV monthly answers

Use this when Slack asks for GMV and NMV/Net GMV for a calendar month or custom window.

## Route

1. Still make the mandatory first `answer_question` runner call with the exact Slack question.
2. If the planner/shortcut routes only to saved topic `marketplace-gmv`, do **not** answer from that saved topic alone when the question also asks for NMV. Build a bounded Supabase ad hoc request with metric ids `gmv` and `net_gmv`.
3. Use `resultType: "breakdown"` even for the small summary table so the runner emits a dashboard link.

## Definitions

- GMV: gross successful marketplace order value at payment time.
  - Source: `marketplace_order`
  - Amount: `SUM(total_amount)`
  - Filter: `payment_status IN ('SUCCESS', 'CONFIRMED')`
  - Date: `created_at AT TIME ZONE 'Asia/Kolkata'`
- NMV / Net GMV: GMV after refunds/cancellations, recorded in the adjustment period.
  - Metric contract: `net_gmv`
  - If the schema does not expose a separate refund timestamp, use `marketplace_order.updated_at` as the adjustment-period proxy and state this caveat.

## SQL shape

Use a compact CTE structure:

```sql
with params as (
  select
    timestamp '<from yyyy-mm-dd> 00:00:00' as from_ts,
    timestamp '<to-exclusive yyyy-mm-dd> 00:00:00' as to_ts
),
gross_orders as (
  select mo.*
  from marketplace_order mo
  left join profiles p on p.id = mo.user_id
  cross join params
  where (mo.created_at at time zone 'Asia/Kolkata') >= params.from_ts
    and (mo.created_at at time zone 'Asia/Kolkata') < params.to_ts
    and mo.payment_status in ('SUCCESS', 'CONFIRMED')
    and coalesce(p.is_deleted, false) = false
),
adjustments as (
  select mo.*,
    case
      when coalesce(mo.refund_amount, 0) > 0 then coalesce(mo.refund_amount, 0)
      when coalesce(mo.payment_status, '') in ('REFUND', 'REFUNDED')
        or coalesce(mo.order_status::text, '') in ('REFUND', 'REFUNDED', 'CANCELLED', 'CANCELED')
      then mo.total_amount
      else 0
    end as adjustment_amount
  from marketplace_order mo
  left join profiles p on p.id = mo.user_id
  cross join params
  where (mo.updated_at at time zone 'Asia/Kolkata') >= params.from_ts
    and (mo.updated_at at time zone 'Asia/Kolkata') < params.to_ts
    and coalesce(p.is_deleted, false) = false
    and (
      coalesce(mo.refund_amount, 0) > 0
      or coalesce(mo.payment_status, '') in ('REFUND', 'REFUNDED')
      or coalesce(mo.order_status::text, '') in ('REFUND', 'REFUNDED', 'CANCELLED', 'CANCELED')
    )
),
summary as (
  select
    coalesce((select sum(total_amount) from gross_orders), 0)::float as gmv,
    coalesce((select sum(adjustment_amount) from adjustments), 0)::float as adjustments,
    (coalesce((select sum(total_amount) from gross_orders), 0) - coalesce((select sum(adjustment_amount) from adjustments), 0))::float as nmv,
    (select count(*) from gross_orders)::int as successful_orders,
    (select count(distinct user_id) from gross_orders where user_id is not null)::int as marketplace_users,
    coalesce((select sum(reward_redemption) from gross_orders), 0)::float as rewards_redeemed,
    (select count(*) from adjustments where adjustment_amount > 0)::int as adjusted_orders,
    (select max(updated_at)::text from marketplace_order) as source_freshness
)
select metric, amount, orders, users, rewards_redeemed, adjusted_orders, source_freshness
from (
  select 1 as metric_order, 'GMV' as metric, gmv as amount, successful_orders as orders, marketplace_users as users, rewards_redeemed, adjusted_orders, source_freshness from summary
  union all
  select 2 as metric_order, 'Refunds/cancellations' as metric, adjustments as amount, successful_orders as orders, marketplace_users as users, rewards_redeemed, adjusted_orders, source_freshness from summary
  union all
  select 3 as metric_order, 'NMV' as metric, nmv as amount, successful_orders as orders, marketplace_users as users, rewards_redeemed, adjusted_orders, source_freshness from summary
) rows
order by metric_order
```

Pitfall: PostgreSQL does not allow an arbitrary expression in `ORDER BY` directly after a `UNION` unless the expression is part of the result. Wrap the union in a subquery with `metric_order`, then order outside.

## Slack answer shape

Lead with a compact code-block table:

```text
Metric                 Amount (₹)   Orders  Users
GMV                  ...            ...     ...
Refunds/cancell.     ...            ... adj. orders
NMV                  ...            ...     ...
```

Then include the direct dashboard link and metadata: metric contracts `gmv`, `net_gmv`; sources `marketplace_order`, `profiles`; window; timezone; freshness; assumptions; caveat about `updated_at` as refund/cancellation timing proxy when applicable.
