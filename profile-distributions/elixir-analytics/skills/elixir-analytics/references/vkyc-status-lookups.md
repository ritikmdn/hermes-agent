# vKYC Status Lookups

Use this reference when Slack asks for the vKYC/KYC status of a specific user by email, phone, or user id.

## Source of truth

- `public.profiles`: user identity, `email`, `phone_number`, `kyc_status`, `is_deleted`.
- `public.vkyc`: video KYC attempts. A user may have multiple rows.
- Latest vKYC row: `DISTINCT ON (user_id) ... ORDER BY user_id, coalesce(updated_at, created_at) DESC NULLS LAST`.
- KYC-approved definition from glossary/schema: `profiles.kyc_status = 'FULL_KYC' OR latest_vkyc.kycstatus = '1'`.

## Query shape

For email lookups, match exactly after lower-casing and exclude soft-deleted profiles:

```sql
with target_profile as (
  select
    p.id as user_id,
    trim(coalesce(p.first_name, '') || ' ' || coalesce(p.last_name, '')) as user_name,
    p.email,
    p.kyc_status as profile_kyc_status,
    p.updated_at as profile_updated_at
  from public.profiles p
  where lower(p.email) = lower('<email>')
    and coalesce(p.is_deleted, false) = false
), latest_vkyc as (
  select distinct on (v.user_id)
    v.user_id,
    v.kycstatus,
    v.vcipidstatus,
    v.videoconfstatus,
    v.qastatus,
    v.panstatus,
    v.agent_discrepancy_status,
    v.agentremark,
    v.auditorremark,
    v.description,
    v.schedule_date,
    v.schedule_time,
    v.created_at as vkyc_created_at,
    v.updated_at as vkyc_updated_at
  from public.vkyc v
  join target_profile tp on tp.user_id = v.user_id
  order by v.user_id, coalesce(v.updated_at, v.created_at) desc nulls last
)
select
  tp.user_id,
  nullif(tp.user_name, '') as user_name,
  tp.profile_kyc_status,
  coalesce(lv.kycstatus, 'NO_VKYC_ROW') as latest_vkyc_kycstatus,
  (tp.profile_kyc_status = 'FULL_KYC' or lv.kycstatus = '1') as is_kyc_approved,
  case
    when lv.user_id is null then 'No vKYC row found'
    when lv.kycstatus = '1' then 'vKYC approved'
    else 'vKYC not approved / pending or failed'
  end as interpreted_vkyc_status,
  lv.vcipidstatus,
  lv.videoconfstatus,
  lv.qastatus,
  lv.panstatus,
  lv.agent_discrepancy_status,
  lv.agentremark,
  lv.auditorremark,
  lv.description,
  lv.schedule_date,
  lv.schedule_time,
  lv.vkyc_created_at,
  lv.vkyc_updated_at,
  greatest(
    tp.profile_updated_at,
    coalesce(lv.vkyc_updated_at, lv.vkyc_created_at),
    (select max(updated_at) from public.profiles),
    (select max(coalesce(updated_at, created_at)) from public.vkyc)
  ) as freshness
from target_profile tp
left join latest_vkyc lv on lv.user_id = tp.user_id;
```

## Slack answer guidance

- Treat this as an operational all-time snapshot, not a date-windowed metric.
- Metric contract: `none` unless a KYC metric contract is added later.
- Include source tables, all-time date window, timezone note, freshness, assumptions, and caveat that only `kycstatus = '1'` is source-defined as approved.
- If raw fields conflict (for example `kycstatus = '1'`/`description = Success` but `auditorremark` mentions rejection), lead with the derived status and explicitly flag the retained/conflicting remark for ops verification.
- For dashboard links, exact single-row `resultType: "table"` may produce no URL. Rerun the same lookup as a compact `resultType: "breakdown"` key/value projection and verify the core status fields match before replying.
