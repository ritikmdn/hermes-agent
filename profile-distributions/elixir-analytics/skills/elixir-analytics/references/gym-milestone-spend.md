# Gym milestone spend ad hoc pattern

Use this when Slack asks for spend, GTV, or average monthly spend for gym milestone users.

## Default interpretation

- `gym milestone users` means users with active gym milestone membership/voucher instances:
  - `milestone_program_instances.status = 'active'`
  - `milestone_program_instances.program_id = customer_vouchers.id`
  - join `profiles` and filter `coalesce(profiles.is_deleted, false) = false`
- `spend` defaults to GTV: gross successful card spend, not marketplace gym purchase amount.
- For "last 3 months", prefer the last 3 completed calendar months before the current partial month unless the user explicitly says rolling 90 days.
- Average monthly spend should usually include two measures:
  - average per gym milestone user, including zero-spend users
  - average per spending gym user, excluding zero-spend users
- Make the cohort timing explicit. Default to current active gym users unless the user asks for historical/as-of membership cohorts.

## Sources and metric contracts

Metric contracts: `gym_milestone_users`, `gtv`.

Canonical sources:

- `milestone_program_instances` — active gym milestone enrollment/purchase instances.
- `customer_vouchers` — required grounding join for program/voucher linkage.
- `profiles` — non-deleted user filter.
- `transactions` — GTV/card spend source.
- `marketplace_order` — join only to exclude reward reconciliation rows using the standard GTV semantics.

## SQL shape

```sql
with gym_users as (
  select distinct mpi.user_id
  from milestone_program_instances mpi
  join customer_vouchers cv on cv.id = mpi.program_id
  join profiles p on p.id = mpi.user_id
  where mpi.status = 'active'
    and coalesce(p.is_deleted, false) = false
    and mpi.user_id is not null
),
months as (
  select generate_series(:start_month::date, :end_month::date, interval '1 month')::date as month_start
),
classified_transactions as (
  select
    t.*,
    ((t.transaction_timestamp AT TIME ZONE 'UTC') AT TIME ZONE 'Asia/Kolkata') as business_transaction_timestamp,
    mo_recon.id as marketplace_recon_order_id,
    (
      coalesce(t.debit_amount, 0) > 0
      and t.transaction_type != 'B2C'
      and t.status = 'PAYMENT_SUCCESS'
      and mo_recon.id is null
    ) as is_card_spend,
    (
      coalesce(t.debit_amount, 0) > 0
      and t.transaction_type != 'B2C'
      and t.status = 'PAYMENT_SUCCESS'
      and mo_recon.id is not null
    ) as is_reward_reconciliation
  from transactions t
  left join marketplace_order mo_recon
    on t.txn_id like '%_RECON_%'
   and mo_recon.id = split_part(t.txn_id, '_RECON_', 1)
  where ((t.transaction_timestamp AT TIME ZONE 'UTC') AT TIME ZONE 'Asia/Kolkata') >= :start_month::date
    and ((t.transaction_timestamp AT TIME ZONE 'UTC') AT TIME ZONE 'Asia/Kolkata') < (:end_month::date + interval '1 month')
),
monthly as (
  select
    date_trunc('month', ct.business_transaction_timestamp)::date as month_start,
    coalesce(sum(ct.debit_amount), 0)::float as gtv,
    count(*)::int as transactions,
    count(distinct ct.user_id)::int as spending_users,
    max(coalesce(ct.updated_at, ct.created_at at time zone 'UTC', ct.transaction_timestamp at time zone 'UTC'))::text as source_freshness
  from classified_transactions ct
  join gym_users gu on gu.user_id = ct.user_id
  where ct.is_card_spend = true
    and ct.is_reward_reconciliation = false
  group by 1
),
cohort_size as (
  select count(*)::int as gym_users from gym_users
),
monthly_with_zeroes as (
  select
    m.month_start,
    cs.gym_users,
    coalesce(mon.gtv, 0)::float as gtv,
    coalesce(mon.transactions, 0)::int as transactions,
    coalesce(mon.spending_users, 0)::int as spending_users,
    case when cs.gym_users > 0 then (coalesce(mon.gtv, 0) / cs.gym_users)::float end as avg_spend_per_gym_user,
    case when coalesce(mon.spending_users, 0) > 0 then (coalesce(mon.gtv, 0) / mon.spending_users)::float end as avg_spend_per_spending_user,
    mon.source_freshness
  from months m
  cross join cohort_size cs
  left join monthly mon on mon.month_start = m.month_start
)
select
  'overall_avg'::text as period,
  null::date as month_start,
  max(gym_users)::int as gym_users,
  sum(gtv)::float as total_gtv,
  sum(transactions)::int as transactions,
  null::int as spending_users,
  avg(avg_spend_per_gym_user)::float as avg_monthly_spend_per_gym_user,
  avg(avg_spend_per_spending_user)::float as avg_monthly_spend_per_spending_user,
  max(source_freshness)::text as source_freshness
from monthly_with_zeroes
union all
select
  to_char(month_start, 'YYYY-MM')::text as period,
  month_start,
  gym_users,
  gtv,
  transactions,
  spending_users,
  avg_spend_per_gym_user,
  avg_spend_per_spending_user,
  source_freshness
from monthly_with_zeroes
order by month_start nulls first;
```

## Answer notes

Include:

- overall average monthly spend per gym milestone user
- average monthly spend per spending gym user, if helpful
- monthly rows for the completed months
- cohort size, spending users, GTV, transactions
- metric contracts, sources, date window, timezone, freshness, assumptions, caveats

Caveat clearly: current active cohort, not historical as-of cohort. If the user asks for historical cohorts, cohort membership must be evaluated as-of each month instead of using current active users.
