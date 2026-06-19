# Gym last-purchase ad hoc pattern

Use this when a Slack question asks who bought the latest gyms / last N gyms.

## Interpretation

Default "gyms" to active gym milestone membership/voucher instances, not the
legacy `profiles.is_gym_member` flag and not generic marketplace orders. One
row is one gym membership/voucher instance, so a user can appear more than once
if they bought multiple gyms.

## Source grounding

Canonical tables:

- `milestone_program_instances` — canonical gym milestone enrollment/purchase
  instance; filter `status = 'active'`; order by `purchase_timestamp` for latest
  purchases.
- `profiles` — join `profiles.id = milestone_program_instances.user_id`;
  always filter `coalesce(profiles.is_deleted, false) = false`.
- `customer_vouchers` — join `customer_vouchers.id =
  milestone_program_instances.program_id` for voucher/payment status, purchase
  city, provider/variant ids.
- `gym_providers`, `gym_variants` — current-state catalog labels.

Metric contract: `gym_milestone_users`.

## SQL shape

```sql
SELECT
  trim(p.first_name || ' ' || coalesce(p.last_name, '')) AS user_name,
  p.id AS user_id,
  gp.name AS gym_provider,
  gv.label AS gym_variant,
  cv.purchase_city,
  mpi.purchase_amount,
  (mpi.purchase_timestamp AT TIME ZONE 'Asia/Kolkata') AS purchased_at_ist,
  mpi.status AS milestone_status,
  cv.payment_status AS voucher_payment_status,
  cv.status AS voucher_status,
  cv.updated_at AS voucher_updated_at,
  mpi.updated_at AS milestone_updated_at
FROM milestone_program_instances mpi
JOIN profiles p ON p.id = mpi.user_id
LEFT JOIN customer_vouchers cv ON cv.id = mpi.program_id
LEFT JOIN gym_providers gp ON gp.id = cv.provider_id
LEFT JOIN gym_variants gv ON gv.id = cv.variant_id
WHERE mpi.status = 'active'
  AND coalesce(p.is_deleted, false) = false
ORDER BY mpi.purchase_timestamp DESC, mpi.id DESC
LIMIT 5
```

Change the `LIMIT` for "last N gyms". For "last N distinct gym buyers", add a
dedupe/window step by `user_id` instead of returning instances.

## Answer metadata

- Date window: all time, ordered by purchase timestamp; no lower bound unless
  user specifies one.
- Timezone: display purchase timestamp in Asia/Kolkata; source timestamps are
  UTC.
- Assumption: active gym milestone membership/voucher instances.
- Caveat: catalog joins are current-state labels; if the user meant all
  marketplace gym orders including cancelled/refunded rows, ask or run a
  separate marketplace-order cut.
