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
    if re.search(r"\bswiggy\b", normalized):
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
    if period_key == "this_week" and re.search(r"\bswiggy\b", normalized):
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


def _matches_wearable_identified_customer_count(question: str) -> bool:
    if _definition_change_intent(question):
        return False
    normalized = _normalize_question(question)
    if _requires_combined_active_wearable_user_denominator(normalized):
        return False
    if not re.search(
        r"\b(how\s+many|count|counts|number|total|users?|customers?)\b",
        normalized,
    ):
        return False
    return bool(
        re.search(
            r"\b(wearables?|smart\s*watch(?:es)?|watch(?:es)?|bands?|"
            r"fitness\s+trackers?|whoop|fitbit|garmin|oura|coros|ultrahuman)\b",
            normalized,
        )
    )


def _requires_combined_active_wearable_user_denominator(normalized_question: str) -> bool:
    """Avoid answering combined-active wearable questions from one Supabase shortcut."""
    if not re.search(
        r"\b(active\s+users?|users?\s+active|active\s+in\s+(?:the\s+)?last|"
        r"active\s+last)\b",
        normalized_question,
    ):
        return False
    if re.search(
        r"\b(sync|synced|syncing|health\s+data|health\s+sync|wearable\s+sync)\b",
        normalized_question,
    ):
        return False
    return bool(
        re.search(
            r"\b(wearables?|smart\s*watch(?:es)?|watch(?:es)?|bands?|"
            r"fitness\s+trackers?|whoop|fitbit|garmin|oura|coros|"
            r"ultrahuman|samsung|apple\s+watch)\b",
            normalized_question,
        )
    )


