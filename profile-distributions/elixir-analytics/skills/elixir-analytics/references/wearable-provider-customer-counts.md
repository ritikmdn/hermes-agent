# Wearable-identified user counts

Use this when Slack asks how many Elixir customers/users/cardholders have
wearables, smartwatches, rings, bands, or brands such as Apple Watch, Whoop,
Samsung Watch, Oura, Garmin, Fitbit, Coros, or Ultrahuman.

## Metric contract

`wearable_identified_users` is the canonical wearable audience metric.

Formal local contract:
`profile-distributions/elixir-analytics/metric-contracts/wearable_identified_users.yaml`.

Definition: distinct non-deleted card-issued users for whom we have credible
wearable evidence from Apple Health manufacturer/source metadata or Terra
provider/device-source evidence.

Do not roll generic aggregators into wearable totals. APPLE_HEALTH, generic
GOOGLE, Google Fit, Health Connect, and generic TERRA are health-sync evidence,
not wearable evidence by themselves.

Default population:

- `card-issued` user = `profiles.isCardIssued = true` OR the user has at least
  one row in `cards`.
- Always exclude `coalesce(profiles.is_deleted, false) = false`.
- If the user explicitly asks for registered users, app users, health-sync
  users, or another denominator, use the requested denominator and state it.

Keep these concepts separate:

- `health_sync_users`: users with health data, including phone-only Apple Health
  and Google Fit / Android health.
- `wearable_identified_users`: all-time wearable ecosystem/device evidence.
- `wearable_sync_users`: wearable-identified users with recent health data in
  `health_data_daily.date`; default recency is rolling last 30 Asia/Kolkata
  calendar days.
- `multi_wearable_users`: users with 2+ distinct wearable brands/ecosystems, not
  2+ source paths for the same brand.

## Product labels

Business answers default to brand rollup labels, not source labels:

- Apple Watch users
- Whoop users
- Oura users
- Samsung Watch users
- Ultrahuman users
- Garmin users
- Fitbit users
- Huawei Watch users
- Nothing Watch users
- Cultsport Watch users
- Other wearables
- ANT+ sensor

Technical source labels are retained for QA/debugging, for example
`SAMSUNG_GALAXY_WATCH_VIA_GOOGLE_TERRA`, but do not use them as the primary
business-facing labels unless the user asks for source-path split.

## Apple Health evidence

Apple Health wearable evidence comes from:

`profiles.onboardstatus.data.metadata.healthDeviceManufacturer`

Do not require an active `health_data_providers.provider = 'APPLE_HEALTH'` row
for Apple Health wearable attribution. Do not require `health_data_daily` for
default ownership/ecosystem questions like "has Apple Watch" or "cardholders
with Oura". Require `health_data_daily` only for questions about sync, recent
health data, health score, or reward eligibility.

Flatten the manufacturer array with `jsonb_array_elements_text`.

Count source evidence as:

- `APPLE_WATCH_VIA_APPLE_HEALTH`: exact Apple Watch hardware token only,
  currently `WatchN,M` patterns such as `|Watch6,16|`. `apple watch` / `watchos` are QA-only fallback tokens for now and must not count until we intentionally
  promote them.
- `WHOOP_VIA_APPLE_HEALTH`: token contains `whoop`.
- `GARMIN_VIA_APPLE_HEALTH`: token contains `garmin`.
- `FITBIT_VIA_APPLE_HEALTH`: token contains `fitbit`.
- `OURA_VIA_APPLE_HEALTH`: token contains `oura`.
- `COROS_VIA_APPLE_HEALTH`: token contains `coros`.
- `ULTRAHUMAN_VIA_APPLE_HEALTH`: token contains `ultrahuman`.

Apple Health phone-only evidence is not wearable evidence. Report users whose
manufacturer metadata has iPhone/iOS-native evidence and no wearable evidence as
`IPHONE_HEALTH_ONLY`.

