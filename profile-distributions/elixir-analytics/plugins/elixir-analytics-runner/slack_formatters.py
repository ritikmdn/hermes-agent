"""Slack answer formatters for Chandler analytics shortcuts."""

from __future__ import annotations

from typing import Any

from .answer_payloads import dashboard_label_for_url, direct_dashboard_link
from .runner_modes import coerce_int
from .shortcut_requests import _relative_period_label


_coerce_int = coerce_int
_dashboard_label_for_url = dashboard_label_for_url
_direct_dashboard_link = direct_dashboard_link


def _format_inr(value: Any) -> str:
    try:
        amount = round(float(value or 0))
    except (TypeError, ValueError):
        amount = 0
    return f"₹{amount:,.0f}"


def _format_inr_decimal(value: Any, *, digits: int = 2) -> str:
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        amount = 0.0
    return f"₹{amount:,.{digits}f}"


def _format_number(value: Any) -> str:
    try:
        return f"{round(float(value or 0)):,.0f}"
    except (TypeError, ValueError):
        return "0"


def _as_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _format_percent_change(current: Any, previous: Any) -> str:
    previous_value = _as_float(previous)
    current_value = _as_float(current)
    if previous_value == 0:
        return "-"
    return f"{((current_value - previous_value) / previous_value) * 100:+.1f}%"


def _profile_shortcut_dashboard_line(payload: dict[str, Any]) -> str | None:
    dashboard_url = _direct_dashboard_link(payload)
    if not dashboard_url:
        return None
    return f"Dashboard: <{dashboard_url}|{_dashboard_label_for_url(dashboard_url)}>"


def _metadata_value(payload: dict[str, Any], key: str) -> str | None:
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return None
    value = metadata.get(key)
    if value is None:
        return None
    return str(value)


def _card_gtv_7d_slack_text(payload: dict[str, Any]) -> str:
    rows = payload.get("rows")
    first_row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
    fine_print = (
        "Fine print: completed-window card GTV is gross successful card spend "
        "only; wallet loads, refunds, and reward reconciliation rows are excluded."
    )

    if not first_row:
        lines = [
            "I did not find card GTV in the last 7 completed IST days.",
            "",
            fine_print,
        ]
    else:
        lines = [
            (
                "Card GTV for the last 7 completed IST days was "
                f"*{_format_inr(first_row.get('gtv'))}*."
            ),
            "",
            f"- *Transactions:* {_format_number(first_row.get('transactions'))}",
            f"- *Card users:* {_format_number(first_row.get('users'))}",
        ]
        date_window = _metadata_value(payload, "dateWindow")
        if date_window:
            lines.append(f"- *Window:* {date_window}")
        freshness = first_row.get("source_freshness") or _metadata_value(payload, "freshness")
        if freshness:
            lines.append(f"- *Freshness:* {freshness}")
        lines.extend(["", fine_print])

    return "\n".join(lines)


def _card_gtv_completed_days_slack_text(payload: dict[str, Any], *, days: int) -> str:
    rows = payload.get("rows")
    first_row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
    fine_print = (
        "Fine print: completed-window card GTV is gross successful card spend "
        "only; wallet loads, refunds, and reward reconciliation rows are excluded."
    )

    if not first_row:
        lines = [
            f"I did not find card GTV in the last {days} completed IST days.",
            "",
            fine_print,
        ]
    else:
        lines = [
            (
                f"Card GTV for the last {days} completed IST days was "
                f"*{_format_inr(first_row.get('gtv'))}*."
            ),
            "",
            f"- *Transactions:* {_format_number(first_row.get('transactions'))}",
            f"- *Card users:* {_format_number(first_row.get('users'))}",
        ]
        date_window = _metadata_value(payload, "dateWindow")
        if date_window:
            lines.append(f"- *Window:* {date_window}")
        freshness = first_row.get("source_freshness") or _metadata_value(payload, "freshness")
        if freshness:
            lines.append(f"- *Freshness:* {freshness}")
        lines.extend(["", fine_print])

    return "\n".join(lines)


def _card_transaction_count_7d_slack_text(payload: dict[str, Any]) -> str:
    rows = payload.get("rows")
    first_row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
    fine_print = (
        "Fine print: completed-window card transactions include successful card "
        "spend rows only; wallet loads, refunds, and reward reconciliation rows are excluded."
    )

    if not first_row:
        lines = [
            "I did not find card transactions in the last 7 completed IST days.",
            "",
            fine_print,
        ]
    else:
        lines = [
            (
                "Card transaction count for the last 7 completed IST days was "
                f"*{_format_number(first_row.get('transactions'))}*."
            ),
            "",
            f"- *GTV:* {_format_inr(first_row.get('gtv'))}",
            f"- *Card users:* {_format_number(first_row.get('users'))}",
        ]
        date_window = _metadata_value(payload, "dateWindow")
        if date_window:
            lines.append(f"- *Window:* {date_window}")
        freshness = first_row.get("source_freshness") or _metadata_value(payload, "freshness")
        if freshness:
            lines.append(f"- *Freshness:* {freshness}")
        lines.extend(["", fine_print])

    return "\n".join(lines)


