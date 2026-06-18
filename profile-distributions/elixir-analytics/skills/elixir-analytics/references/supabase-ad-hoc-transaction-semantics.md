# Supabase Ad Hoc Transaction Semantics

Use this when a Slack analytics question needs ad hoc Supabase SQL for card spend/GTV, especially merchant-specific trends or breakdowns.

## Durable pitfall

`classified_transactions` is an application-layer CTE produced by the transaction semantics helper; it is not a physical Supabase relation. Direct SQL like `from classified_transactions` will fail with `relation "classified_transactions" does not exist` unless the query itself defines that CTE.

## Recommended pattern

1. Always call `elixir_analytics_runner(mode='answer_question')` first for the exact Slack question.
2. If it returns `requires_model_request`, call `mode='plan'` and then build a Supabase ad hoc request.
3. For GTV/card-spend semantics in ad hoc SQL, either:
   - use an existing saved-topic/helper path that injects transaction semantics, or
   - inline a `classified_transactions as (...)` CTE based on `src/lib/analytics/transaction-semantics.ts`.
4. Merchant-specific card-spend queries should usually match both `merchant_name` and `description` with `ILIKE '%<merchant token>%'` and explicitly label this as a text-match assumption/caveat.
5. Keep response metadata complete: metric contract `gtv`; source tables `transactions`, `marketplace_order` when excluding reward reconciliation rows, and `profiles` when filtering deleted users; Asia/Kolkata window; freshness; dashboard URL.

## External/network GTV reconciliation pattern

Use this when a user compares Elixir GTV with an external card-network/acquirer number (e.g. Visa).

1. Run the saved GTV topic first when possible, but for a single day or discrepancy investigation build an ad hoc Supabase request using the same `classified_transactions` semantics.
2. Interpret bare calendar dates as `Asia/Kolkata` business days unless the user says the external report uses a different network/settlement timezone. State the window explicitly: `YYYY-MM-DD` inclusive to next day exclusive.
3. Compare Elixir GTV to the external number with both absolute and percent deltas:
   - `difference = elixir_gtv - external_gtv`
   - `% difference = difference / external_gtv * 100`
4. Include a compact diagnostic breakdown of excluded categories for the same day: wallet loads, refunds/reversals, failed/pending/cancelled rows, reward reconciliation, and any unknown debit/credit buckets.
5. Explicitly test likely date-boundary mismatch when the delta is non-zero: compare IST business day vs UTC calendar day using the same GTV filters. If UTC worsens the gap, say so; if it improves it, call out timezone as a plausible cause.
6. If the gap is small, look for individual successful card-spend rows near the delta amount as candidate explanations, but label this as heuristic unless the user provides network transaction IDs.
7. Keep caveats sober: Visa/network figures may be based on authorization date, clearing/settlement date, network timezone, or include/exclude reversals and failed authorizations differently. Do not claim the exact cause without transaction-level matching.
8. Always include a dashboard URL for the runnable diagnostic table when the runner provides one.

## Minimal CTE shape for merchant GTV

For merchant GTV trends, the ad hoc query can inline the subset of transaction semantics needed for card spend:

```sql
classified_transactions as (
  select
    t.*,
    ((t.transaction_timestamp at time zone 'UTC') at time zone 'Asia/Kolkata') as business_transaction_timestamp,
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
  -- add bounded Asia/Kolkata business-date predicates here
)
```

Then filter:

```sql
where is_card_spend = true
  and coalesce(is_reward_reconciliation, false) = false
  and (merchant_name ilike '%swiggy%' or description ilike '%swiggy%')
```

Add `left join profiles p on p.id = user_id` and `coalesce(p.is_deleted, false) = false` when user deletion filtering is needed.
