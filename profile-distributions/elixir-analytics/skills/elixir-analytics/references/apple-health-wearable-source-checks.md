# Apple Health wearable/source metadata checks

Use this when a Slack question asks whether Apple Health users can be split into Apple Watch vs iPhone-only, or when the user hints that Apple wearable data may live in a specific column.

## Current production-table finding

In the analytics tables checked during the June 2026 Slack session, Apple Health wearable/source metadata was **not populated** in the obvious production columns:

- `health_data_providers.provider_metadata`: Apple Health rows had no non-empty JSON.
- `health_data_providers.scopes`: Apple Health rows had no populated scopes.
- `health_data_activity.data`: populated for Apple Health, but top-level keys were activity metrics only (`activity_type_name`, `avg_hr_bpm`, `calories_burned`, `distance_meters`, `duration_seconds`, `start_time`, `end_time`, `hr_samples`, `max_hr_bpm`). No source/device/watch terms found.
- `health_data_body.data`, `health_data_daily_summary.data`, `health_data_sleep.data`: no Apple Health rows in that check.
- `health_data_provider_webhook_events.payload`: no Apple Health rows in that check.

Do **not** state categorically that Apple Watch data does not exist upstream. Say: "I don't see Apple Watch/source metadata populated in the current analytics tables I can query; if it lives in another upstream/raw table or column, share the table/column and I can rerun."

## Workflow

1. Run the normal analytics fast path first (`run-analytics-question.ts` / `answer_question` equivalent). If it falls through, use the planner, then a Supabase ad hoc runner request.
2. Inspect schema/catalog for health tables before querying unfamiliar columns.
3. Search candidate column names in `information_schema.columns` for health tables:
   - `source`, `device`, `watch`, `wear`, `provider`, `metadata`, `scope`.
4. Check Apple Health candidate JSON columns:
   - `health_data_providers.provider_metadata`
   - `health_data_providers.scopes`
   - `health_data_activity.data`
   - `health_data_body.data`
   - `health_data_daily_summary.data`
   - `health_data_sleep.data`
   - `health_data_provider_webhook_events.payload`
5. Search both key names and text values for source/device/watch terms. Top-level JSON key enumeration is useful but not enough; if needed, do a recursive JSON key/value probe or a direct text search over bounded rows.
6. Join `health_data_providers` to `profiles` and filter `coalesce(profiles.is_deleted, false) = false` for user counts.
7. For active Apple Health counts, require `health_data_providers.provider='APPLE_HEALTH'`, `active=true`, and `disconnected_at is null`; verify actual sync by joining to provider-backed health rows via `(provider, provider_user_id)`.
8. Always include a dashboard link from the deterministic ad hoc runner.

## Answer convention

- If Apple metadata is empty in analytics tables, correct the earlier broad caveat rather than defending it: "You're right to ask; I checked the raw/candidate columns."
- Keep the distinction clear:
  - `APPLE_HEALTH` = Apple Health sync, not necessarily Apple Watch.
  - Terra/Android wearable brand can come from `health_data_providers.provider_metadata->>'provider'` when populated.
  - Apple Watch-vs-iPhone cannot be classified unless a populated source/device column is identified.

## Compact diagnostic SQL snippets

Column search:

```sql
select table_name, column_name, data_type
from information_schema.columns
where table_schema = 'public'
  and table_name like 'health_data%'
  and (
    column_name ilike '%source%' or column_name ilike '%device%'
    or column_name ilike '%watch%' or column_name ilike '%wear%'
    or column_name ilike '%provider%' or column_name ilike '%metadata%'
    or column_name ilike '%scope%'
  )
order by table_name, ordinal_position;
```

Provider metadata/scopes summary:

```sql
select
  count(distinct hdp.user_id) as active_apple_health_users,
  count(*) filter (where hdp.provider_metadata is not null and hdp.provider_metadata <> '{}'::jsonb) as rows_with_provider_metadata,
  count(*) filter (where hdp.scopes is not null and length(trim(hdp.scopes)) > 0) as rows_with_scopes,
  max(hdp.updated_at) as latest_provider_updated_at
from health_data_providers hdp
join profiles p on p.id = hdp.user_id
where hdp.provider = 'APPLE_HEALTH'
  and coalesce(hdp.active, false) = true
  and hdp.disconnected_at is null
  and coalesce(p.is_deleted, false) = false;
```

Candidate JSON text search pattern:

```sql
select
  'health_data_activity.data' as location,
  count(*) as apple_rows,
  count(*) filter (
    where data::text ilike '%watch%'
       or data::text ilike '%device%'
       or data::text ilike '%source%'
       or data::text ilike '%model%'
       or data::text ilike '%manufacturer%'
  ) as rows_with_source_terms,
  left(min(data::text), 220) as sample_text
from health_data_activity
where provider = 'APPLE_HEALTH';
```