def _period_copula(period_key: str) -> str:
    return "was" if period_key == "yesterday" else "is"


def _card_gtv_period_slack_text(payload: dict[str, Any], *, period_key: str) -> str:
    rows = payload.get("rows")
    first_row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
    label = _relative_period_label(period_key)
    verb = _period_copula(period_key)
    fine_print = (
        "Fine print: card GTV is gross successful card spend only; wallet "
        "loads, refunds, and reward reconciliation rows are excluded."
    )

    if not first_row:
        lines = [
            f"I did not find card GTV for {label} in IST.",
            "",
            fine_print,
        ]
    else:
        lines = [
            (
                f"Card GTV {label} in IST {verb} "
                f"*{_format_inr(first_row.get('gtv'))}*."
            ),
            "",
            f"- *Transactions:* {_format_number(first_row.get('transactions'))}",
            f"- *Card users:* {_format_number(first_row.get('users'))}",
        ]
        date_window = _metadata_value(payload, "dateWindow")
        if date_window:
            lines.append(f"- *Window:* {date_window}")
        freshness = first_row.get("source_freshness") or _metadata_value(payload, "freshness")
        if freshness:
            lines.append(f"- *Freshness:* {freshness}")
        lines.extend(["", fine_print])

    return "\n".join(lines)


def _card_transaction_count_period_slack_text(
    payload: dict[str, Any],
    *,
    period_key: str,
) -> str:
    rows = payload.get("rows")
    first_row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
    label = _relative_period_label(period_key)
    verb = _period_copula(period_key)
    fine_print = (
        "Fine print: card transaction count includes successful card spend "
        "rows only; wallet loads, refunds, and reward reconciliation rows are excluded."
    )

    if not first_row:
        lines = [
            f"I did not find card transactions for {label} in IST.",
            "",
            fine_print,
        ]
    else:
        lines = [
            (
                f"Card transaction count {label} in IST {verb} "
                f"*{_format_number(first_row.get('transactions'))}*."
            ),
            "",
            f"- *GTV:* {_format_inr(first_row.get('gtv'))}",
            f"- *Card users:* {_format_number(first_row.get('users'))}",
        ]
        date_window = _metadata_value(payload, "dateWindow")
        if date_window:
            lines.append(f"- *Window:* {date_window}")
        freshness = first_row.get("source_freshness") or _metadata_value(payload, "freshness")
        if freshness:
            lines.append(f"- *Freshness:* {freshness}")
        lines.extend(["", fine_print])

    return "\n".join(lines)


def _card_gtv_daily_slack_text(payload: dict[str, Any], *, days: int) -> str:
    raw_rows = payload.get("rows")
    rows = [
        row
        for row in raw_rows
        if isinstance(row, dict)
    ] if isinstance(raw_rows, list) else []
    rows = sorted(rows, key=lambda row: str(row.get("business_date") or ""))
    fine_print = (
        "Fine print: daily GTV is gross successful card spend by Asia/Kolkata "
        "business date, including today-to-date; wallet loads, refunds, and "
        "reward reconciliation rows are excluded."
    )

    if not rows:
        lines = [
            f"I did not find daily card GTV in the last {days} IST days.",
            "",
            fine_print,
        ]
    else:
        total_gtv = sum(_as_float(row.get("gtv")) for row in rows)
        average_gtv = total_gtv / max(len(rows), 1)
        latest = rows[-1]
        first_to_last = _format_percent_change(
            latest.get("gtv"),
            rows[0].get("gtv") if rows else None,
        )
        lines = [
            (
                f"Daily card GTV over the last {days} IST days: "
                f"total {_format_inr(total_gtv)}, averaging "
                f"{_format_inr(average_gtv)}/day."
            ),
            (
                f"Latest day {latest.get('business_date')} is "
                f"{_format_inr(latest.get('gtv'))}"
                + (f" ({first_to_last} vs first day)." if first_to_last != "-" else ".")
            ),
        ]
        date_window = _metadata_value(payload, "dateWindow")
        if date_window:
            lines.append(f"Window: {date_window}")
        lines.extend(
            [
                "",
                "| Date | GTV | Txns | Users | DoD |",
                "|---|---:|---:|---:|---:|",
            ]
        )
        previous_gtv: Any = None
        for row in rows:
            lines.append(
                "| "
                f"{row.get('business_date')} | "
                f"{_format_inr(row.get('gtv'))} | "
                f"{_format_number(row.get('transactions'))} | "
                f"{_format_number(row.get('users'))} | "
                f"{_format_percent_change(row.get('gtv'), previous_gtv)} |"
            )
            previous_gtv = row.get("gtv")
        lines.extend(["", fine_print])

    dashboard_line = _profile_shortcut_dashboard_line(payload)
    if dashboard_line:
        lines.extend(["", dashboard_line])
    return "\n".join(lines)