## Terra evidence

Terra membership still comes from active synced Terra provider rows:

- `health_data_providers.provider = 'TERRA'`
- `coalesce(health_data_providers.active, false) = true`
- `health_data_providers.disconnected_at is null`
- at least one provider-backed synced row joined by `(provider, provider_user_id)`
  from `health_data_activity`, `health_data_body`, `health_data_daily_summary`,
  or `health_data_sleep`.

Terra provider label:

```sql
upper(coalesce(nullif(hdp.provider_metadata->>'provider', ''), hdp.provider))
```

Dedicated wearable ecosystems:

- `COROS` -> `COROS_VIA_TERRA`
- `FITBIT` -> `FITBIT_VIA_TERRA`
- `GARMIN` -> `GARMIN_VIA_TERRA`
- `HUAWEI` -> `HUAWEI_WATCH_VIA_TERRA`
- `OURA` -> `OURA_VIA_TERRA`
- `SAMSUNG` -> `SAMSUNG_GALAXY_WATCH_VIA_TERRA`
- `ULTRAHUMAN` -> `ULTRAHUMAN_VIA_TERRA`
- `WHOOP` -> `WHOOP_VIA_TERRA`

Terra `GOOGLE` is Google Fit / Android health rail, not wearable proof by
itself. If no wearable source exists inside device/source names, report it as
`GOOGLE_FIT_ANDROID_HEALTH_ONLY`, not unattributed.

## Terra device-source attribution

For active synced Terra rows, inspect only:

- `health_data_activity.data->'device_data'->>'name'`
- `health_data_body.data->'device_data'->>'name'`
- `health_data_daily_summary.data->'device_data'->>'name'`
- `health_data_sleep.data->'device_data'->>'name'`
- nested `health_data_* .data.device_data.name` / `other_devices[*].name`

Count a Terra device source only when the source name matches a curated wearable
ecosystem/source pattern or explicit wearable model evidence:

- `GARMIN_VIA_TERRA`: `garmin`, `forerunner`, `fenix`, `venu`, `instinct`,
  `epix`.
- `COROS_VIA_TERRA`: `coros`, `pace 2`, `pace 3`, `apex`, `vertix`.
- `SAMSUNG_GALAXY_WATCH_VIA_TERRA`: Samsung provider, Samsung Health source,
  Samsung watch model prefix `SM-R...`, `shealth`, `sec.android.app.shealth`,
  or `galaxy watch`.
- `HUAWEI_WATCH_VIA_TERRA`: Huawei provider or `huawei.health`.
- `FOSSIL_VIA_TERRA`: `fossil` or `q explorist`.
- `NOTHING_WATCH_VIA_TERRA`: `nothing.smartcenter`.
- `CULTSPORT_WATCH_VIA_TERRA`: `fit.cure.android.cswatch`.
- `ANTPLUS_SENSOR_VIA_TERRA`: `dsi.ant.plugins.antplus`, `antplus`,
  `ant.plugins`.
- Other curated source labels such as `NOISEFIT_VIA_TERRA`,
  `DAFIT_CRREPA_VIA_TERRA`, `AMAZFIT_HUAMI_VIA_TERRA`,
  `XIAOMI_MI_FIT_OR_MI_BAND_VIA_TERRA`, `FASTRACK_TITAN_VIA_TERRA`,
  `BOAT_COVEIOT_VIA_TERRA`, `FIREBOLTT_VIA_TERRA`, and `FOSSIL_VIA_TERRA` are
  preserved in source evidence and roll up to Other wearables for business
  reporting.

When the Terra provider label is `GOOGLE`, use Google-specific source labels
for source transparency:

