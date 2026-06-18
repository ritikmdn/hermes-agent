# Transacting User Age Groups

Use this reference when Slack asks to analyse age groups / age bands of transacting users.

## Default interpretation

- Interpret unqualified `transacting users` as **card transacting users / active spenders** unless the question clearly says marketplace buyers or app-active users.
- Use rolling Asia/Kolkata business time for `last N days` unless the user asks for completed days or a calendar period.
- Derive age from `profiles.dob` at query time. `dob` is present in the generated schema catalog even though it may be absent from the shorter `SCHEMA.md` profile summary.
- Always include missing/invalid DOB handling if present; do not silently drop those users.

## Source tables

- `transactions` — realized card spend windowed by `transaction_timestamp`.
- `profiles` — `id`, `dob`, and `is_deleted` filter.
- `marketplace_order` — exclude marketplace reward reconciliation rows when inlining transaction semantics.

## SQL shape

Inline `classified_transactions`; it is not a physical table.

```sql
with bounds as (
  select
    (current_timestamp at time zone 'Asia/Kolkata') as as_of_ist,
    ((current_timestamp at time zone 'Asia/Kolkata') - interval '30 days') as start_ist
),
classified_transactions as (
  select
    t.*,
    ((t.transaction_timestamp at time zone 'UTC') at time zone 'Asia/Kolkata') as business_transaction_timestamp,
    mo_recon.id as marketplace_recon_order_id,
    (
      coalesce(t.debit_amount, 0) > 0
      and coalesce(t.transaction_type, '') != 'B2C'
      and t.status = 'PAYMENT_SUCCESS'
      and mo_recon.id is null
    ) as is_card_spend
  from transactions t
  left join marketplace_order mo_recon
    on t.txn_id like '%_RECON_%'
   and mo_recon.id::text = split_part(t.txn_id, '_RECON_', 1)
  cross join bounds b
  where ((t.transaction_timestamp at time zone 'UTC') at time zone 'Asia/Kolkata') >= b.start_ist
    and ((t.transaction_timestamp at time zone 'UTC') at time zone 'Asia/Kolkata') < b.as_of_ist
),
user_rollup as (
  select
    p.id as user_id,
    p.dob,
    case
      when p.dob is null then 'Unknown DOB'
      when p.dob > (select as_of_ist::date from bounds) then 'Invalid DOB'
      when date_part('year', age((select as_of_ist::date from bounds), p.dob)) < 18 then '<18'
      when date_part('year', age((select as_of_ist::date from bounds), p.dob)) between 18 and 24 then '18-24'
      when date_part('year', age((select as_of_ist::date from bounds), p.dob)) between 25 and 34 then '25-34'
      when date_part('year', age((select as_of_ist::date from bounds), p.dob)) between 35 and 44 then '35-44'
      when date_part('year', age((select as_of_ist::date from bounds), p.dob)) between 45 and 54 then '45-54'
      else '55+'
    end as age_group,
    count(*) as txn_count,
    sum(coalesce(ct.debit_amount, 0)) as gtv_rupees,
    max(ct.transaction_timestamp) as latest_transaction_utc
  from classified_transactions ct
  join profiles p on p.id = ct.user_id
  where ct.is_card_spend = true
    and coalesce(p.is_deleted, false) = false
  group by p.id, p.dob
)
select
  age_group,
  count(*)::int as users,
  sum(txn_count)::int as txns,
  round(sum(gtv_rupees))::int as gtv_inr,
  round(sum(gtv_rupees) / nullif(count(*), 0))::int as avg_gtv_per_user_inr,
  max(latest_transaction_utc) as latest_transaction_utc
from user_rollup
group by age_group;
```

## Answer shape

Use a compact Slack code-block table with:

- age group
- users
- user share %
- transactions
- GTV INR
- average GTV/user INR

Then add a short summary highlighting concentration, e.g. whether 18–34 dominates users or whether 25–34 over-indexes on GTV.

## Metadata and caveats

- Metric contract: `gtv` for spend/value columns. There may not be a dedicated rolling transacting-user contract; say this is an ad hoc card-transacting-user breakdown.
- Sources: `transactions`, `profiles`, `marketplace_order`.
- Caveat DOB completeness/accuracy and scope: marketplace-only buyers and app-active users are excluded unless explicitly requested.
- Include the deterministic runner dashboard link; use `resultType: "breakdown"` so a visualization link is emitted even for a small table.