def _card_gtv_weekly_30d_slack_text(payload: dict[str, Any]) -> str:
    raw_rows = payload.get("rows")
    rows = [
        row
        for row in raw_rows
        if isinstance(row, dict)
    ] if isinstance(raw_rows, list) else []
    rows = sorted(rows, key=lambda row: str(row.get("week_start") or ""))
    fine_print = (
        "Fine print: weekly GTV is gross successful card spend grouped by "
        "Asia/Kolkata business week; wallet loads, refunds, and reward "
        "reconciliation rows are excluded."
    )

    if not rows:
        lines = [
            "I did not find weekly card GTV in the last 30 completed IST days.",
            "",
            fine_print,
        ]
    else:
        total_gtv = sum(_as_float(row.get("gtv")) for row in rows)
        lines = [
            (
                "Weekly card GTV over the last 30 completed IST days: "
                f"total {_format_inr(total_gtv)}."
            ),
        ]
        date_window = _metadata_value(payload, "dateWindow")
        if date_window:
            lines.append(f"Window: {date_window}")
        lines.extend(
            [
                "",
                "| Week | GTV | Txns | Users |",
                "|---|---:|---:|---:|",
            ]
        )
        for row in rows:
            lines.append(
                "| "
                f"{row.get('week_start')} | "
                f"{_format_inr(row.get('gtv'))} | "
                f"{_format_number(row.get('transactions'))} | "
                f"{_format_number(row.get('users'))} |"
            )
        lines.extend(["", fine_print])

    dashboard_line = _profile_shortcut_dashboard_line(payload)
    if dashboard_line:
        lines.extend(["", dashboard_line])
    return "\n".join(lines)


def _top_card_spender_7d_slack_text(payload: dict[str, Any]) -> str:
    rows = payload.get("rows")
    first_row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
    fine_print = (
        "Fine print: gross card spend / GTV over the last 7 Asia/Kolkata days, "
        "including today-to-date. Excludes deleted users, wallet loads, reward "
        "reconciliation rows, and non-card spend; refunds/reversals are not netted."
    )

    if not first_row:
        lines = [
            "I did not find any card spenders in the last 7 IST days.",
            "",
            fine_print,
        ]
    else:
        name = str(first_row.get("user_name") or first_row.get("user_id") or "Unknown user").strip()
        lines = [
            f"Highest spender in the last 7 IST days was *{name}*.",
            "",
            f"- *GTV:* {_format_inr(first_row.get('gross_spend_inr'))}",
            f"- *Transactions:* {_format_number(first_row.get('txn_count'))}",
            f"- *Avg txn value:* {_format_inr(first_row.get('avg_txn_value_inr'))}",
        ]
        first_spend = first_row.get("first_card_txn_at")
        last_spend = first_row.get("last_card_txn_at")
        if first_spend:
            lines.append(f"- *First spend in window:* {first_spend}")
        if last_spend:
            lines.append(f"- *Last spend in window:* {last_spend}")
        lines.extend(["", fine_print])

    dashboard_line = _profile_shortcut_dashboard_line(payload)
    if dashboard_line:
        lines.extend(["", dashboard_line])
    return "\n".join(lines)


def _slack_table_cell(value: Any, *, limit: int = 80) -> str:
    text = str(value or "-").replace("\n", " ").replace("|", "\\|").strip()
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1].rstrip()}…"


def _top_card_spender_period_title(period_key: str, *, ranking: bool) -> str:
    if period_key == "last_7d":
        return (
            "Top card spenders in the last 7 IST days:"
            if ranking
            else "Highest spender in the last 7 IST days"
        )
    label = _relative_period_label(period_key)
    if ranking:
        return f"Top card spenders {label} in IST:"
    return f"Highest card spender {label} in IST"


