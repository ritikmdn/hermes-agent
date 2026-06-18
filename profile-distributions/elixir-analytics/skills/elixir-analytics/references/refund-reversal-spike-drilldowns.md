# Refund / reversal spike drilldowns

Use this when a broad WoW/last-week change answer identifies refunds/reversals as the top mover and the user asks what caused it, whether it was one person, or whether the user benefited via rewards.

## Drilldown sequence

1. Keep the same date window from the parent answer. For "last week" in Slack, prefer the last completed 7 business days in `Asia/Kolkata` and compare to the prior 7 days unless the parent answer used a different window.
2. First break refunds/reversals by:
   - refund kind: `card_refund`, `marketplace_refund_credit`, `ecom_reversal`, `status_refunded`
   - merchant/description
   - largest current-window rows
3. If asked whether it was one person, aggregate current-window refunds by `transactions.user_id`:
   - distinct users, row count, total value
   - top user value/share
   - top 2 combined value/share when concentration is high
   - use masked IDs by default in broad answers (`left(uuid,8)…right(uuid,4)`).
4. If the user asks "who", it is acceptable in this internal Slack analytics context to join `profiles` and return name plus masked phone, but keep the output sober and limited to the implicated top users.
5. If asked whether it benefits them via rewards:
   - Join `rewards` by `user_id` for the same window for credited/debited totals and reward sources.
   - Separately check direct links from `rewards.transaction_id::text = transactions.id::text` for the refund rows.
   - If direct links are zero, say so clearly; do not overstate fraud/abuse.
   - Drill into reward sources for the top user to separate reward credits from `REFUND Transaction Partial Reversal` debits.

## Interpretation pattern

Good summary shape:

- "Not a single user, but highly concentrated" when top users dominate.
- Name the top user(s) only when asked.
- Quantify whether rewards are net positive or reversed: current credits, current debits, net, and any direct reward rows linked to refund transaction IDs.
- If reward rows are not directly linked to refund rows, caveat that a full abuse check must pair each refund back to the original purchase/authorization and original reward accrual.

## SQL pitfalls

- `rewards.transaction_id` may be text while `transactions.id` is uuid; cast both sides to text for direct linkage.
- Avoid `ORDER BY` expressions directly over a `UNION` result; wrap the union in a CTE/subquery first.
- Reconstructing a UUID from a masked ID is invalid. For follow-up drilldowns, dynamically select the target user from the same refund ranking query or carry the full ID only inside the query pipeline.
- Keep PII minimal: masked user IDs by default; names/masked phones only when the user explicitly asks who the user is.
