# Apple Health wearable/source metadata checks

Use this when a Slack question asks whether Apple Health users can be split into
Apple Watch, iPhone Health only, or third-party wearable ecosystems such as
WHOOP, Garmin, Fitbit, Oura, Coros, or Ultrahuman.

## Current source-of-truth

Apple Health wearable attribution comes from:

`profiles.onboardstatus.data.metadata.healthDeviceManufacturer`

For wearable ownership/ecosystem questions, do not require an active
`health_data_providers.provider = 'APPLE_HEALTH'` row and do not require a
`health_data_daily` row. `profiles.onboardstatus` is the evidence source for
Apple Health wearable attribution.

Require `health_data_daily` only when the question is about:

- users with health data
- current/recent sync
- synced in last N days
- reward eligibility
- health score users

## Workflow

1. Start from non-deleted card-issued users unless the user states a different
   denominator.
2. Inspect `profiles.onboardstatus.data.metadata.healthDeviceManufacturer`.
3. Flatten the manufacturer array with `jsonb_array_elements_text`.
4. Classify source evidence:
   - exact `WatchN,M` hardware token -> `APPLE_WATCH_VIA_APPLE_HEALTH`
   - `whoop` -> `WHOOP_VIA_APPLE_HEALTH`
   - `garmin` -> `GARMIN_VIA_APPLE_HEALTH`
   - `fitbit` -> `FITBIT_VIA_APPLE_HEALTH`
   - `oura` -> `OURA_VIA_APPLE_HEALTH`
   - `coros` -> `COROS_VIA_APPLE_HEALTH`
   - `ultrahuman` -> `ULTRAHUMAN_VIA_APPLE_HEALTH`
5. Treat iPhone/iOS-native tokens with no wearable evidence as
   `IPHONE_HEALTH_ONLY`, not wearable.
6. Treat `apple watch` / `watchos` as QA-only fallback strings for now. Do not
   count them until they are intentionally promoted.

## Answer convention

- Product label: Apple Watch users.
- Technical label: `APPLE_WATCH_VIA_APPLE_HEALTH`.
- Do not call them Apple Watch owners; we know the user synced with Apple Watch
  evidence, not current ownership.
- Brand rollups combine Apple Health and Terra paths for brands such as Whoop,
  Oura, Garmin, Fitbit, and Ultrahuman.
- Health-only Apple evidence belongs in `IPHONE_HEALTH_ONLY`.