def _wearable_identified_customer_count_request(question: str) -> dict[str, Any]:
    sql = """
    with eligible_profiles as (
      select
        p.id as user_id,
        p.onboardstatus,
        p.reward_rate
      from public.profiles p
      where coalesce(p.is_deleted, false) = false
        and (
          coalesce(p."isCardIssued", false) = true
          or exists (select 1 from public.cards c where c.user_id = p.id)
        )
    ), daily_health_users as (
      select
        ep.user_id,
        max(hdd.date) as latest_health_data_date,
        max(hdd.updated_at) as latest_health_updated_at
      from eligible_profiles ep
      join public.health_data_daily hdd on hdd.user_id = ep.user_id
      where coalesce(hdd.deleted, false) = false
      group by 1
    ), recent_health_users_30d as (
      select distinct hdd.user_id
      from public.health_data_daily hdd
      join eligible_profiles ep on ep.user_id = hdd.user_id
      where coalesce(hdd.deleted, false) = false
        and hdd.date >= ((now() at time zone 'Asia/Kolkata')::date - interval '29 days')::date
        and hdd.date < ((now() at time zone 'Asia/Kolkata')::date + interval '1 day')::date
    ), apple_manufacturer_values as (
      select
        ep.user_id,
        ep.reward_rate,
        dhu.latest_health_data_date,
        dhu.latest_health_updated_at,
        m.value as manufacturer_value
      from eligible_profiles ep
      left join daily_health_users dhu on dhu.user_id = ep.user_id
      join lateral jsonb_array_elements_text(
        case
          when jsonb_typeof(
            ep.onboardstatus #> '{data,metadata,healthDeviceManufacturer}'
          ) = 'array'
          then ep.onboardstatus #> '{data,metadata,healthDeviceManufacturer}'
          else '[]'::jsonb
        end
      ) m(value) on true
    ), apple_flags as (
      select
        user_id,
        bool_or(
          manufacturer_value ~* '(^|[|])Watch[0-9]+,[0-9]+([|]|$)'
        ) as has_apple_watch,
        bool_or(manufacturer_value ilike '%whoop%') as has_whoop,
        bool_or(manufacturer_value ilike '%garmin%') as has_garmin,
        bool_or(manufacturer_value ilike '%fitbit%') as has_fitbit,
        bool_or(manufacturer_value ilike '%oura%') as has_oura,
        bool_or(manufacturer_value ilike '%coros%') as has_coros,
        bool_or(manufacturer_value ilike '%ultrahuman%') as has_ultrahuman,
        bool_or(
          manufacturer_value ~* '(^|[|])iPhone[0-9,]*([|]|$)'
          or manufacturer_value ilike '%iphone%'
          or manufacturer_value ilike '%com.apple.health%'
          or manufacturer_value ilike '%healthkit%'
        ) as has_iphone_health,
        max(latest_health_updated_at) as latest_synced_at
      from apple_manufacturer_values
      group by 1
    ), apple_source_evidence as (
      select user_id, 'APPLE_HEALTH'::text as integration_path,
        'APPLE_WATCH'::text as wearable_brand,
        'Apple Watch users'::text as brand_label,
        'APPLE_WATCH_VIA_APPLE_HEALTH'::text as source_label,
        true as counts_as_wearable,
        latest_synced_at,
        null::timestamp with time zone as latest_provider_updated_at
      from apple_flags
      where coalesce(has_apple_watch, false)
      union all
      select user_id, 'APPLE_HEALTH', 'WHOOP', 'Whoop users',
        'WHOOP_VIA_APPLE_HEALTH', true, latest_synced_at, null::timestamp with time zone
      from apple_flags
      where coalesce(has_whoop, false)
      union all
      select user_id, 'APPLE_HEALTH', 'GARMIN', 'Garmin users',
        'GARMIN_VIA_APPLE_HEALTH', true, latest_synced_at, null::timestamp with time zone
      from apple_flags
      where coalesce(has_garmin, false)
      union all
      select user_id, 'APPLE_HEALTH', 'FITBIT', 'Fitbit users',
        'FITBIT_VIA_APPLE_HEALTH', true, latest_synced_at, null::timestamp with time zone
      from apple_flags
      where coalesce(has_fitbit, false)
      union all
      select user_id, 'APPLE_HEALTH', 'OURA', 'Oura users',
        'OURA_VIA_APPLE_HEALTH', true, latest_synced_at, null::timestamp with time zone
      from apple_flags
      where coalesce(has_oura, false)
      union all
      select user_id, 'APPLE_HEALTH', 'COROS', 'Coros users',
        'COROS_VIA_APPLE_HEALTH', true, latest_synced_at, null::timestamp with time zone
      from apple_flags
      where coalesce(has_coros, false)
      union all
      select user_id, 'APPLE_HEALTH', 'ULTRAHUMAN', 'Ultrahuman users',
        'ULTRAHUMAN_VIA_APPLE_HEALTH', true, latest_synced_at, null::timestamp with time zone
      from apple_flags
      where coalesce(has_ultrahuman, false)
    ), active_providers as (
      select
        hdp.user_id,
        hdp.provider,
        hdp.provider_user_id,
        upper(coalesce(nullif(hdp.provider_metadata->>'provider', ''), hdp.provider)) as provider_label,
        case
          when hdp.provider = 'TERRA' then 'TERRA'
          else 'OTHER_HEALTH_SYNC'
        end as integration_path,
        hdp.updated_at as provider_updated_at
      from public.health_data_providers hdp
      join eligible_profiles ep on ep.user_id = hdp.user_id
      where hdp.provider = 'TERRA'
        and coalesce(hdp.active, false) = true
        and hdp.disconnected_at is null
    ), provider_health_rows as (
      select
        provider,
        provider_user_id,
        updated_at as synced_at
      from public.health_data_activity
      union all
      select
        provider,
        provider_user_id,
        updated_at as synced_at
      from public.health_data_body
      union all
      select
        provider,
        provider_user_id,
        updated_at as synced_at
      from public.health_data_daily_summary
      union all
      select
        provider,
        provider_user_id,
        updated_at as synced_at
      from public.health_data_sleep
    ), synced_providers as (
      select
        ap.user_id,
        ap.provider,
        ap.provider_user_id,
        ap.provider_label,
        ap.integration_path,
        ap.provider_updated_at,
        max(phr.synced_at) as latest_synced_at
      from active_providers ap
      join provider_health_rows phr
        on phr.provider = ap.provider
       and phr.provider_user_id = ap.provider_user_id
      group by 1, 2, 3, 4, 5, 6
    ), terra_source_evidence as (
      select
        user_id,
        'TERRA'::text as integration_path,
        case provider_label
          when 'COROS' then 'COROS'
          when 'FITBIT' then 'FITBIT'
          when 'GARMIN' then 'GARMIN'
          when 'HUAWEI' then 'HUAWEI_WATCH'
          when 'OURA' then 'OURA'
          when 'SAMSUNG' then 'SAMSUNG_WATCH'
          when 'ULTRAHUMAN' then 'ULTRAHUMAN'
          when 'WHOOP' then 'WHOOP'
        end as wearable_brand,
        case provider_label
          when 'COROS' then 'Coros users'
          when 'FITBIT' then 'Fitbit users'
          when 'GARMIN' then 'Garmin users'
          when 'HUAWEI' then 'Huawei Watch users'
          when 'OURA' then 'Oura users'
          when 'SAMSUNG' then 'Samsung Watch users'
          when 'ULTRAHUMAN' then 'Ultrahuman users'
          when 'WHOOP' then 'Whoop users'
        end as brand_label,
        case provider_label
          when 'COROS' then 'COROS_VIA_TERRA'
          when 'FITBIT' then 'FITBIT_VIA_TERRA'
          when 'GARMIN' then 'GARMIN_VIA_TERRA'
          when 'HUAWEI' then 'HUAWEI_WATCH_VIA_TERRA'
          when 'OURA' then 'OURA_VIA_TERRA'
          when 'SAMSUNG' then 'SAMSUNG_GALAXY_WATCH_VIA_TERRA'
          when 'ULTRAHUMAN' then 'ULTRAHUMAN_VIA_TERRA'
          when 'WHOOP' then 'WHOOP_VIA_TERRA'
        end as source_label,
        true as counts_as_wearable,
        latest_synced_at,
        provider_updated_at as latest_provider_updated_at
      from synced_providers
      where provider = 'TERRA'
        and provider_label in (
          'COROS', 'FITBIT', 'GARMIN', 'HUAWEI', 'OURA', 'SAMSUNG',
          'ULTRAHUMAN', 'WHOOP'
        )
    ), terra_device_health_rows as (
      select
        sp.user_id,
        sp.provider_label,
        sp.latest_synced_at,
        sp.provider_updated_at as latest_provider_updated_at,
        hda.data
      from synced_providers sp
      join public.health_data_activity hda
        on hda.provider = sp.provider
       and hda.provider_user_id = sp.provider_user_id
      where sp.provider = 'TERRA'
      union all
      select
        sp.user_id,
        sp.provider_label,
        sp.latest_synced_at,
        sp.provider_updated_at as latest_provider_updated_at,
        hdb.data
      from synced_providers sp
      join public.health_data_body hdb
        on hdb.provider = sp.provider
       and hdb.provider_user_id = sp.provider_user_id
      where sp.provider = 'TERRA'
      union all
      select
        sp.user_id,
        sp.provider_label,
        sp.latest_synced_at,
        sp.provider_updated_at as latest_provider_updated_at,
        hds.data
      from synced_providers sp
      join public.health_data_daily_summary hds
        on hds.provider = sp.provider
       and hds.provider_user_id = sp.provider_user_id
      where sp.provider = 'TERRA'
      union all
      select
        sp.user_id,
        sp.provider_label,
        sp.latest_synced_at,
        sp.provider_updated_at as latest_provider_updated_at,
        hsl.data
      from synced_providers sp
      join public.health_data_sleep hsl
        on hsl.provider = sp.provider
       and hsl.provider_user_id = sp.provider_user_id
      where sp.provider = 'TERRA'
    ), terra_device_names as (
      select
        user_id,
        provider_label,
        latest_synced_at,
        latest_provider_updated_at,
        nullif(btrim(data->'device_data'->>'name'), '') as device_name
      from terra_device_health_rows
      where data ? 'device_data'
      union all
      select
        tdhr.user_id,
        tdhr.provider_label,
        tdhr.latest_synced_at,
        tdhr.latest_provider_updated_at,
        nullif(btrim(other_device.value->>'name'), '') as device_name
      from terra_device_health_rows tdhr
      cross join lateral jsonb_array_elements(
        case
          when jsonb_typeof(tdhr.data->'device_data'->'other_devices') = 'array'
          then tdhr.data->'device_data'->'other_devices'
          else '[]'::jsonb
        end
      ) other_device(value)
      where tdhr.data ? 'device_data'
    ), terra_device_classified_sources as (
      select
        user_id,
        latest_synced_at,
        latest_provider_updated_at,
        case
          when lower(device_name) like '%fitbit%' then
            case when provider_label = 'GOOGLE'
              then 'FITBIT_VIA_GOOGLE_TERRA'
              else 'FITBIT_VIA_TERRA'
            end
          when lower(device_name) like '%whoop%' then
            case when provider_label = 'GOOGLE'
              then 'WHOOP_VIA_GOOGLE_TERRA'
              else 'WHOOP_VIA_TERRA'
            end
          when lower(device_name) like '%garmin%'
            or lower(device_name) like '%forerunner%'
            or lower(device_name) like '%fenix%'
            or lower(device_name) like '%venu%'
            or lower(device_name) like '%instinct%'
            or lower(device_name) like '%epix%' then 'GARMIN_VIA_TERRA'
          when lower(device_name) like '%coros%'
            or lower(device_name) like '%pace 2%'
            or lower(device_name) like '%pace 3%'
            or lower(device_name) like '%apex%'
            or lower(device_name) like '%vertix%' then 'COROS_VIA_TERRA'
          when lower(device_name) like '%noisefit%'
            or lower(device_name) like '%noise_activity%' then
            case when provider_label = 'GOOGLE'
              then 'NOISEFIT_VIA_GOOGLE_TERRA'
              else 'NOISEFIT_VIA_TERRA'
            end
          when lower(device_name) like '%huami%'
            or lower(device_name) like '%amazfit%' then
            case when provider_label = 'GOOGLE'
              then 'AMAZFIT_HUAMI_VIA_GOOGLE_TERRA'
              else 'AMAZFIT_HUAMI_VIA_TERRA'
            end
          when lower(device_name) like '%xiaomi.hm.health%'
            or lower(device_name) like '%xiaomi.wearable%'
            or lower(device_name) like '%mi band%'
            or lower(device_name) like '%miband%' then
            case when provider_label = 'GOOGLE'
              then 'XIAOMI_MI_FIT_OR_MI_BAND_VIA_GOOGLE_TERRA'
              else 'XIAOMI_MI_FIT_OR_MI_BAND_VIA_TERRA'
            end
          when lower(device_name) like '%titan.fastrack%'
            or lower(device_name) like '%isport.fastrack%'
            or lower(device_name) like '%titan.reflex%'
            or lower(device_name) like '%fastrack%' then
            case when provider_label = 'GOOGLE'
              then 'FASTRACK_TITAN_VIA_GOOGLE_TERRA'
              else 'FASTRACK_TITAN_VIA_TERRA'
            end
          when lower(device_name) like '%boat%'
            or lower(device_name) like '%coveiot%' then
            case when provider_label = 'GOOGLE'
              then 'BOAT_COVEIOT_VIA_GOOGLE_TERRA'
              else 'BOAT_COVEIOT_VIA_TERRA'
            end
          when lower(device_name) like '%fireboltt%'
            or lower(device_name) like '%fire_boltt%'
            or lower(device_name) like '%fire-boltt%' then
            case when provider_label = 'GOOGLE'
              then 'FIREBOLTT_VIA_GOOGLE_TERRA'
              else 'FIREBOLTT_VIA_TERRA'
            end
          when lower(device_name) like '%fossil%'
            or lower(device_name) like '%q explorist%' then
            case when provider_label = 'GOOGLE'
              then 'FOSSIL_VIA_GOOGLE_TERRA'
              else 'FOSSIL_VIA_TERRA'
            end
          when lower(device_name) like '%huawei.health%' then
            case when provider_label = 'GOOGLE'
              then 'HUAWEI_WATCH_VIA_GOOGLE_TERRA'
              else 'HUAWEI_WATCH_VIA_TERRA'
            end
          when lower(device_name) like '%crrepa%'
            or lower(device_name) like '%dafit%' then
            case when provider_label = 'GOOGLE'
              then 'DAFIT_CRREPA_VIA_GOOGLE_TERRA'
              else 'DAFIT_CRREPA_VIA_TERRA'
            end
          when lower(device_name) like '%nothing.smartcenter%' then
            case when provider_label = 'GOOGLE'
              then 'NOTHING_WATCH_VIA_GOOGLE_TERRA'
              else 'NOTHING_WATCH_VIA_TERRA'
            end
          when lower(device_name) like '%fit.cure.android.cswatch%' then
            case when provider_label = 'GOOGLE'
              then 'CULTSPORT_WATCH_VIA_GOOGLE_TERRA'
              else 'CULTSPORT_WATCH_VIA_TERRA'
            end
          when lower(device_name) like '%dsi.ant.plugins.antplus%'
            or lower(device_name) like '%antplus%'
            or lower(device_name) like '%ant.plugins%' then
            case when provider_label = 'GOOGLE'
              then 'ANTPLUS_SENSOR_VIA_GOOGLE_TERRA'
              else 'ANTPLUS_SENSOR_VIA_TERRA'
            end
          when lower(device_name) like '%sm-r%'
            or lower(device_name) like '%galaxy watch%' then
            case when provider_label = 'GOOGLE'
              then 'SAMSUNG_GALAXY_WATCH_VIA_GOOGLE_TERRA'
              else 'SAMSUNG_GALAXY_WATCH_VIA_TERRA'
            end
          when lower(device_name) like '%sec.android.app.shealth%'
            or lower(device_name) like '%shealth%' then
            case when provider_label = 'GOOGLE'
              then 'SAMSUNG_GALAXY_WATCH_VIA_GOOGLE_TERRA'
              else 'SAMSUNG_GALAXY_WATCH_VIA_TERRA'
            end
        end as source_label
      from terra_device_names
      where device_name is not null
    ), terra_device_source_evidence as (
      select
        user_id,
        'TERRA'::text as integration_path,
        case
          when source_label like 'FITBIT%' then 'FITBIT'
          when source_label like 'WHOOP%' then 'WHOOP'
          when source_label like 'GARMIN%' then 'GARMIN'
          when source_label like 'COROS%' then 'COROS'
          when source_label like 'HUAWEI%' then 'HUAWEI_WATCH'
          when source_label like 'SAMSUNG%' then 'SAMSUNG_WATCH'
          when source_label like 'NOTHING%' then 'NOTHING_WATCH'
          when source_label like 'CULTSPORT%' then 'CULTSPORT_WATCH'
          when source_label like 'ANTPLUS%' then 'ANTPLUS_SENSOR'
          else 'OTHER_WEARABLE'
        end as wearable_brand,
        case
          when source_label like 'FITBIT%' then 'Fitbit users'
          when source_label like 'WHOOP%' then 'Whoop users'
          when source_label like 'GARMIN%' then 'Garmin users'
          when source_label like 'COROS%' then 'Coros users'
          when source_label like 'HUAWEI%' then 'Huawei Watch users'
          when source_label like 'SAMSUNG%' then 'Samsung Watch users'
          when source_label like 'NOTHING%' then 'Nothing Watch users'
          when source_label like 'CULTSPORT%' then 'Cultsport Watch users'
          when source_label like 'ANTPLUS%' then 'ANT+ sensor'
          else 'Other wearables'
        end as brand_label,
        source_label,
        case when source_label like 'ANTPLUS%' then false else true end as counts_as_wearable,
        max(latest_synced_at) as latest_synced_at,
        max(latest_provider_updated_at) as latest_provider_updated_at
      from terra_device_classified_sources
      where source_label is not null
      group by 1, 2, 3, 4, 5, 6
    ), wearable_evidence as (
      select * from apple_source_evidence
      union all
      select * from terra_source_evidence
      union all
      select * from terra_device_source_evidence
    ), headline_wearable_evidence as (
      select *
      from wearable_evidence
      where counts_as_wearable = true
    ), multi_wearable_users as (
      select
        user_id,
        max(latest_synced_at) as latest_synced_at,
        max(latest_provider_updated_at) as latest_provider_updated_at
      from headline_wearable_evidence
      group by 1
      having count(distinct wearable_brand) >= 2
    ), iphone_health_only as (
      select
        af.user_id,
        max(af.latest_synced_at) as latest_synced_at
      from apple_flags af
      left join headline_wearable_evidence hwe on hwe.user_id = af.user_id
      where af.has_iphone_health = true
        and hwe.user_id is null
      group by 1
    ), google_fit_only as (
      select
        sp.user_id,
        max(sp.latest_synced_at) as latest_synced_at,
        max(sp.provider_updated_at) as latest_provider_updated_at
      from synced_providers sp
      left join headline_wearable_evidence hwe on hwe.user_id = sp.user_id
      where sp.provider_label = 'GOOGLE'
        and hwe.user_id is null
      group by 1
    ), health_data_source_unknown as (
      select
        dhu.user_id,
        dhu.latest_health_updated_at as latest_synced_at
      from daily_health_users dhu
      left join headline_wearable_evidence hwe on hwe.user_id = dhu.user_id
      left join iphone_health_only iho on iho.user_id = dhu.user_id
      left join google_fit_only gfo on gfo.user_id = dhu.user_id
      where hwe.user_id is null
        and iho.user_id is null
        and gfo.user_id is null
    ), source_counts as (
      select
        'wearable_brand'::text as classification,
        brand_label as provider_label,
        count(distinct user_id)::int as users,
        max(latest_synced_at)::text as latest_synced_at,
        max(latest_provider_updated_at)::text as latest_provider_updated_at
      from headline_wearable_evidence
      group by 1, 2
      union all
      select
        'wearable_evidence_source'::text as classification,
        source_label as provider_label,
        count(distinct user_id)::int as users,
        max(latest_synced_at)::text as latest_synced_at,
        max(latest_provider_updated_at)::text as latest_provider_updated_at
      from wearable_evidence
      group by 1, 2
    ), reward_rate_4_plus_segments as (
      select
        'reward_rate_4_plus_wearable_segment'::text as classification,
        brand_label as provider_label,
        count(distinct ep.user_id)::int as users,
        max(hwe.latest_synced_at)::text as latest_synced_at,
        max(hwe.latest_provider_updated_at)::text as latest_provider_updated_at
      from eligible_profiles ep
      join headline_wearable_evidence hwe on hwe.user_id = ep.user_id
      where coalesce(ep.reward_rate, 0) >= 4
      group by 1, 2
      union all
      select
        'reward_rate_4_plus_total'::text as classification,
        'REWARD_RATE_4_PLUS_WEARABLE_SEGMENT'::text as provider_label,
        count(distinct ep.user_id)::int as users,
        max(hwe.latest_synced_at)::text as latest_synced_at,
        max(hwe.latest_provider_updated_at)::text as latest_provider_updated_at
      from eligible_profiles ep
      join headline_wearable_evidence hwe on hwe.user_id = ep.user_id
      where coalesce(ep.reward_rate, 0) >= 4
    ), totals as (
      select
        'health_sync_total'::text as classification,
        'TOTAL_HEALTH_SYNC_USERS'::text as provider_label,
        count(distinct user_id)::int as users,
        max(latest_health_updated_at)::text as latest_synced_at,
        null::text as latest_provider_updated_at
      from daily_health_users
      union all
      select
        'health_sync_total'::text as classification,
        'TOTAL_APPLE_HEALTH_SYNC_USERS'::text as provider_label,
        count(distinct af.user_id)::int as users,
        max(dhu.latest_health_updated_at)::text as latest_synced_at,
        null::text as latest_provider_updated_at
      from apple_flags af
      join daily_health_users dhu on dhu.user_id = af.user_id
      union all
      select
        'health_sync_total'::text as classification,
        'TOTAL_TERRA_SYNC_USERS'::text as provider_label,
        count(distinct user_id)::int as users,
        max(latest_synced_at)::text as latest_synced_at,
        max(provider_updated_at)::text as latest_provider_updated_at
      from synced_providers
      where provider = 'TERRA'
      union all
      select
        'wearable_identified_total'::text as classification,
        'TOTAL_WEARABLE_IDENTIFIED_USERS'::text as provider_label,
        count(distinct user_id)::int as users,
        max(latest_synced_at)::text as latest_synced_at,
        max(latest_provider_updated_at)::text as latest_provider_updated_at
      from headline_wearable_evidence
      union all
      select
        'wearable_sync_total'::text as classification,
        'TOTAL_WEARABLE_SYNC_USERS_30D'::text as provider_label,
        count(distinct hwe.user_id)::int as users,
        max(hwe.latest_synced_at)::text as latest_synced_at,
        max(hwe.latest_provider_updated_at)::text as latest_provider_updated_at
      from headline_wearable_evidence hwe
      join recent_health_users_30d rhu on rhu.user_id = hwe.user_id
      union all
      select
        'multi_wearable_total'::text as classification,
        'MULTI_WEARABLE_USERS'::text as provider_label,
        count(distinct user_id)::int as users,
        max(latest_synced_at)::text as latest_synced_at,
        max(latest_provider_updated_at)::text as latest_provider_updated_at
      from multi_wearable_users
      union all
      select
        'health_sync_without_wearable'::text as classification,
        'IPHONE_HEALTH_ONLY'::text as provider_label,
        count(distinct user_id)::int as users,
        max(latest_synced_at)::text as latest_synced_at,
        null::text as latest_provider_updated_at
      from iphone_health_only
      union all
      select
        'health_sync_without_wearable'::text as classification,
        'GOOGLE_FIT_ANDROID_HEALTH_ONLY'::text as provider_label,
        count(distinct user_id)::int as users,
        max(latest_synced_at)::text as latest_synced_at,
        max(latest_provider_updated_at)::text as latest_provider_updated_at
      from google_fit_only
      union all
      select
        'health_sync_without_wearable'::text as classification,
        'HEALTH_DATA_EXISTS_SOURCE_UNKNOWN'::text as provider_label,
        count(distinct user_id)::int as users,
        max(latest_synced_at)::text as latest_synced_at,
        null::text as latest_provider_updated_at
      from health_data_source_unknown
    ), unioned as (
      select * from totals
      union all
      select * from source_counts
      union all
      select * from reward_rate_4_plus_segments
    )
    select *
    from unioned
    order by
      case
        when provider_label = 'TOTAL_WEARABLE_IDENTIFIED_USERS' then 0
        when provider_label = 'TOTAL_HEALTH_SYNC_USERS' then 1
        when provider_label = 'TOTAL_APPLE_HEALTH_SYNC_USERS' then 2
        when provider_label = 'TOTAL_TERRA_SYNC_USERS' then 3
        when provider_label = 'TOTAL_WEARABLE_SYNC_USERS_30D' then 4
        when provider_label = 'MULTI_WEARABLE_USERS' then 5
        when classification = 'wearable_brand' then 6
        when provider_label = 'REWARD_RATE_4_PLUS_WEARABLE_SEGMENT' then 7
        else 6
      end,
      users desc,
      provider_label asc;
  """

    return {
        "question": question,
        "interpretedDefinition": (
            "wearable-identified users are distinct non-deleted card-issued "
            "users with credible wearable evidence. Apple Health wearable "
            "evidence comes from profiles.onboardstatus.data.metadata."
            "healthDeviceManufacturer without requiring an Apple Health provider "
            "row or health_data_daily row by default; Apple Watch users require "
            "exact WatchN,M hardware tokens. Terra wearable evidence comes from "
            "active synced TERRA provider labels plus curated Terra device_data "
            "source names/model evidence. Current reward-rate segmentation uses "
            "profiles.reward_rate; the historical reward_rate table is only for "
            "explicit historical week/trend questions."
        ),
        "metricIds": ["wearable_identified_users"],
        "sql": sql,
        "resultType": "breakdown",
        "sources": [
            "public.profiles",
            "public.cards",
            "profiles.onboardstatus",
            "public.health_data_daily",
            "public.health_data_providers",
            "public.health_data_activity",
            "public.health_data_body",
            "public.health_data_daily_summary",
            "public.health_data_sleep",
        ],
        "dateWindow": (
            "All-time wearable evidence for the card-issued user base; "
            "wearable sync uses health_data_daily.date for the rolling last 30 "
            "Asia/Kolkata calendar days."
        ),
        "timezone": "Asia/Kolkata",
        "assumptions": (
            "Plain wearable questions default to the card-issued Elixir user "
            "base. Brand rollups are product-facing; source labels remain in "
            "the evidence rows for QA. Apple Health connection, iPhone Health "
            "only, Google Fit / Android health only, and source-unknown health "
            "data are not wearable users. Terra Samsung Health and Huawei "
            "Health are treated as watch evidence per accepted business "
            "assumption."
        ),
        "caveats": (
            "APPLE_HEALTH, generic GOOGLE, Google Fit, Health Connect, and "
            "generic TERRA are health-sync provider evidence, not wearable "
            "hardware proof by themselves. iPhone Health only and Google Fit / "
            "Android health only are attributed non-wearable health buckets; "
            "health data exists/source unknown is a gap bucket. Apple Watch is "
            "strictly exact WatchN,M token evidence for now; `apple watch` and "
            "`watchos` soft tokens are QA-only."
        ),
        "freshness": (
            "Wearable identification is all-time evidence. Wearable sync "
            "freshness uses health_data_daily.date for the rolling 30-day sync "
            "bucket; Terra source freshness also includes provider-backed row "
            "updated_at/provider updated_at where available."
        ),
        "resultSummary": (
            "Returns card-issued wearable-identified totals, health-sync and "
            "non-wearable health-only buckets, product-facing brand rollups, "
            "source evidence rows, multi-wearable users by distinct brand, "
            "30-day wearable sync users, and current reward-rate >= 4% wearable "
            "segments."
        ),
        "friction": "none",
        "conventionAdded": (
            "Wearable identification is an audience/device segmentation layer "
            "over non-deleted card-issued users. Apple Health evidence comes "
            "from onboardstatus manufacturer metadata; Terra evidence comes "
            "from provider metadata and curated device source names. Sync/"
            "currentness and reward-rate segmentation remain separate filters."
        ),
        "repeatPromoteCandidate": True,
    }


def _profile_answer_question_shortcut(
    args: dict[str, Any],
    *,
    mode: str,
) -> tuple[list[str], str, str, dict[str, Any]] | None:
    from .card_shortcut_requests import (
        _card_gtv_completed_days_request,
        _card_gtv_daily_request,
        _card_gtv_period_request,
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
    elif _matches_wearable_identified_customer_count(question):
        shortcut = "wearable_identified_users"
        request = _wearable_identified_customer_count_request(question)
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
    elif period_key := _card_transaction_count_period_key(question):
        shortcut = f"card_transaction_count_{period_key}"
        request = _card_transaction_count_period_request(
            question,
            period_key=period_key,
        )
    elif _matches_card_transaction_count_7d(question):
        shortcut = "card_transaction_count_7d"
        request = _card_transaction_count_7d_request(question)
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
