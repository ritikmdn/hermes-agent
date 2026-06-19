"""Shortcut classifiers and Supabase request builders for Chandler analytics."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .runner_modes import MAX_ROWS, coerce_bool, coerce_int


DEFAULT_FAST_PATH_MAX_ROWS = 25
_coerce_bool = coerce_bool
_coerce_int = coerce_int

def _normalize_question(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def _escape_sql_literal(value: str) -> str:
    return value.replace("'", "''")


def _escape_sql_like(value: str) -> str:
    return (
        _escape_sql_literal(value)
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )





def _india_last_days_window(days: int) -> dict[str, str]:
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    today = now.date()
    from_date = today - timedelta(days=days - 1)
    to_exclusive = today + timedelta(days=1)
    return {
        "from": from_date.isoformat(),
        "toExclusive": to_exclusive.isoformat(),
        "today": today.isoformat(),
        "timezone": "Asia/Kolkata",
    }


def _india_week_to_date_window() -> dict[str, str]:
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    today = now.date()
    week_start = today - timedelta(days=today.weekday())
    to_exclusive = today + timedelta(days=1)
    return {
        "from": week_start.isoformat(),
        "toExclusive": to_exclusive.isoformat(),
        "today": today.isoformat(),
        "timezone": "Asia/Kolkata",
    }


def _india_yesterday_window() -> dict[str, str]:
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    today = now.date()
    yesterday = today - timedelta(days=1)
    return {
        "from": yesterday.isoformat(),
        "toExclusive": today.isoformat(),
        "today": today.isoformat(),
        "timezone": "Asia/Kolkata",
    }


def _india_completed_days_window(days: int) -> dict[str, str]:
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    today = now.date()
    from_date = today - timedelta(days=days)
    return {
        "from": from_date.isoformat(),
        "toExclusive": today.isoformat(),
        "today": today.isoformat(),
        "timezone": "Asia/Kolkata",
    }


def _shift_month(month_start: Any, months: int) -> Any:
    month_index = month_start.month - 1 + months
    year = month_start.year + month_index // 12
    month = month_index % 12 + 1
    return month_start.replace(year=year, month=month, day=1)


def _india_completed_months_window(months: int) -> dict[str, str]:
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    today = now.date()
    current_month = today.replace(day=1)
    from_date = _shift_month(current_month, -months)
    last_month = _shift_month(current_month, -1)
    return {
        "from": from_date.isoformat(),
        "toExclusive": current_month.isoformat(),
        "lastMonth": last_month.isoformat(),
        "today": today.isoformat(),
        "timezone": "Asia/Kolkata",
    }


def _classified_transactions_cte_sql(from_date: str, to_exclusive: str) -> str:
    safe_from = _escape_sql_literal(from_date)
    safe_to_exclusive = _escape_sql_literal(to_exclusive)
    marketplace_refund_suffix = _escape_sql_literal(
        "(_CANCEL_REFUND|_REFUND|_REFUND_CORRECTION)$"
    )
    po_refund = _escape_sql_literal("^PO[^-]+--.+--.+$")
    voucher_refund_attempt = _escape_sql_literal(
        "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}-[0-9]+$"
    )
    business_timestamp = "((t.transaction_timestamp AT TIME ZONE 'UTC') AT TIME ZONE 'Asia/Kolkata')"
    marketplace_refund_credit = f"""(
          coalesce(t.credit_amount, 0) > 0
          and t.transaction_type = 'B2C'
          and t.status = 'PAYMENT_SUCCESS'
          and (
            t.txn_id ~ '{marketplace_refund_suffix}'
            or t.txn_id ~ '{po_refund}'
            or t.txn_id ~ '{voucher_refund_attempt}'
          )
        )"""

    return f"""
    classified_transactions as (
      select
        t.*,
        {business_timestamp} as business_transaction_timestamp,
        coalesce(mo_refund_by_id.id, mo_refund_by_partner_order.id) as marketplace_refund_order_id,
        coalesce(mo_refund_by_id.partner_order_id, mo_refund_by_partner_order.partner_order_id) as marketplace_refund_partner_order_id,
        coalesce(mo_refund_by_id.refund_amount, mo_refund_by_partner_order.refund_amount, 0) as marketplace_refund_amount,
        mo_recon.id as marketplace_recon_order_id,
        case
          when coalesce(t.status, '') in ('PAYMENT_FAILURE', 'PAYMENT_PENDING', 'CANCELLED', 'REFUND_FAILED') then 'ignored'
          when {marketplace_refund_credit}
          then 'marketplace_refund'
          when coalesce(t.credit_amount, 0) > 0
            and t.transaction_type = 'B2C'
            and t.status = 'PAYMENT_SUCCESS'
          then 'wallet_load'
          when coalesce(t.debit_amount, 0) > 0
            and t.transaction_type != 'B2C'
            and t.status = 'PAYMENT_SUCCESS'
            and mo_recon.id is not null
          then 'reward_recon'
          when coalesce(t.debit_amount, 0) > 0
            and t.transaction_type != 'B2C'
            and t.status = 'PAYMENT_SUCCESS'
          then 'card_spend'
          when (
              t.transaction_type in ('REFUND', 'ECOM_REVERSAL')
              or t.status = 'REFUNDED'
            )
          then 'transaction_refund'
          when coalesce(t.credit_amount, 0) > 0 then 'unknown_credit'
          when coalesce(t.debit_amount, 0) > 0 then 'unknown_debit'
          else 'unknown'
        end as business_transaction_type,
        (
          coalesce(t.credit_amount, 0) > 0
          and t.transaction_type = 'B2C'
          and t.status = 'PAYMENT_SUCCESS'
          and not {marketplace_refund_credit}
        ) as is_wallet_load,
        (
          coalesce(t.debit_amount, 0) > 0
          and t.transaction_type != 'B2C'
          and t.status = 'PAYMENT_SUCCESS'
          and mo_recon.id is null
        ) as is_card_spend,
        (
          coalesce(t.debit_amount, 0) > 0
          and t.transaction_type != 'B2C'
          and t.status = 'PAYMENT_SUCCESS'
          and mo_recon.id is not null
        ) as is_reward_reconciliation,
        (
          {marketplace_refund_credit}
        ) as is_marketplace_refund_credit,
        (
          not {marketplace_refund_credit}
          and (
            t.transaction_type in ('REFUND', 'ECOM_REVERSAL')
            or t.status = 'REFUNDED'
          )
          and coalesce(t.status, '') not in ('PAYMENT_FAILURE', 'PAYMENT_PENDING', 'CANCELLED', 'REFUND_FAILED')
        ) as is_transaction_refund
      from transactions t
      left join marketplace_order mo_refund_by_id
        on t.txn_id ~ '{marketplace_refund_suffix}'
       and mo_refund_by_id.id = regexp_replace(t.txn_id, '{marketplace_refund_suffix}', '')
      left join marketplace_order mo_refund_by_partner_order
        on t.txn_id ~ '{po_refund}'
       and mo_refund_by_partner_order.partner_order_id = split_part(t.txn_id, '--', 1)
      left join marketplace_order mo_recon
        on t.txn_id like '%_RECON_%'
       and mo_recon.id = split_part(t.txn_id, '_RECON_', 1)
      where {business_timestamp} >= '{safe_from}'::date
        and {business_timestamp} < '{safe_to_exclusive}'::date
    )
  """


def _relative_period_key(question: str) -> str | None:
    normalized = _normalize_question(question)
    if re.search(r"\btoday\b", normalized):
        return "today"
    if re.search(r"\byesterday\b", normalized):
        return "yesterday"
    if re.search(r"\bthis\s+week\b", normalized):
        return "this_week"
    return None


def _relative_period_window(period_key: str) -> dict[str, str]:
    if period_key == "today":
        return _india_last_days_window(1)
    if period_key == "yesterday":
        return _india_yesterday_window()
    if period_key == "this_week":
        return _india_week_to_date_window()
    raise ValueError(f"Unsupported period: {period_key}")


def _relative_period_label(period_key: str) -> str:
    if period_key == "this_week":
        return "this week"
    return period_key


def _relative_period_assumption(period_key: str) -> str:
    if period_key == "today":
        return "Today includes the current India business day, including today-to-date."
    if period_key == "yesterday":
        return "Yesterday is the completed yesterday India business day."
    if period_key == "this_week":
        return "This week is India business week-to-date, including today-to-date."
    raise ValueError(f"Unsupported period: {period_key}")


def _merchant_card_spend_7d_query(question: str) -> str | None:
    from .commerce_shortcut_requests import _extract_merchant_query

    normalized = _normalize_question(question)
    if not re.search(r"\blast\s*7\s*days?\b", normalized):
        return None
    if not re.search(r"\b(spend|spends|spent|spending|gtv)\b", normalized):
        return None
    return _extract_merchant_query(question)


def _merchant_card_spend_period_query(question: str) -> tuple[str, str] | None:
    from .commerce_shortcut_requests import _extract_merchant_query

    normalized = _normalize_question(question)
    period_key = _relative_period_key(question)
    if not period_key:
        return None
    if re.search(r"\b(which\s+users?|who)\b", normalized):
        return None
    if not re.search(r"\b(spend|spends|spent|spending|gtv)\b", normalized):
        return None
    merchant_query = _extract_merchant_query(question)
    if not merchant_query:
        return None
    return merchant_query, period_key


def _merchant_users_this_week_query(question: str) -> str | None:
    from .commerce_shortcut_requests import _extract_merchant_query

    normalized = _normalize_question(question)
    if not re.search(r"\bthis\s+week\b", normalized):
        return None
    if not re.search(r"\b(which\s+users?|who)\b", normalized):
        return None
    if not re.search(r"\b(spend|spends|spent|spending)\b", normalized):
        return None
    return _extract_merchant_query(question)


def _merchant_users_period_query(question: str) -> tuple[str, str] | None:
    from .commerce_shortcut_requests import _extract_merchant_query

    normalized = _normalize_question(question)
    period_key = _relative_period_key(question)
    if not period_key:
        return None
    if not re.search(r"\b(which\s+users?|who)\b", normalized):
        return None
    if not re.search(r"\b(spend|spends|spent|spending)\b", normalized):
        return None
    merchant_query = _extract_merchant_query(question)
    if not merchant_query:
        return None
    return merchant_query, period_key


def _top_merchants_card_spend_period_key(question: str) -> str | None:
    normalized = _normalize_question(question)
    if not re.search(r"\b(merchant|merchants|vendor|vendors)\b", normalized):
        return None
    if not re.search(r"\b(top|biggest|largest|highest)\b", normalized):
        return None
    if not re.search(r"\b(card\s+)?(spend|spent|gtv)\b", normalized):
        return None
    return _relative_period_key(question)


def _card_gtv_daily_days(question: str) -> int | None:
    normalized = _normalize_question(question)
    if not re.search(r"\b(gtv|card\s+spend)\b", normalized):
        return None
    if not re.search(
        r"\b(daily|by\s+day|day\s*wise|day-by-day|day\s+by\s+day|trend)\b",
        normalized,
    ):
        return None
    days_match = re.search(r"\blast\s+(\d{1,3})\s+days?\b", normalized)
    if not days_match:
        return None
    days = int(days_match.group(1))
    if days not in {7, 30}:
        return None
    return days


def _definition_change_intent(question: str) -> bool:
    normalized = _normalize_question(question)
    return bool(
        re.search(
            r"\b(change|update|modify|define|definition|instead|rather\s+than|"
            r"use\s+.+\s+as|count\s+as|exclude|include)\b",
            normalized,
        )
    )


def _card_gtv_completed_days(question: str) -> int | None:
    from .commerce_shortcut_requests import _extract_simple_merchant_query

    normalized = _normalize_question(question)
    if not re.search(r"\b(gtv|card\s+spend)\b", normalized):
        return None
    if re.search(
        r"\b(daily|by\s+day|day\s*wise|day-by-day|day\s+by\s+day|trend|"
        r"weekly|by\s+week|transaction|transactions|txn|txns|users?|"
        r"spender|spenders|merchant|merchants|vendor|vendors|top|highest|"
        r"biggest|largest)\b",
        normalized,
    ):
        return None
    if _definition_change_intent(question) or _extract_simple_merchant_query(question):
        return None
    days_match = re.search(r"\b(?:over\s+(?:the\s+)?)?last\s+(\d{1,3})\s+days?\b", normalized)
    if not days_match:
        days_match = re.search(r"\b(?:past|previous)\s+(\d{1,3})\s+days?\b", normalized)
    if not days_match:
        return None
    days = int(days_match.group(1))
    if days == 7 or days < 2 or days > 31:
        return None
    return days


def _card_gtv_period_key(question: str) -> str | None:
    from .commerce_shortcut_requests import _extract_simple_merchant_query

    normalized = _normalize_question(question)
    if not re.search(r"\b(gtv|card\s+spend)\b", normalized):
        return None
    if re.search(
        r"\b(daily|by\s+day|day\s*wise|day-by-day|day\s+by\s+day|trend|weekly|by\s+week)\b",
        normalized,
    ):
        return None
    if re.search(r"\blast\s+\d{1,3}\s+days?\b", normalized):
        return None
    if _extract_simple_merchant_query(question):
        return None
    return _relative_period_key(question)


def _matches_card_gtv_weekly_30d(question: str) -> bool:
    normalized = _normalize_question(question)
    return bool(
        re.search(r"\bgtv\b", normalized)
        and re.search(r"\b(last\s+)?30\s+days?\b", normalized)
        and re.search(r"\b(week|weekly)\b", normalized)
    )


def _matches_card_gtv_7d(question: str) -> bool:
    normalized = _normalize_question(question)
    return bool(
        re.search(r"\bgtv\b", normalized)
        and re.search(r"\blast\s+7\s+days?\b", normalized)
        and not re.search(
            r"\b(week|weekly|daily|by\s+day|day\s*wise|day-by-day|day\s+by\s+day|trend)\b",
            normalized,
        )
    )


def _matches_card_transaction_count_7d(question: str) -> bool:
    normalized = _normalize_question(question)
    if not re.search(r"\blast\s+7\s+days?\b", normalized):
        return False
    if not re.search(r"\bcard\b", normalized):
        return False
    return bool(
        re.search(r"\b(transaction|transactions|txn|txns)\b", normalized)
        and re.search(r"\b(count|counts|number|many|volume)\b", normalized)
    )


def _card_transaction_count_period_key(question: str) -> str | None:
    normalized = _normalize_question(question)
    if not re.search(r"\bcard\b", normalized):
        return None
    if not re.search(r"\b(transaction|transactions|txn|txns)\b", normalized):
        return None
    if not re.search(r"\b(count|counts|number|many|volume)\b", normalized):
        return None
    if re.search(r"\blast\s+\d{1,3}\s+days?\b", normalized):
        return None
    return _relative_period_key(question)


def _is_top_card_spenders_rank_intent(normalized: str) -> bool:
    return bool(
        re.search(r"\btop\b[\s\S]*\b(card\s+)?(spenders?|users?)\b", normalized)
        or re.search(
            r"\bwhich\s+users?\b[\s\S]*\b(spend|spends|spent|spending)\b[\s\S]*\bmost\b",
            normalized,
        )
        or re.search(
            r"\bshow\b[\s\S]*\btop\s+users?\b[\s\S]*\b(card\s+)?(spend|spent|gtv)\b",
            normalized,
        )
    )


def _is_top_card_spender_singular_intent(normalized: str) -> bool:
    return bool(
        re.search(r"\bwho\b[\s\S]*\bspent\b[\s\S]*\bmost\b", normalized)
        or re.search(r"\b(highest|biggest|top)\b[\s\S]*\b(card\s+)?spender\b", normalized)
        or re.search(r"\bhighest\s+spender\b", normalized)
    )


def _matches_top_card_spenders_7d(question: str) -> bool:
    normalized = _normalize_question(question)
    if not re.search(r"\blast\s*7\s*days?\b", normalized):
        return False
    if not re.search(r"\b(card|spend|spends|spent|spending|spender|spenders|gtv)\b", normalized):
        return False
    return _is_top_card_spenders_rank_intent(normalized)


def _top_card_spender_period_match(question: str) -> tuple[str, bool] | None:
    from .commerce_shortcut_requests import _extract_simple_merchant_query

    normalized = _normalize_question(question)
    period_key = _relative_period_key(question)
    if not period_key:
        return None
    if _extract_simple_merchant_query(question):
        return None
    if not re.search(r"\b(card|spend|spends|spent|spending|spender|spenders|gtv)\b", normalized):
        return None
    if _is_top_card_spenders_rank_intent(normalized):
        return period_key, True
    if _is_top_card_spender_singular_intent(normalized):
        return period_key, False
    return None


def _matches_top_card_spender_7d(question: str) -> bool:
    normalized = _normalize_question(question)
    if not re.search(r"\blast\s*7\s*days?\b", normalized):
        return False
    return bool(
        re.search(r"\bwho\b[\s\S]*\bspent\b[\s\S]*\bmost\b", normalized)
        or re.search(r"\btop\b[\s\S]*\b(card\s+)?spenders?\b", normalized)
        or re.search(r"\b(highest|biggest)\b[\s\S]*\b(card\s+)?spenders?\b", normalized)
        or re.search(r"\bhighest\s+spender\b", normalized)
    )


def _matches_top_card_spender_7d_spend_breakdown(question: str) -> bool:
    normalized = _normalize_question(question)
    if not re.search(r"\blast\s*7\s*days?\b", normalized):
        return False
    if not re.search(r"\b(highest|top)\s+(card\s+)?spender\b", normalized):
        return False
    return bool(
        re.search(r"\bwhat\b[\s\S]*\b(spend|spent)\b[\s\S]*\bon\b", normalized)
        or re.search(r"\bwhere\b[\s\S]*\b(spend|spent)\b", normalized)
        or re.search(r"\bspend\s+breakdown\b", normalized)
    )


def _matches_top_merchants_card_spend_7d(question: str) -> bool:
    normalized = _normalize_question(question)
    if not re.search(r"\blast\s*7\s*days?\b", normalized):
        return False
    if not re.search(r"\b(merchant|merchants|vendor|vendors)\b", normalized):
        return False
    return bool(
        re.search(r"\b(top|biggest|largest|highest)\b", normalized)
        and re.search(r"\b(card\s+)?(spend|spent|gtv)\b", normalized)
    )


def _matches_swiggy_spend_trend_10d(question: str) -> bool:
    normalized = _normalize_question(question)
    if not re.search(r"\bswiggy\b", normalized):
        return False
    if not re.search(r"\blast\s*10\s*days?\b", normalized):
        return False
    return bool(
        re.search(r"\b(spend|spends|spent|gtv)\b", normalized)
        and re.search(r"\b(evolved|trend|trending|daily|over|movement|changed)\b", normalized)
    )


def _matches_gym_milestone_avg_monthly_spend_3mo(question: str) -> bool:
    if _definition_change_intent(question):
        return False
    normalized = _normalize_question(question)
    if not re.search(r"\bgym\b", normalized):
        return False
    if not re.search(r"\bmilestone\b", normalized):
        return False
    if not re.search(r"\b(avg|average|mean)\b", normalized):
        return False
    if not re.search(r"\bmonthly\b", normalized):
        return False
    if not re.search(r"\b(spend|spends|spent|spending|gtv)\b", normalized):
        return False
    return bool(
        re.search(
            r"\b(?:over\s+(?:the\s+)?)?last\s+(?:3|three)\s+months?\b",
            normalized,
        )
        or re.search(r"\b(?:past|previous)\s+(?:3|three)\s+months?\b", normalized)
    )


def _profile_answer_question_shortcut(
    args: dict[str, Any],
    *,
    mode: str,
) -> tuple[list[str], str, str, dict[str, Any]] | None:
    from .card_shortcut_requests import (
        _card_gtv_7d_request,
        _card_gtv_completed_days_request,
        _card_gtv_daily_request,
        _card_gtv_period_request,
        _card_gtv_weekly_30d_request,
        _card_transaction_count_7d_request,
        _card_transaction_count_period_request,
        _top_card_spender_7d_request,
        _top_card_spender_7d_spend_breakdown_request,
        _top_card_spenders_7d_request,
        _top_card_spenders_period_request,
    )

    from .commerce_shortcut_requests import (
        _gym_milestone_avg_monthly_spend_3mo_request,
        _merchant_card_spend_7d_request,
        _merchant_card_spend_period_request,
        _merchant_users_period_request,
        _merchant_users_this_week_request,
        _swiggy_spend_trend_10d_request,
        _top_merchants_card_spend_7d_request,
        _top_merchants_card_spend_period_request,
    )

    if mode != "answer_question":
        return None
    question = str(args.get("question") or "").strip()
    if not question:
        return None

    shortcut: str | None = None
    request: dict[str, Any] | None = None
    daily_gtv_days = _card_gtv_daily_days(question)
    if daily_gtv_days:
        shortcut = f"card_gtv_daily_{daily_gtv_days}d"
        request = _card_gtv_daily_request(question, days=daily_gtv_days)
    elif completed_gtv_days := _card_gtv_completed_days(question):
        shortcut = f"card_gtv_last_{completed_gtv_days}d"
        request = _card_gtv_completed_days_request(question, days=completed_gtv_days)
    elif _matches_gym_milestone_avg_monthly_spend_3mo(question):
        shortcut = "gym_milestone_avg_monthly_spend_3mo"
        request = _gym_milestone_avg_monthly_spend_3mo_request(question)
    elif _matches_top_merchants_card_spend_7d(question):
        shortcut = "top_merchants_card_spend_7d"
        request = _top_merchants_card_spend_7d_request(question)
    elif period_key := _top_merchants_card_spend_period_key(question):
        shortcut = f"top_merchants_card_spend_{period_key}"
        request = _top_merchants_card_spend_period_request(
            question,
            period_key=period_key,
        )
    elif merchant_query := _merchant_users_this_week_query(question):
        shortcut = "merchant_users_this_week"
        request = _merchant_users_this_week_request(
            question,
            merchant_query=merchant_query,
        )
    elif merchant_users_period_match := _merchant_users_period_query(question):
        merchant_query, period_key = merchant_users_period_match
        shortcut = f"merchant_users_{period_key}"
        request = _merchant_users_period_request(
            question,
            merchant_query=merchant_query,
            period_key=period_key,
        )
    elif merchant_period_match := _merchant_card_spend_period_query(question):
        merchant_query, period_key = merchant_period_match
        shortcut = f"merchant_card_spend_{period_key}"
        request = _merchant_card_spend_period_request(
            question,
            merchant_query=merchant_query,
            period_key=period_key,
        )
    elif merchant_query := _merchant_card_spend_7d_query(question):
        shortcut = "merchant_card_spend_7d"
        request = _merchant_card_spend_7d_request(
            question,
            merchant_query=merchant_query,
        )
    elif period_key := _card_gtv_period_key(question):
        shortcut = f"card_gtv_{period_key}"
        request = _card_gtv_period_request(question, period_key=period_key)
    elif _matches_card_gtv_weekly_30d(question):
        shortcut = "card_gtv_weekly_30d"
        request = _card_gtv_weekly_30d_request(question)
    elif period_key := _card_transaction_count_period_key(question):
        shortcut = f"card_transaction_count_{period_key}"
        request = _card_transaction_count_period_request(
            question,
            period_key=period_key,
        )
    elif _matches_card_transaction_count_7d(question):
        shortcut = "card_transaction_count_7d"
        request = _card_transaction_count_7d_request(question)
    elif _matches_card_gtv_7d(question):
        shortcut = "card_gtv_7d"
        request = _card_gtv_7d_request(question)
    elif _matches_top_card_spenders_7d(question):
        shortcut = "top_card_spenders_7d"
        request = _top_card_spenders_7d_request(question)
    elif top_spender_period_match := _top_card_spender_period_match(question):
        period_key, is_ranking = top_spender_period_match
        shortcut_prefix = "top_card_spenders" if is_ranking else "top_card_spender"
        shortcut = f"{shortcut_prefix}_{period_key}"
        request = _top_card_spenders_period_request(question, period_key=period_key)
    elif _matches_top_card_spender_7d_spend_breakdown(question):
        shortcut = "top_card_spender_7d_spend_breakdown"
        request = _top_card_spender_7d_spend_breakdown_request(question)
    elif _matches_top_card_spender_7d(question):
        shortcut = "top_card_spender_7d"
        request = _top_card_spender_7d_request(question)
    elif _matches_swiggy_spend_trend_10d(question):
        shortcut = "swiggy_spend_trend_10d"
        request = _swiggy_spend_trend_10d_request(question)

    if not shortcut or request is None:
        return None

    max_rows = _coerce_int(
        args.get("max_rows"),
        DEFAULT_FAST_PATH_MAX_ROWS,
        minimum=1,
        maximum=MAX_ROWS,
    )
    if shortcut == "card_gtv_daily_30d":
        max_rows = max(max_rows, 30)
    command = [
        "node",
        "--import",
        "tsx",
        "scripts/run-ad-hoc-query.ts",
        "--max-rows",
        str(max_rows),
    ]
    if _coerce_bool(args.get("dry_run")):
        command.append("--dry-run")

    return command, json.dumps(request, ensure_ascii=False), shortcut, request
