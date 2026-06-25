"""Commerce-oriented Slack answer formatters for Chandler analytics."""

from __future__ import annotations

from typing import Any

from .commerce_shortcut_requests import _merchant_display_name
from .shortcut_requests import _relative_period_label
from .slack_formatters import (
    _as_float,
    _format_inr,
    _format_inr_decimal,
    _format_number,
    _format_percent_change,
    _metadata_value,
    _period_copula,
    _profile_shortcut_dashboard_line,
    _slack_table_cell,
)


def _top_merchants_card_spend_7d_slack_text(payload: dict[str, Any]) -> str:
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
            "I did not find merchant card spend in the last 7 IST days.",
            "",
            fine_print,
        ]
    else:
        total = sum(_as_float(row.get("gross_spend_inr")) for row in rows)
        txn_count = sum(_as_float(row.get("txn_count")) for row in rows)
        lines = [
            "Top merchants by card spend in the last 7 IST days:",
            f"Total shown: {_format_inr(total)} across {_format_number(txn_count)} txns.",
            "",
            "| Merchant | GTV | Txns | Users | Latest |",
            "|---|---:|---:|---:|---|",
        ]
        for row in rows:
            lines.append(
                "| "
                f"{_slack_table_cell(row.get('merchant_name'), limit=96)} | "
                f"{_format_inr(row.get('gross_spend_inr'))} | "
                f"{_format_number(row.get('txn_count'))} | "
                f"{_format_number(row.get('user_count'))} | "
                f"{_slack_table_cell(row.get('latest_card_txn_at'), limit=32)} |"
            )
        lines.extend(["", fine_print])

    dashboard_line = _profile_shortcut_dashboard_line(payload)
    if dashboard_line:
        lines.extend(["", dashboard_line])
    return "\n".join(lines)


