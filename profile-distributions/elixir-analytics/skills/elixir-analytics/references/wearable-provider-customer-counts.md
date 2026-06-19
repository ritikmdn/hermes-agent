# Wearable / smartwatch / band customer counts

Use this when Slack asks how many Elixir customers have wearables, smartwatches, or bands such as Apple Watch, Samsung Galaxy Watch, Whoop, Fitbit, Garmin, Oura, etc.

## Query semantics

- Count distinct non-deleted customers from `health_data_providers.user_id` joined to `profiles.id`.
- Exclude deleted profiles with `coalesce(profiles.is_deleted, false) = false`.
- Count only active provider connections: `coalesce(health_data_providers.active, false) = true` and `disconnected_at is null`.
- Use `provider_metadata->>'provider'` as the Terra brand split when populated; otherwise show raw `provider` with `unknown`/null metadata.
- Include a `TOTAL_DISTINCT_CUSTOMERS` row because provider rows can overlap when one customer has multiple providers.
- Freshness should be `max(health_data_providers.updated_at)`.

## Important caveats

- `APPLE_HEALTH` is Apple Health sync, not proof of Apple Watch. Do not label all Apple Health users as Apple Watch users unless a populated source/device/watch metadata column is identified.
- Terra brand counts can identify brands such as `SAMSUNG`, `GARMIN`, `FITBIT`, `COROS`, `GOOGLE` when metadata is populated. Google may mean Google Fit/Health Connect style provider metadata, not necessarily a physical wearable.
- If the user asks for specific brands that are absent from the result (e.g. Whoop/Oura), say they did not appear in the active provider metadata returned by the query, not that no upstream integration exists.

## Supabase ad hoc runner pattern

Build a full `AdHocQueryRequest`. The runner expects string fields for `assumptions`, `caveats`, and `conventionAdded` — not arrays/booleans.

Use `resultType: "breakdown"`, `sources: ["public.health_data_providers", "public.profiles"]`, `timezone: "UTC"`, and `repeatPromoteCandidate: true`.

Postgres pitfall: if using `UNION ALL` and ordering by an expression, wrap the union in a CTE/subquery before the final `ORDER BY`.

```sql
with active_providers as (
  select
    hdp.user_id,
    hdp.provider,
    hdp.provider_metadata,
    hdp.updated_at
  from public.health_data_providers hdp
  join public.profiles p on p.id = hdp.user_id
  where coalesce(p.is_deleted, false) = false
    and coalesce(hdp.active, false) = true
    and hdp.disconnected_at is null
), provider_counts as (
  select
    provider,
    nullif(provider_metadata->>'provider', '') as metadata_provider,
    count(distinct user_id) as customers,
    count(*) as active_provider_rows,
    max(updated_at) as latest_provider_updated_at
  from active_providers
  group by 1, 2
), total as (
  select
    'TOTAL_DISTINCT_CUSTOMERS'::text as provider,
    null::text as metadata_provider,
    count(distinct user_id) as customers,
    count(*) as active_provider_rows,
    max(updated_at) as latest_provider_updated_at
  from active_providers
), unioned as (
  select * from total
  union all
  select * from provider_counts
)
select *
from unioned
order by
  case when provider = 'TOTAL_DISTINCT_CUSTOMERS' then 0 else 1 end,
  customers desc,
  provider asc,
  metadata_provider asc;
```

## Slack answer shape

- Lead with the deduped total distinct customers.
- Use a compact code-block table when provider labels/columns are long.
- Include the runner dashboard URL.
- Include metric contract: `none / ad hoc provider-connectivity query`, source tables, date window, timezone, freshness, assumptions, and caveats.
