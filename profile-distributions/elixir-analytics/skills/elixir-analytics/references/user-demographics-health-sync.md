# User Demographics and Health Sync Drilldowns

Use this reference for ad hoc Supabase questions about user age groups, profile demographics, and health/wearable sync counts.

## Age groups for transacting users

Default interpretation for plain Slack questions like "analyse age groups of all transacting users over last 30 days":

- Interpret `transacting users` as card transacting users / active spenders unless the user explicitly says marketplace buyers, app active, or combined activity.
- Use the transaction semantics CTE for realized card spend:
  - `debit_amount > 0`
  - `transaction_type != 'B2C'`
  - `status = 'PAYMENT_SUCCESS'`
  - exclude marketplace reward reconciliation rows joined via `txn_id like '%_RECON_%'` to `marketplace_order`.
- Join `profiles` and filter `coalesce(profiles.is_deleted, false) = false`.
- Derive age from `profiles.dob` at query time and include separate `Unknown DOB` / `Invalid DOB` buckets when present.
- Recommended buckets: `<18`, `18-24`, `25-34`, `35-44`, `45-54`, `55+`, `Unknown DOB`, `Invalid DOB`.
- Return distinct users, user share, txn count, GTV, avg GTV/user, and max included transaction timestamp.
- Use `resultType: "breakdown"` so Slack gets a dashboard link.

Caveats to state:

- Age depends on profile DOB completeness and accuracy.
- Marketplace-only buyers and app-only active users are excluded unless explicitly requested.

## Health data synced using a wearable/provider

Default interpretation for plain Slack questions like "how many users have health data synced using some wearable?":

- Count distinct non-deleted users with:
  1. an active `health_data_providers` row (`active = true`, `disconnected_at is null`), and
  2. at least one provider-backed synced health row in one of:
     - `health_data_activity`
     - `health_data_body`
     - `health_data_daily_summary`
     - `health_data_sleep`
- Join provider-backed tables by `(provider, provider_user_id)` to `health_data_providers`.
- Do not use `health_data_daily` for provider attribution because it is user-level and lacks `provider_user_id`.
- For each user, pick the latest synced provider if multiple active providers exist, so each user is counted once.
- Group source as:
  - `APPLE_HEALTH` when `health_data_providers.provider = 'APPLE_HEALTH'`
  - `upper(provider_metadata->>'provider')` for Terra/Android rows when present (e.g. `GOOGLE`, `FITBIT`, `SAMSUNG`, `GARMIN`, `COROS`, `OURA`, `ULTRAHUMAN`)
  - otherwise fall back to `health_data_providers.provider`.
- Use `resultType: "breakdown"` and include source/brand, users, share, earliest connection, and latest synced timestamp.

Caveats to state:

- This counts provider-backed health sync, not guaranteed physical wearable hardware.
- Apple Health rows cannot be split into Apple Watch vs iPhone-only from stored data; label `APPLE_HEALTH` accordingly.
- Android/Terra wearable brand comes from `health_data_providers.provider_metadata->>'provider'`.

## Slack answer shape

Lead with the total, then a compact code-block table. Include dashboard URL, sources, date window, timezone, freshness, assumptions, and caveats.