def _top_merchants_card_spend_period_slack_text(
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
    label = _relative_period_label(period_key)
    fine_print = (
        "Fine print: gross card spend / GTV includes successful card spend "
        "only; wallet loads, reward reconciliation rows, deleted users, and "
        "non-card spend are excluded. Refunds/reversals are not netted."
    )

    if not rows:
        lines = [
            f"I did not find merchant card spend for {label} in IST.",
            "",
            fine_print,
        ]
    else:
        total = sum(_as_float(row.get("gross_spend_inr")) for row in rows)
        txn_count = sum(_as_float(row.get("txn_count")) for row in rows)
        lines = [
            f"Top merchants by card spend {label} in IST:",
            f"Total shown: {_format_inr(total)} across {_format_number(txn_count)} txns.",
            "",
            "| Merchant | GTV | Txns | Users | Latest |",
            "|---|---:|---:|---:|---|",
        ]
        for row in rows:
            lines.append(
                "| "
                f"{_slack_table_cell(row.get('merchant_name'), limit=96)} | "
                f"{_format_inr(row.get('gross_spend_inr'))} | "
                f"{_format_number(row.get('txn_count'))} | "
                f"{_format_number(row.get('user_count'))} | "
                f"{_slack_table_cell(row.get('latest_card_txn_at'), limit=32)} |"
            )
        lines.extend(["", fine_print])

    dashboard_line = _profile_shortcut_dashboard_line(payload)
    if dashboard_line:
        lines.extend(["", dashboard_line])
    return "\n".join(lines)


def _merchant_query_from_payload(payload: dict[str, Any]) -> str:
    metadata_value = _metadata_value(payload, "merchantQuery")
    if metadata_value:
        return metadata_value
    rows = payload.get("rows")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and row.get("merchant_query"):
                return str(row.get("merchant_query"))
    return "merchant"


def _merchant_card_spend_7d_slack_text(payload: dict[str, Any]) -> str:
    rows = payload.get("rows")
    first_row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
    merchant_query = _merchant_query_from_payload(payload)
    merchant_name = _merchant_display_name(merchant_query)
    fine_print = (
        "Fine print: gross successful card spend matched merchant_name or "
        f"description containing `{merchant_query}`; excludes deleted users, "
        "wallet loads, reward reconciliation rows, and non-card spend."
    )

    if not first_row:
        lines = [
            f"I did not find {merchant_name} card spend in the last 7 IST days.",
            "",
            fine_print,
        ]
    else:
        lines = [
            (
                f"{merchant_name} card spend in the last 7 IST days was "
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


def _merchant_card_spend_period_slack_text(
    payload: dict[str, Any],
    *,
    period_key: str,
) -> str:
    rows = payload.get("rows")
    first_row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
    merchant_query = _merchant_query_from_payload(payload)
    merchant_name = _merchant_display_name(merchant_query)
    label = _relative_period_label(period_key)
    verb = _period_copula(period_key)
    fine_print = (
        "Fine print: gross successful card spend matched merchant_name or "
        f"description containing `{merchant_query}`; excludes deleted users, "
        "wallet loads, reward reconciliation rows, and non-card spend."
    )

    if not first_row:
        lines = [
            f"I did not find {merchant_name} card spend for {label} in IST.",
            "",
            fine_print,
        ]
    else:
        lines = [
            (
                f"{merchant_name} card spend {label} in IST {verb} "
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


def _merchant_users_this_week_slack_text(payload: dict[str, Any]) -> str:
    raw_rows = payload.get("rows")
    rows = [
        row
        for row in raw_rows
        if isinstance(row, dict)
    ] if isinstance(raw_rows, list) else []
    merchant_query = _merchant_query_from_payload(payload)
    merchant_name = _merchant_display_name(merchant_query)
    fine_print = (
        "Fine print: gross successful card spend matched merchant_name or "
        f"description containing `{merchant_query}`; excludes deleted users, "
        "wallet loads, reward reconciliation rows, and non-card spend."
    )

    if not rows:
        lines = [
            f"I did not find users who spent on {merchant_name} this week.",
            "",
            fine_print,
        ]
    else:
        total = sum(_as_float(row.get("gross_spend_inr")) for row in rows)
        txn_count = sum(_as_float(row.get("txn_count")) for row in rows)
        lines = [
            f"Users who spent on {merchant_name} this week:",
            f"Total shown: {_format_inr(total)} across {_format_number(txn_count)} txns.",
            "",
            "| User | GTV | Txns | Latest |",
            "|---|---:|---:|---|",
        ]
        for row in rows:
            user_name = row.get("user_name") or row.get("user_id") or "Unknown user"
            lines.append(
                "| "
                f"{_slack_table_cell(user_name, limit=64)} | "
                f"{_format_inr(row.get('gross_spend_inr'))} | "
                f"{_format_number(row.get('txn_count'))} | "
                f"{_slack_table_cell(row.get('last_card_txn_at'), limit=32)} |"
            )
        lines.extend(["", fine_print])

    dashboard_line = _profile_shortcut_dashboard_line(payload)
    if dashboard_line:
        lines.extend(["", dashboard_line])
    return "\n".join(lines)


def _merchant_users_period_slack_text(
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
    merchant_query = _merchant_query_from_payload(payload)
    merchant_name = _merchant_display_name(merchant_query)
    label = _relative_period_label(period_key)
    fine_print = (
        "Fine print: gross successful card spend matched merchant_name or "
        f"description containing `{merchant_query}`; excludes deleted users, "
        "wallet loads, reward reconciliation rows, and non-card spend."
    )

    if not rows:
        lines = [
            f"I did not find users who spent on {merchant_name} for {label} in IST.",
            "",
            fine_print,
        ]
    else:
        lines = [
            f"Users who spent on {merchant_name} {label} in IST:",
            "",
            "| User | GTV | Txns | Latest |",
            "|---|---:|---:|---|",
        ]
        for row in rows:
            name = row.get("user_name") or row.get("user_id") or "Unknown user"
            lines.append(
                "| "
                f"{_slack_table_cell(name, limit=48)} | "
                f"{_format_inr(row.get('gross_spend_inr'))} | "
                f"{_format_number(row.get('txn_count'))} | "
                f"{_slack_table_cell(row.get('last_card_txn_at'), limit=32)} |"
            )
        lines.extend(["", fine_print])

    dashboard_line = _profile_shortcut_dashboard_line(payload)
    if dashboard_line:
        lines.extend(["", dashboard_line])
    return "\n".join(lines)


def _swiggy_spend_trend_10d_slack_text(payload: dict[str, Any]) -> str:
    raw_rows = payload.get("rows")
    rows = [
        row
        for row in raw_rows
        if isinstance(row, dict)
    ] if isinstance(raw_rows, list) else []
    rows = sorted(rows, key=lambda row: str(row.get("business_date") or ""))
    fine_print = (
        "Fine print: gross successful card spend where merchant name or "
        "description contains `swiggy`; excludes deleted users, wallet loads, "
        "reward reconciliation rows, and non-card spend."
    )

    if not rows:
        lines = [
            "I did not find Swiggy card spend in the last 10 IST days.",
            "",
            fine_print,
        ]
    else:
        total = sum(_as_float(row.get("swiggy_gtv_inr")) for row in rows)
        average = total / len(rows)
        latest = rows[-1]
        peak = max(rows, key=lambda row: _as_float(row.get("swiggy_gtv_inr")))
        first = rows[0]
        first_to_last = _format_percent_change(
            latest.get("swiggy_gtv_inr"),
            first.get("swiggy_gtv_inr"),
        )
        lines = [
            (
                "Swiggy card spend over the last 10 IST days: "
                f"total {_format_inr(total)}, averaging ~{_format_inr(average)}/day. "
                f"Latest day {latest.get('business_date')} is "
                f"{_format_inr(latest.get('swiggy_gtv_inr'))}"
                + (f" ({first_to_last} vs first day)." if first_to_last != "-" else ".")
            ),
            (
                f"Peak day was {peak.get('business_date')} at "
                f"{_format_inr(peak.get('swiggy_gtv_inr'))}."
            ),
            "",
            "| Date | Swiggy GTV | Txns | Users | DoD |",
            "|---|---:|---:|---:|---:|",
        ]
        previous_gtv: Any = None
        for row in rows:
            lines.append(
                "| "
                f"{row.get('business_date')} | "
                f"{_format_inr(row.get('swiggy_gtv_inr'))} | "
                f"{_format_number(row.get('txn_count'))} | "
                f"{_format_number(row.get('user_count'))} | "
                f"{_format_percent_change(row.get('swiggy_gtv_inr'), previous_gtv)} |"
            )
            previous_gtv = row.get("swiggy_gtv_inr")
        lines.extend(["", fine_print])

    dashboard_line = _profile_shortcut_dashboard_line(payload)
    if dashboard_line:
        lines.extend(["", dashboard_line])
    return "\n".join(lines)


def _gym_milestone_avg_monthly_spend_3mo_slack_text(payload: dict[str, Any]) -> str:
    raw_rows = payload.get("rows")
    rows = [
        row
        for row in raw_rows
        if isinstance(row, dict)
    ] if isinstance(raw_rows, list) else []
    overall = next(
        (row for row in rows if str(row.get("period") or "") == "overall_3mo_avg"),
        rows[0] if rows else None,
    )
    month_rows = sorted(
        [
            row
            for row in rows
            if str(row.get("period") or "") != "overall_3mo_avg"
        ],
        key=lambda row: str(row.get("period") or ""),
    )
    fine_print = (
        "Fine print: cohort is current active gym milestone users with "
        "non-deleted profiles. Spend is gross successful card spend; zero-spend "
        "users are included in avg / gym user."
    )

    if not overall:
        lines = [
            "I did not find monthly card spend for current active gym milestone users.",
            "",
            fine_print,
        ]
    else:
        lines = [
            (
                "For current active gym milestone users, average monthly card spend "
                "over the last 3 completed months was:"
            ),
            "",
            (
                f"*{_format_inr_decimal(overall.get('avg_monthly_spend_per_gym_user'))}* "
                "per gym milestone user / month"
            ),
            (
                f"*{_format_inr_decimal(overall.get('avg_monthly_spend_per_spending_user'))}* "
                "per spending gym user / month"
            ),
            "",
            "| Period | Gym users | Spending users | GTV | Txns | Avg / gym user | Avg / spending user |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        for row in month_rows:
            lines.append(
                "| "
                f"{_slack_table_cell(row.get('period'))} | "
                f"{_format_number(row.get('gym_users'))} | "
                f"{_format_number(row.get('spending_users'))} | "
                f"{_format_inr(row.get('total_gtv'))} | "
                f"{_format_number(row.get('transactions'))} | "
                f"{_format_inr_decimal(row.get('avg_monthly_spend_per_gym_user'))} | "
                f"{_format_inr_decimal(row.get('avg_monthly_spend_per_spending_user'))} |"
            )
        date_window = _metadata_value(payload, "dateWindow")
        if date_window:
            lines.extend(["", f"- *Window:* {date_window}"])
        freshness = overall.get("source_freshness") or _metadata_value(payload, "freshness")
        if freshness:
            lines.append(f"- *Freshness:* {freshness}")
        lines.extend(["", fine_print])

    dashboard_line = _profile_shortcut_dashboard_line(payload)
    if dashboard_line:
        lines.extend(["", dashboard_line])
    return "\n".join(lines)
