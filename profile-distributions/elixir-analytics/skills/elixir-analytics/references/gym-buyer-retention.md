# Gym buyer onboarding + retention drilldown

Use this for Slack follow-ups after gym-buyer regularity questions, especially: “when did they onboard?”, “are users just downloading for gym?”, or “did they leave after buying gym?”.

## Default interpretation

- Preserve the parent gym-buyer cohort/window unless the user changes it.
- `gym buyers` = distinct non-deleted users with successful placed `ELIXIR-GYM` marketplace orders:
  - `marketplace_order.partner_code = 'ELIXIR-GYM'`
  - `payment_status in ('SUCCESS', 'CONFIRMED')`
  - `coalesce(order_status::text, 'CONFIRMED') not in ('CANCELLED','REFUND','REFUNDED','FAILED','PENDING')`
  - join `profiles` and require `coalesce(profiles.is_deleted, false) = false`
- `onboarded onto Elixir` = latest `cards.issued_at` for the user. Show `profiles.created_at` only as app registration if useful.
- `post-gym retained business activity` = successful card spend or successful non-gym marketplace order on an IST date after the user’s first gym purchase date in the cohort.
  - Use next-IST-date+ activity, not same timestamp/day, so the gym purchase payment itself does not contaminate retention.
  - This is a business-activity proxy; it does not prove app retention, uninstall, or session activity. Offer PostHog app-session follow-up when the user asks whether they “left the app” literally.

## Recommended answer shape

Lead with a compact executive read, then tables:

1. `N of M bought gym within 30 days of card onboarding` (or the appropriate window/bucket summary).
2. `X of M showed later business activity after the gym purchase date`.
3. A compact code-block summary by onboarding recency and prior status.
4. A compact code-block user table with columns that fit Slack:
   - `#`, `Customer`, `Prior`, `Onboarded`, `Gym date`, `Days`, `Later activity`

Example summary table:

```text
Segment                         Buyers  Later active  Later %
overall                             22             9    40.9%

onboard recency: 0-7 days            6             1    16.7%
onboard recency: 8-30 days           2             0     0.0%
onboard recency: 31-90 days          6             3    50.0%
onboard recency: 90+ days            8             5    62.5%

prior status: new/no prior          12             2    16.7%
prior status: some prior             3             1    33.3%
prior status: regular                7             6    85.7%
```

## SQL shape

Use `scripts/run-ad-hoc-query.ts` with `resultType: "users"` for the user-level table and, if needed, a companion `resultType: "breakdown"` aggregate to verify rollups.

Core CTEs:

```sql
WITH bounds AS (...),
latest_card AS (
  SELECT DISTINCT ON (user_id) user_id, issued_at
  FROM cards
  ORDER BY user_id, issued_at DESC NULLS LAST
),
gym_orders AS (... first successful ELIXIR-GYM order per user ...),
prior_card AS (... successful card_spend before first_gym_order_at ...),
prior_marketplace AS (... successful marketplace orders before first_gym_order_at ...),
post_card AS (
  SELECT go.user_id,
         count(*) AS post_gym_card_txns,
         sum(coalesce(t.debit_amount,0)) AS post_gym_card_spend,
         max(t.transaction_timestamp) AS latest_post_gym_card_at
  FROM gym_orders go
  JOIN transactions t ON t.user_id = go.user_id
  LEFT JOIN marketplace_order mo_recon
    ON t.txn_id LIKE '%_RECON_%'
   AND mo_recon.id::text = split_part(t.txn_id, '_RECON_', 1)
  WHERE (t.transaction_timestamp AT TIME ZONE 'Asia/Kolkata')::date
          > (go.first_gym_order_at AT TIME ZONE 'Asia/Kolkata')::date
    AND coalesce(t.debit_amount,0) > 0
    AND t.transaction_type != 'B2C'
    AND t.status = 'PAYMENT_SUCCESS'
    AND mo_recon.id IS NULL
  GROUP BY go.user_id
),
post_marketplace AS (
  SELECT go.user_id,
         count(*) AS post_gym_non_gym_orders,
         max(mo.created_at) AS latest_post_gym_marketplace_at
  FROM gym_orders go
  JOIN marketplace_order mo ON mo.user_id = go.user_id
  WHERE (mo.created_at AT TIME ZONE 'Asia/Kolkata')::date
          > (go.first_gym_order_at AT TIME ZONE 'Asia/Kolkata')::date
    AND mo.partner_code <> 'ELIXIR-GYM'
    AND mo.payment_status IN ('SUCCESS','CONFIRMED')
    AND coalesce(mo.order_status::text,'CONFIRMED') NOT IN
      ('CANCELLED','REFUND','REFUNDED','FAILED','PENDING')
  GROUP BY go.user_id
)
SELECT ...
```

## Pitfalls

- Do not count same-day card spend as retention: for gym orders paid with card, the payment may appear as a card transaction and will make every buyer look retained.
- Do not claim churn/uninstall from missing business activity; call it “no later business activity yet”.
- Recent buyers have less elapsed time to show later activity. Say this explicitly, especially for purchases in the last few days.
- The planner may route “left the app” wording to PostHog. If the immediate follow-up is about onboarding dates and gym buyers, answer the Supabase business-retention view first, then offer a separate PostHog app-session retention check.
- Always use the deterministic runner and include dashboard links for both user-level and aggregate outputs when available.