- `FITBIT_VIA_GOOGLE_TERRA`
- `WHOOP_VIA_GOOGLE_TERRA`
- `NOISEFIT_VIA_GOOGLE_TERRA`
- `AMAZFIT_HUAMI_VIA_GOOGLE_TERRA`
- `XIAOMI_MI_FIT_OR_MI_BAND_VIA_GOOGLE_TERRA`
- `FASTRACK_TITAN_VIA_GOOGLE_TERRA`
- `BOAT_COVEIOT_VIA_GOOGLE_TERRA`
- `FIREBOLTT_VIA_GOOGLE_TERRA`
- `FOSSIL_VIA_GOOGLE_TERRA`
- `HUAWEI_WATCH_VIA_GOOGLE_TERRA`
- `DAFIT_CRREPA_VIA_GOOGLE_TERRA`
- `NOTHING_WATCH_VIA_GOOGLE_TERRA`
- `CULTSPORT_WATCH_VIA_GOOGLE_TERRA`
- `ANTPLUS_SENSOR_VIA_GOOGLE_TERRA`
- `SAMSUNG_GALAXY_WATCH_VIA_GOOGLE_TERRA`

Do not count plain `com.google.android.gms`, `com.google.android.apps.fitness`,
or `com.google.android.fit <phone model> ... top_level` strings as wearable
evidence without a curated wearable source string or explicit wearable model.

## Health-only and gap buckets

These are health-sync coverage buckets, not wearable users:

- `IPHONE_HEALTH_ONLY`: Apple Health/iPhone source evidence and no wearable
  evidence.
- `GOOGLE_FIT_ANDROID_HEALTH_ONLY`: Terra Google Fit / Android health evidence
  and no wearable evidence.
- `HEALTH_DATA_EXISTS_SOURCE_UNKNOWN`: health data exists, usually in
  `health_data_daily`, but no usable source evidence is available.

## Counting rules

- `TOTAL_WEARABLE_IDENTIFIED_USERS`: distinct users with one or more
  wearable evidence rows where the evidence counts as a consumer wearable.
- `TOTAL_HEALTH_SYNC_USERS`: distinct users with health data.
- `TOTAL_APPLE_HEALTH_SYNC_USERS`: distinct users with health data plus Apple
  Health/iOS source metadata.
- `TOTAL_TERRA_SYNC_USERS`: distinct users with active synced Terra rows.
- `TOTAL_WEARABLE_SYNC_USERS_30D`: wearable-identified users with
  `health_data_daily.date` in the rolling last 30 Asia/Kolkata calendar days.
- `MULTI_WEARABLE_USERS`: distinct users with at least two wearable brands.
- Source evidence rows may multi-count users; total users must dedupe by
  `user_id`.
- "How many Whoop users?" counts all Whoop paths: Apple Health, Terra, and
  Google Terra.
- "How many Samsung Watch users?" combines direct Samsung Terra, Samsung via
  Google Terra, Samsung Health assumptions, and explicit Galaxy Watch model
  evidence.

## Active-user percentages

When the question asks for wearable share among users active in the last 30
days, do not use the plain wearable shortcut denominator. Default active users
to combined active users over the rolling last 30 Asia/Kolkata calendar days:
card activity OR app activity.

This is a combined-source question:

- Supabase: card-issued population, wearable evidence, and card activity.
- PostHog: authenticated app activity from `events` using `properties.userId`.

Answer with the combined-active denominator, distinct users per requested brand
rollup, and percentage of that denominator. State that the denominator is
combined card-or-app active users.

## Reward-rate segmentation

For current reward-rate questions, use `profiles.reward_rate`.

- "reward rate 4% and above" = `profiles.reward_rate >= 4`.
- Do not use the historical `reward_rate` table unless the user asks for a
  historical week/trend.
- For current segmentation, join current/all-time wearable evidence to current
  `profiles.reward_rate`.

## Slack answer shape

- Default to product-facing brand rollups.
- State the denominator: default is non-deleted card-issued Elixir users.
- Mention source-path split only when asked or when doing QA.
- Include concise fine print for inferred categories such as Samsung Health,
  Huawei Health, and Other wearables.
