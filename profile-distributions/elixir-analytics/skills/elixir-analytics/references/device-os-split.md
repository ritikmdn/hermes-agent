# Device OS split (iOS vs Android)

Use this pattern when a Slack user asks for the split between iOS and Android devices and does not specify installs/device inventory.

## Interpretation

- Treat “devices” as authenticated app activity by OS in PostHog.
- Metric contract: `active_app_user`.
- Source: `posthog.events`.
- Grain: distinct `properties.userId` grouped by `properties['$os']`.
- Default window: use the analytics runtime’s normal recent-window convention when the user does not specify one; in the seed session this was rolling last 30 days in `Asia/Kolkata`.

## HogQL shape

```sql
select
  properties['$os'] as os,
  count(distinct properties.userId) as distinct_users,
  count() as event_count
from events
where timestamp >= toDateTime('<start>', 'Asia/Kolkata')
  and timestamp < toDateTime('<end>', 'Asia/Kolkata')
  and properties.userId is not null
  and (event = '$pageview' or event = '$screen' or not startsWith(event, '$'))
  and properties['$os'] in ('iOS', 'Android')
group by os
order by distinct_users desc
```

## Response pattern

Return a compact table with:

- OS
- distinct users
- user split percentage
- events
- event split percentage

Include the runner-provided dashboard URL and the normal metadata: metric contract, source table, date window, timezone, freshness, assumptions, and caveats.

## Caveats

- This is app activity, not installed-device inventory.
- Users active on both iOS and Android may count in both OS rows, so the split is row-share over OS-user pairs rather than globally unique users if there is overlap.
- Events without `properties.userId` or without OS in iOS/Android are excluded.
