# GTV spike suspicious-activity review

Use this when a user asks why GTV spiked and whether the spike-driver transactions look suspicious.

## Scope pattern

1. Start with the regular Slack fast path (`answer_question`) for the raw question.
2. If no shortcut handles it, use `supabase_ad_hoc` with the `gtv` metric contract:
   - successful card spend only;
   - exclude B2C wallet loads;
   - exclude failed/pending/cancelled transactions;
   - exclude refunds/reversals;
   - exclude marketplace reward-reconciliation rows.
3. Interpret "last 3-4 days" as the last 4 completed Asia/Kolkata business days unless the user gives dates. Compare against the prior 4 completed days for trend explanation, and against the prior 30 days for suspicious-review baselines.

## Spike diagnosis fields

For the first explanation, compute:

- daily GTV, transaction count, user count, average transaction size;
- prior-period vs spike-period deltas;
- merchant, user, MCC, and marketplace/non-marketplace contribution deltas.

A common pattern: GTV spikes can be driven by average ticket size and a small set of high-value merchants/users, not by broad transaction/user-count growth.

## Suspicious-review heuristics

When asked if spike transactions are suspicious, do **not** make a fraud verdict from transaction rows alone. Flag clusters for review using indicators such as:

- high value: any transaction >= ₹50k;
- rapid repeats: >=3 same-user / same-merchant transactions on one business day;
- same-user daily GTV >= ₹1L;
- new same-user / same-merchant combo in the spike window versus prior 30 days with >= ₹10k spend;
- sensitive MCCs: `6012`, `6540`, and contextually `4112` when high-value/repeated;
- payment aggregator merchant labels such as `RAZ*...` with high-value or repeated spend;
- missing MCC on marketplace purchase rows.

Useful MCC labels from the 2026-06 GTV review:

- `6012` — financial institutions / quasi-cash-like;
- `6540` — stored value / quasi-cash-like;
- `4112` — passenger railways / travel;
- `4111` — local/suburban transit;
- `5399` — miscellaneous general merchandise;
- `4814` — telecom services;
- `6300` — insurance sales/underwriting;
- `5411` — grocery stores;
- `5262` — other/direct marketing/garden supply style category in available transaction data;
- `NULL` — missing MCC / often marketplace purchase rows.

## Reporting guidance

Keep the Slack answer compact and action-oriented:

- say whether the pattern is merely unusual or worth review, not "fraud";
- show the top flagged clusters by user, merchant, MCC, GTV, txn count, and reason;
- explicitly call out the biggest sensitive-MCC cluster(s);
- include the runner dashboard URL;
- include metric contract id, source tables, date window, timezone, freshness, assumptions, and caveat that device/IP/issuer-risk/chargeback data was not used.

## Query pitfall

When aggregating flags, avoid joining/unnesting flag arrays before summing amounts, because it duplicates transaction amounts once per flag. Aggregate the transaction amounts first, compute boolean flags with `bool_or`, then construct the final flag array in the outer select.
