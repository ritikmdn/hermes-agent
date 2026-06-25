# Operational Health Diagnostics

Use this reference when a Slack user asks whether a business/system flow is
"broken" or asks "what's going on with <flow>?" and the answer can be assessed
from analytics data.

## General pattern

1. Run `elixir_analytics_runner` with `mode: "answer_question"` and the exact
   raw Slack question first.
2. If no deterministic shortcut matches, run `mode: "plan"` and follow the
   recommended route.
3. For Supabase-backed flows, build a bounded `supabase_ad_hoc` request that
   compares:
   - recent production volume against nearby baselines, for example last 24h vs
     previous 24h or today vs prior 7 full-day average,
   - hourly/daily counts when a recent outage is suspected,
   - funnel inputs and backlog symptoms that would accumulate if the flow were
     stuck.
4. Reply with a clear health read: "does not look broken", "looks degraded", or
   "needs provider/log investigation". Include the dashboard link and metadata.
5. Caveat that database-side diagnostics show records and funnel symptoms, not
   upstream provider API logs, backend job logs, or app error traces unless those
   sources were actually queried.

## Recent transaction failure pattern diagnostic

Use this when the user asks for the "last N transaction failures", recent
failures, or "what's the pattern?" This is a pattern read, not a failure-rate
metric.

Recommended default:

- Start with `answer_question`; if it returns `requires_model_request`, run
  planner, then `supabase_ad_hoc`.
- Interpret transaction failure as:

  ```sql
  transactions.status IN ('PAYMENT_FAILURE', 'REFUND_FAILED')
  ```

  unless the user gives a narrower definition.
- Use `transactions.transaction_timestamp` for recency and order descending.
- Keep the row sample bounded to the user's requested N; if omitted, use 10-25.
- Join `profiles` only for compact display names/segments. Do not expose phone,
  email, or raw sensitive contact fields unless explicitly requested.
- Return repetition counts alongside rows, for example counts over the bounded
  sample by `status`, `transaction_type`, `user_id`, `merchant_name` or
  `description`, `merchant_category_code`, and parsed failure reason from
  `description`.
- Summarize the pattern: dominant failure reason, whether it is all ECOM or
  concentrated in one type, whether failures repeat for the same user/merchant,
  and whether any amount outliers exist.

Useful SQL skeleton:

```sql
WITH last_failures AS (
  SELECT
    t.id,
    t.user_id,
    trim(coalesce(p.first_name, '') || ' ' || coalesce(p.last_name, '')) AS user_name,
    t.transaction_timestamp,
    t.transaction_type,
    t.status,
    t.debit_amount,
    t.credit_amount,
    t.merchant_name,
    t.description,
    t.merchant_category_code
  FROM transactions t
  LEFT JOIN profiles p ON p.id = t.user_id
  WHERE t.status IN ('PAYMENT_FAILURE', 'REFUND_FAILED')
  ORDER BY t.transaction_timestamp DESC
  LIMIT 10
)
SELECT
  row_number() OVER (ORDER BY transaction_timestamp DESC) AS failure_rank,
  transaction_timestamp AT TIME ZONE 'Asia/Kolkata' AS failure_time_ist,
  nullif(user_name, '') AS user_name,
  transaction_type,
  status,
  debit_amount,
  credit_amount,
  merchant_name,
  description,
  merchant_category_code,
  count(*) OVER (PARTITION BY coalesce(merchant_name, description)) AS same_merchant_or_desc_in_sample,
  count(*) OVER (PARTITION BY user_id) AS same_user_in_sample,
  count(*) OVER (PARTITION BY transaction_type) AS same_type_in_sample,
  count(*) OVER (PARTITION BY status) AS same_status_in_sample,
  count(*) OVER (PARTITION BY merchant_category_code) AS same_mcc_in_sample
FROM last_failures
ORDER BY failure_rank;
```

Answer guidance:

- Lead with the diagnostic read, e.g. "does not look like a merchant-wide
  outage" or "looks concentrated in one provider/merchant/user cohort".
- Include any runner-provided dashboard link.
- Caveat small samples clearly: latest N rows identify pattern symptoms, not a
  rate or statistical trend.
- If failure reasons are free-text in `description`, say that provider/log-level
  reason codes were not queried.

## Card creation/onboarding diagnostic

Definition: card creation/onboarding is a row in `cards` with `cards.issued_at`.
Do not use `profiles.created_at` as the onboarding date.

Recommended checks:

- `cards.issued_at` last 24h vs previous 24h.
- Today IST card rows/users vs prior 7 full-day average.
- Last 15 IST days daily card rows/users.
- Last 48h hourly card rows/users if the question implies an acute outage.
- Current KYC-approved-without-card backlog.

KYC-approved logic for backlog:

```sql
profiles.kyc_status = 'FULL_KYC'
OR latest_vkyc.kycstatus = '1'
```

Always exclude deleted profiles for profile/backlog counts:

```sql
coalesce(profiles.is_deleted, false) = false
```

Card issued flag/backlog should account for either source:

```sql
coalesce(profiles."isCardIssued", false)
OR EXISTS (SELECT 1 FROM cards c WHERE c.user_id = profiles.id)
```

Interpretation guidance:

- If recent card rows are in line with or above baseline and
  KYC-approved-without-card backlog is near zero, say DB-side card creation does
  not look broken.
- If card rows drop to zero while registrations/KYC approvals continue and
  backlog grows, flag likely breakage and recommend/provider-log investigation.
- Multiple cards per user can exist for replacements, so show both card rows and
  distinct users where possible.
