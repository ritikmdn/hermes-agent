# GTV vs Visa / LivQuik reconciliation

Use this when reconciling Elixir GTV against Visa network / LivQuik spreadsheet totals.

## Spreadsheet formula pattern observed

Ritik's Visa/LivQuik sheet calculates daily GTV as:

- **GTV non-marketplace**: sum `Txn Amount` for rows whose `Description` matches the non-marketplace transaction labels, bounded by `Txn Date >= start` and `< end + 1`.
- **GTV marketplace**: sum `Txn Amount` for rows whose `Description` matches marketplace labels, same date window, and `Transaction to be eliminated <> "Yes"`.

In the 2026-06-06 reconciliation, the attached LivQuik raw file matched Visa exactly only after excluding `ELIXIR-GYFTER` marketplace rows:

- Non-marketplace `Description = Transaction`: ₹490,983
- Marketplace `Description = Marketplace purchase`: ₹9,015
- Marketplace `Description = ELIXIR-GYFTER`: ₹5,526
- Formula including Gyfter: ₹505,524
- Visa reported: ₹499,998
- Formula excluding Gyfter: ₹499,998

## Diagnostic pattern

When Elixir GTV is above Visa by a small marketplace-sized amount:

1. Ask for or inspect the LivQuik raw export if available.
2. Recompute the user's formula deterministically from the file:
   - parse tab-delimited exports;
   - sum `Txn Amount`, not necessarily `Amount`, because `Txn Amount` may be rounded to rupees while Supabase `debit_amount` may retain paise;
   - bucket by `Description`, `Marketplace`, `TransactionType`, `Status`, `CR/DR Flag`, and `Transaction to be eliminated`.
3. Compare:
   - non-marketplace `Transaction` rows;
   - marketplace `Marketplace purchase` rows;
   - marketplace `ELIXIR-GYFTER` rows separately;
   - refunds/reversals and wallet loads separately.
4. If excluding `ELIXIR-GYFTER` exactly closes the gap, report that as the reconciliation cause and recommend clarifying whether Gyfter should be part of source-of-truth GTV or excluded to align with Visa.

## Reporting guidance

Keep the answer compact and reconciliation-first:

- State Elixir total, Visa total, and difference.
- Show the bucket table with the exact rows/sums causing the gap.
- Mention whether timezone, failed transactions, refunds, wallet loads, or marketplace rows explain the difference.
- If a temp CSV/artifact is created for Slack delivery, delete it when the user says the issue is resolved.

## Caveats

- Visa/network totals may use authorization date, settlement/clearing date, or a different inclusion list than Elixir's `gtv` metric contract.
- Without Visa transaction-level IDs, exact row-level exclusion cannot be proven from aggregates alone; attached raw LivQuik data can close that gap.
- Current Elixir GTV contract includes successful `PURCHASE`/marketplace rows unless the transaction-semantics helper classifies them as reward reconciliation. Gyfter inclusion/exclusion is therefore a business-definition decision, not just a SQL bug.