def _top_card_spender_period_slack_text(
    payload: dict[str, Any],
    *,
    period_key: str,
) -> str:
    rows = payload.get("rows")
    first_row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
    label = _relative_period_label(period_key)
    verb = _period_copula(period_key)
    fine_print = (
        "Fine print: gross card spend / GTV includes successful card spend "
        "only; wallet loads, reward reconciliation rows, deleted users, and "
        "non-card spend are excluded. Refunds/reversals are not netted."
    )

    if not first_row:
        lines = [
            f"I did not find any card spenders for {label} in IST.",
            "",
            fine_print,
        ]
    else:
        name = str(first_row.get("user_name") or first_row.get("user_id") or "Unknown user").strip()
        lines = [
            f"{_top_card_spender_period_title(period_key, ranking=False)} {verb} *{name}*.",
            "",
            f"- *GTV:* {_format_inr(first_row.get('gross_spend_inr'))}",
            f"- *Transactions:* {_format_number(first_row.get('txn_count'))}",
            f"- *Avg txn value:* {_format_inr(first_row.get('avg_txn_value_inr'))}",
        ]
        first_spend = first_row.get("first_card_txn_at")
        last_spend = first_row.get("last_card_txn_at")
        if first_spend:
            lines.append(f"- *First spend in window:* {first_spend}")
        if last_spend:
            lines.append(f"- *Last spend in window:* {last_spend}")
        lines.extend(["", fine_print])

    dashboard_line = _profile_shortcut_dashboard_line(payload)
    if dashboard_line:
        lines.extend(["", dashboard_line])
    return "\n".join(lines)


def _top_card_spenders_period_slack_text(
    payload: dict[str, Any],
    *,
    period_key: str,
) -> str:
    raw_rows = payload.get("rows")
    rows = [
        row
        for row in raw_rows
        if isinstance(row, dict)
    ] if isinstance(raw_rows, list) else []
    fine_print = (
        "Fine print: gross card spend / GTV includes successful card spend "
        "only; wallet loads, reward reconciliation rows, deleted users, and "
        "non-card spend are excluded. Refunds/reversals are not netted."
    )

    if not rows:
        if period_key == "last_7d":
            no_rows = "I did not find any card spenders in the last 7 IST days."
        else:
            no_rows = (
                f"I did not find any card spenders for "
                f"{_relative_period_label(period_key)} in IST."
            )
        lines = [no_rows, "", fine_print]
    else:
        display_rows = rows[:10]
        lines = [
            _top_card_spender_period_title(period_key, ranking=True),
            "",
            "| # | User | GTV | Txns | Avg txn |",
            "|---:|---|---:|---:|---:|",
        ]
        for index, row in enumerate(display_rows, start=1):
            rank = _format_number(row.get("rank") or index)
            name = row.get("user_name") or row.get("user_id") or "Unknown user"
            lines.append(
                "| "
                f"{rank} | "
                f"{_slack_table_cell(name, limit=34)} | "
                f"{_format_inr(row.get('gross_spend_inr'))} | "
                f"{_format_number(row.get('txn_count'))} | "
                f"{_format_inr(row.get('avg_txn_value_inr'))} |"
            )
        if len(rows) > len(display_rows):
            lines.extend(["", f"Showing top {len(display_rows)} of {len(rows)} returned users."])
        lines.extend(["", fine_print])

    dashboard_line = _profile_shortcut_dashboard_line(payload)
    if dashboard_line:
        lines.extend(["", dashboard_line])
    return "\n".join(lines)


def _top_card_spender_7d_spend_breakdown_slack_text(payload: dict[str, Any]) -> str:
    raw_rows = payload.get("rows")
    rows = [
        row
        for row in raw_rows
        if isinstance(row, dict)
    ] if isinstance(raw_rows, list) else []
    fine_print = (
        "Fine print: gross card spend / GTV over the last 7 Asia/Kolkata days, "
        "including today-to-date. Excludes deleted users, wallet loads, reward "
        "reconciliation rows, and non-card spend; refunds/reversals are not netted."
    )

    if not rows:
        lines = [
            "I did not find a spend breakdown for the highest card spender in the last 7 IST days.",
            "",
            fine_print,
        ]
    else:
        name = str(rows[0].get("top_user_name") or "The highest spender").strip()
        total = sum(_as_float(row.get("gross_spend_inr")) for row in rows)
        txn_count = sum(_as_float(row.get("txn_count")) for row in rows)
        lines = [
            f"{name}'s card spend in the last 7 IST days was concentrated in:",
            f"Total shown: {_format_inr(total)} across {_format_number(txn_count)} txns.",
            "",
            "| Merchant | Description | GTV | Txns | Latest |",
            "|---|---|---:|---:|---|",
        ]
        for row in rows:
            lines.append(
                "| "
                f"{_slack_table_cell(row.get('merchant_name'))} | "
                f"{_slack_table_cell(row.get('description'), limit=96)} | "
                f"{_format_inr(row.get('gross_spend_inr'))} | "
                f"{_format_number(row.get('txn_count'))} | "
                f"{_slack_table_cell(row.get('latest_card_txn_at'), limit=32)} |"
            )
        lines.extend(["", fine_print])

    dashboard_line = _profile_shortcut_dashboard_line(payload)
    if dashboard_line:
        lines.extend(["", dashboard_line])
    return "\n".join(lines)
