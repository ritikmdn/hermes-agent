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
