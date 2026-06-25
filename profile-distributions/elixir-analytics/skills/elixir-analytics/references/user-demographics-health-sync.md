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

- For plain wearable questions, report `wearable-identified` users separately
  from broader health-sync users. Identification of wearable is critical; do
  not collapse these into one number.
- Default wearable denominator is non-deleted card-issued users:
  `profiles.isCardIssued = true` OR at least one row in `cards`.
- `health_sync_users` comes from health data presence, especially
  `health_data_daily`, and includes phone-only Apple Health / Google Fit users.
- `wearable_identified_users` is all-time wearable ecosystem evidence and does
  not require recent sync by default.
- `wearable_sync_users` is wearable-identified users with recent
  `health_data_daily.date`; default recency is rolling last 30 Asia/Kolkata
  calendar days.
- Group wearable evidence as:
  - Apple Health evidence from
    `profiles.onboardstatus.data.metadata.healthDeviceManufacturer`.
    Apple Health wearable evidence does not require an Apple Health provider row
    or a `health_data_daily` row by default.
  - Dedicated wearable ecosystems from Terra provider labels:
    `FITBIT`, `GARMIN`, `OURA`, `WHOOP`, `COROS`, `ULTRAHUMAN`, plus accepted
    watch assumptions for `SAMSUNG` and `HUAWEI`.
  - Curated Terra `device_data.name` / `other_devices[*].name` source strings
    or explicit model evidence for wearable ecosystems such as Garmin
    Forerunner/fenix, COROS PACE, Samsung `SM-R...` Galaxy Watch models,
    Fossil/Q Explorist, NoiseFit, DaFit/CRREPA, Xiaomi/Mi Band,
    Fastrack/Titan, Fitbit, Amazfit/Huami, boAt/CoveIoT, FireBoltt, WHOOP,
    Samsung Health/Galaxy Watch, Huawei Health/Watch, Nothing Watch, Cultsport
    Watch, and ANT+.
  - `health-sync provider` for APPLE_HEALTH/iPhone Health only, generic GOOGLE,
    Google Fit, Health Connect, generic TERRA, and any other provider without
    wearable hardware/source evidence.
  - Use `upper(provider_metadata->>'provider')` for Terra labels when present;
    otherwise fall back to `health_data_providers.provider`.
- Use product-facing brand rollups by default; keep source labels for QA.
- Use `resultType: "breakdown"` and include brand, users, share, denominator,
  health-only buckets, and latest sync timestamp when relevant.

Caveats to state:

- `TOTAL_WEARABLE_IDENTIFIED_USERS` is the wearable number.
- `TOTAL_HEALTH_SYNC_USERS` is the broader health-data base.
- `TOTAL_APPLE_HEALTH_SYNC_USERS` and `TOTAL_TERRA_SYNC_USERS` are sync rail
  totals, not wearable totals.
- APPLE_HEALTH, generic GOOGLE, Google Fit, Health Connect, and generic TERRA
  are health-sync provider evidence, not wearable proof by themselves; however,
  generic Terra rows can still become wearable evidence when the synced
  `device_data.name` / `other_devices[*].name` contains explicit wearable
  source or model evidence.
- Samsung Health and Huawei Health are accepted watch assumptions for wearable
  attribution; do not count unrelated phone-model strings as watch evidence.
- Apple Health users may only have phone data synced. Apple Health device or
  wearable source attribution comes from
  `profiles.onboardstatus.data.metadata.healthDeviceManufacturer`.
- Terra provider labels come from
  `health_data_providers.provider_metadata->>'provider'`; Terra wearable
  attribution can also come from curated `device_data.name` source strings or
  explicit device model evidence.
- Report `IPHONE_HEALTH_ONLY`, `GOOGLE_FIT_ANDROID_HEALTH_ONLY`, and
  `HEALTH_DATA_EXISTS_SOURCE_UNKNOWN` separately from wearable users.
- Source rows may multi-count users with more than one wearable evidence label;
  distinct totals must dedupe by user. `multi_wearable_users` means two or more
  wearable brands/ecosystems, not two source paths for the same brand.

## Slack answer shape

Lead with the total, then a compact code-block table. Include dashboard URL, sources, date window, timezone, freshness, assumptions, and caveats.
