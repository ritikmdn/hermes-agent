"""Commerce shortcut classifiers and request builders for Chandler analytics."""

from __future__ import annotations

import re
from typing import Any

from .shortcut_requests import (
    _classified_transactions_cte_sql,
    _escape_sql_like,
    _escape_sql_literal,
    _india_completed_months_window,
    _india_last_days_window,
    _india_week_to_date_window,
    _normalize_question,
    _relative_period_assumption,
    _relative_period_label,
    _relative_period_window,
)


MERCHANT_QUERY_STOPWORDS = {
    "active",
    "a",
    "an",
    "app",
    "at",
    "biggest",
    "by",
    "card",
    "cards",
    "day",
    "days",
    "delivery",
    "food",
    "gtv",
    "did",
    "do",
    "does",
    "highest",
    "how",
    "in",
    "last",
    "merchant",
    "merchants",
    "month",
    "much",
    "on",
    "spend",
    "spender",
    "spenders",
    "spending",
    "spent",
    "this",
    "top",
    "transaction",
    "transactions",
    "txn",
    "txns",
    "user",
    "users",
    "was",
    "were",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "week",
}


def _clean_merchant_candidate(merchant: str) -> str | None:
    merchant = re.sub(r"\s+", " ", merchant).strip(" .,!?'\"")
    if not merchant:
        return None
    tokens = merchant.split()
    if any(token in MERCHANT_QUERY_STOPWORDS for token in tokens):
        return None
    if all(token.isdigit() for token in tokens):
        return None
    if len(merchant) < 2:
        return None
    return merchant


def _extract_simple_merchant_query(question: str) -> str | None:
    normalized = _normalize_question(question)
    for match in re.finditer(
        r"\b(?:on|at|from)\s+"
        r"(?P<merchant>[a-z0-9][a-z0-9&.'-]*(?:\s+[a-z0-9][a-z0-9&.'-]*){0,2}?)"
        r"(?=\s+(?:this|last|over|in|for|during|between|today|yesterday|"
        r"week|month|day|days?|7|10|30)\b|[?.!,]|$)",
        normalized,
    ):
        merchant = _clean_merchant_candidate(match.group("merchant"))
        if merchant:
            return merchant
    return None


def _extract_leading_merchant_query(question: str) -> str | None:
    normalized = _normalize_question(question)
    match = re.search(
        r"^\s*(?P<merchant>[a-z0-9][a-z0-9&.'-]*(?:\s+[a-z0-9][a-z0-9&.'-]*){0,2}?)"
        r"\s+(?:card\s+)?(?:gtv|spend|spends|spent|spending)\b",
        normalized,
    )
    if not match:
        return None
    return _clean_merchant_candidate(match.group("merchant"))


def _extract_merchant_query(question: str) -> str | None:
    return _extract_simple_merchant_query(question) or _extract_leading_merchant_query(
        question
    )


def _merchant_display_name(merchant_query: str) -> str:
    merchant_query = re.sub(r"\s+", " ", str(merchant_query or "")).strip()
    if not merchant_query:
        return "Merchant"
    if merchant_query.isupper():
        return merchant_query
    return " ".join(part[:1].upper() + part[1:] for part in merchant_query.split())


def _merchant_match_filter_sql(merchant_query: str) -> str:
    safe_merchant = _escape_sql_like(merchant_query)
    return (
        "("
        f"coalesce(ct.merchant_name, '') ilike '%{safe_merchant}%' escape '\\' "
        "or "
        f"coalesce(ct.description, '') ilike '%{safe_merchant}%' escape '\\'"
        ")"
    )


def _top_merchants_card_spend_7d_request(question: str) -> dict[str, Any]:
    window = _india_last_days_window(7)
    sql = f"""
    with
    {_classified_transactions_cte_sql(window["from"], window["toExclusive"])}
    select
      coalesce(nullif(ct.merchant_name, ''), 'Unknown merchant') as merchant_name,
      count(*)::int as txn_count,
      count(distinct ct.user_id)::int as user_count,
      coalesce(sum(ct.debit_amount), 0)::float as gross_spend_inr,
      max(ct.business_transaction_timestamp)::text as latest_card_txn_at
    from classified_transactions ct
    left join profiles p on p.id = ct.user_id
    where ct.is_card_spend = true
      and ct.is_reward_reconciliation = false
      and ct.user_id is not null
      and coalesce(p.is_deleted, false) = false
    group by coalesce(nullif(ct.merchant_name, ''), 'Unknown merchant')
    order by gross_spend_inr desc, txn_count desc, merchant_name
  """

    return {
        "question": question,
        "interpretedDefinition": (
            "Top merchants by gross Elixir card spend (GTV) over the last 7 "
            "Asia/Kolkata days, including today-to-date."
        ),
        "metricIds": ["gtv"],
        "sql": sql,
        "resultType": "merchants",
        "sources": ["transactions", "marketplace_order", "profiles"],
        "dateWindow": f"{window['from']} to {window['toExclusive']}",
        "timezone": window["timezone"],
        "assumptions": (
            "Last 7 days includes today-to-date and the previous 6 India "
            "business days. Merchant comes from the transaction merchant_name "
            "field, with blanks labeled Unknown merchant."
        ),
        "caveats": (
            "Amount is gross successful card spend; wallet loads, reward "
            "reconciliation rows, deleted users, and non-card spend are excluded. "
            "Refunds/reversals are not netted."
        ),
        "freshness": f"Live Supabase read at query runtime on {window['today']}.",
        "resultSummary": (
            "Returns merchants ranked by gross card spend with transaction count, "
            "unique spender count, and latest card transaction timestamp."
        ),
        "friction": "none",
        "conventionAdded": "none",
        "repeatPromoteCandidate": True,
    }


def _top_merchants_card_spend_period_request(
    question: str,
    *,
    period_key: str,
) -> dict[str, Any]:
    window = _relative_period_window(period_key)
    label = _relative_period_label(period_key)
    assumption = _relative_period_assumption(period_key)
    sql = f"""
    with
    {_classified_transactions_cte_sql(window["from"], window["toExclusive"])}
    select
      coalesce(nullif(ct.merchant_name, ''), 'Unknown merchant') as merchant_name,
      count(*)::int as txn_count,
      count(distinct ct.user_id)::int as user_count,
      coalesce(sum(ct.debit_amount), 0)::float as gross_spend_inr,
      max(ct.business_transaction_timestamp)::text as latest_card_txn_at
    from classified_transactions ct
    left join profiles p on p.id = ct.user_id
    where ct.is_card_spend = true
      and ct.is_reward_reconciliation = false
      and ct.user_id is not null
      and coalesce(p.is_deleted, false) = false
    group by coalesce(nullif(ct.merchant_name, ''), 'Unknown merchant')
    order by gross_spend_inr desc, txn_count desc, merchant_name
  """

    return {
        "question": question,
        "interpretedDefinition": (
            f"Top merchants by gross Elixir card spend (GTV) for {label} "
            "in Asia/Kolkata."
        ),
        "metricIds": ["gtv"],
        "sql": sql,
        "resultType": "merchants",
        "sources": ["transactions", "marketplace_order", "profiles"],
        "dateWindow": f"{window['from']} to {window['toExclusive']}",
        "timezone": window["timezone"],
        "assumptions": (
            f"{assumption} Merchant comes from the transaction merchant_name "
            "field, with blanks labeled Unknown merchant."
        ),
        "caveats": (
            "Amount is gross successful card spend; wallet loads, reward "
            "reconciliation rows, deleted users, and non-card spend are excluded. "
            "Refunds/reversals are not netted."
        ),
        "freshness": f"Live Supabase read at query runtime on {window['today']}.",
        "resultSummary": (
            "Returns merchants ranked by gross card spend with transaction count, "
            "unique spender count, and latest card transaction timestamp."
        ),
        "friction": "none",
        "conventionAdded": "none",
        "repeatPromoteCandidate": True,
    }


def _merchant_card_spend_7d_request(
    question: str,
    *,
    merchant_query: str,
) -> dict[str, Any]:
    window = _india_last_days_window(7)
    merchant_filter = _merchant_match_filter_sql(merchant_query)
    safe_merchant = _escape_sql_literal(merchant_query)
    sql = f"""
    with
    {_classified_transactions_cte_sql(window["from"], window["toExclusive"])}
    select
      '{safe_merchant}'::text as merchant_query,
      coalesce(sum(ct.debit_amount), 0)::float as gtv,
      count(*)::int as transactions,
      count(distinct ct.user_id)::int as users,
      max(coalesce(
        ct.updated_at,
        ct.created_at at time zone 'UTC',
        ct.transaction_timestamp at time zone 'UTC'
      ))::text as source_freshness
    from classified_transactions ct
    left join profiles p on p.id = ct.user_id
    where ct.is_card_spend = true
      and ct.is_reward_reconciliation = false
      and ct.user_id is not null
      and coalesce(p.is_deleted, false) = false
      and {merchant_filter}
  """

    return {
        "question": question,
        "interpretedDefinition": (
            f"Gross Elixir card spend (GTV) matching {_merchant_display_name(merchant_query)} "
            "over the last 7 Asia/Kolkata days, including today-to-date."
        ),
        "metricIds": ["gtv"],
        "merchantQuery": merchant_query,
        "sql": sql,
        "resultType": "kpi",
        "sources": ["transactions", "marketplace_order", "profiles"],
        "dateWindow": f"{window['from']} to {window['toExclusive']}",
        "timezone": window["timezone"],
        "assumptions": (
            "Last 7 days includes today-to-date and the previous 6 India "
            f"business days. Merchant is matched when merchant_name or "
            f"description contains `{merchant_query}`."
        ),
        "caveats": (
            "Merchant text matching may miss alternate descriptors. Amount is "
            "gross successful card spend; wallet loads, reward reconciliation "
            "rows, deleted users, and non-card spend are excluded."
        ),
        "freshness": f"Live Supabase read at query runtime on {window['today']}.",
        "resultSummary": (
            "Returns matched merchant GTV, successful card transaction count, "
            "unique spender count, and source freshness."
        ),
        "friction": "none",
        "conventionAdded": "none",
        "repeatPromoteCandidate": True,
    }


def _merchant_card_spend_period_request(
    question: str,
    *,
    merchant_query: str,
    period_key: str,
) -> dict[str, Any]:
    window = _relative_period_window(period_key)
    label = _relative_period_label(period_key)
    assumption = _relative_period_assumption(period_key)
    merchant_filter = _merchant_match_filter_sql(merchant_query)
    safe_merchant = _escape_sql_literal(merchant_query)
    sql = f"""
    with
    {_classified_transactions_cte_sql(window["from"], window["toExclusive"])}
    select
      '{safe_merchant}'::text as merchant_query,
      coalesce(sum(ct.debit_amount), 0)::float as gtv,
      count(*)::int as transactions,
      count(distinct ct.user_id)::int as users,
      max(coalesce(
        ct.updated_at,
        ct.created_at at time zone 'UTC',
        ct.transaction_timestamp at time zone 'UTC'
      ))::text as source_freshness
    from classified_transactions ct
    left join profiles p on p.id = ct.user_id
    where ct.is_card_spend = true
      and ct.is_reward_reconciliation = false
      and ct.user_id is not null
      and coalesce(p.is_deleted, false) = false
      and {merchant_filter}
  """

    return {
        "question": question,
        "interpretedDefinition": (
            f"Gross Elixir card spend (GTV) matching {_merchant_display_name(merchant_query)} "
            f"for {label} in Asia/Kolkata."
        ),
        "metricIds": ["gtv"],
        "merchantQuery": merchant_query,
        "sql": sql,
        "resultType": "kpi",
        "sources": ["transactions", "marketplace_order", "profiles"],
        "dateWindow": f"{window['from']} to {window['toExclusive']}",
        "timezone": window["timezone"],
        "assumptions": (
            f"{assumption} Merchant is matched when merchant_name or "
            f"description contains `{merchant_query}`."
        ),
        "caveats": (
            "Merchant text matching may miss alternate descriptors. Amount is "
            "gross successful card spend; wallet loads, reward reconciliation "
            "rows, deleted users, and non-card spend are excluded."
        ),
        "freshness": f"Live Supabase read at query runtime on {window['today']}.",
        "resultSummary": (
            "Returns matched merchant GTV, successful card transaction count, "
            f"unique spender count, and source freshness for {label}."
        ),
        "friction": "none",
        "conventionAdded": "none",
        "repeatPromoteCandidate": True,
    }


def _merchant_users_this_week_request(
    question: str,
    *,
    merchant_query: str,
) -> dict[str, Any]:
    window = _india_week_to_date_window()
    merchant_filter = _merchant_match_filter_sql(merchant_query)
    safe_merchant = _escape_sql_literal(merchant_query)
    sql = f"""
    with
    {_classified_transactions_cte_sql(window["from"], window["toExclusive"])}
    select
      '{safe_merchant}'::text as merchant_query,
      concat_ws(' ', p.first_name, p.middle_name, p.last_name) as user_name,
      ct.user_id::text as user_id,
      count(*)::int as txn_count,
      coalesce(sum(ct.debit_amount), 0)::float as gross_spend_inr,
      max(ct.business_transaction_timestamp)::text as last_card_txn_at
    from classified_transactions ct
    left join profiles p on p.id = ct.user_id
    where ct.is_card_spend = true
      and ct.is_reward_reconciliation = false
      and ct.user_id is not null
      and coalesce(p.is_deleted, false) = false
      and {merchant_filter}
    group by ct.user_id, p.first_name, p.middle_name, p.last_name
    order by gross_spend_inr desc, txn_count desc, user_name
  """

    return {
        "question": question,
        "interpretedDefinition": (
            f"Users with gross Elixir card spend matching {_merchant_display_name(merchant_query)} "
            "during the current Asia/Kolkata week-to-date window."
        ),
        "metricIds": ["gtv"],
        "merchantQuery": merchant_query,
        "sql": sql,
        "resultType": "users",
        "sources": ["transactions", "marketplace_order", "profiles"],
        "dateWindow": f"{window['from']} to {window['toExclusive']}",
        "timezone": window["timezone"],
        "assumptions": (
            "This week means Asia/Kolkata business week-to-date, including "
            f"today-to-date. Merchant is matched when merchant_name or "
            f"description contains `{merchant_query}`."
        ),
        "caveats": (
            "Merchant text matching may miss alternate descriptors. Amount is "
            "gross successful card spend; wallet loads, reward reconciliation "
            "rows, deleted users, and non-card spend are excluded."
        ),
        "freshness": f"Live Supabase read at query runtime on {window['today']}.",
        "resultSummary": (
            "Returns matched merchant spenders ranked by gross card spend with "
            "transaction count and latest card transaction timestamp."
        ),
        "friction": "none",
        "conventionAdded": "none",
        "repeatPromoteCandidate": True,
    }


def _merchant_users_period_request(
    question: str,
    *,
    merchant_query: str,
    period_key: str,
) -> dict[str, Any]:
    window = _relative_period_window(period_key)
    label = _relative_period_label(period_key)
    assumption = _relative_period_assumption(period_key)
    merchant_filter = _merchant_match_filter_sql(merchant_query)
    safe_merchant = _escape_sql_literal(merchant_query)
    sql = f"""
    with
    {_classified_transactions_cte_sql(window["from"], window["toExclusive"])}
    select
      '{safe_merchant}'::text as merchant_query,
      concat_ws(' ', p.first_name, p.middle_name, p.last_name) as user_name,
      ct.user_id::text as user_id,
      count(*)::int as txn_count,
      coalesce(sum(ct.debit_amount), 0)::float as gross_spend_inr,
      max(ct.business_transaction_timestamp)::text as last_card_txn_at
    from classified_transactions ct
    left join profiles p on p.id = ct.user_id
    where ct.is_card_spend = true
      and ct.is_reward_reconciliation = false
      and ct.user_id is not null
      and coalesce(p.is_deleted, false) = false
      and {merchant_filter}
    group by ct.user_id, p.first_name, p.middle_name, p.last_name
    order by gross_spend_inr desc, txn_count desc, user_name
  """

    return {
        "question": question,
        "interpretedDefinition": (
            f"Users with gross Elixir card spend matching {_merchant_display_name(merchant_query)} "
            f"for {label} in Asia/Kolkata."
        ),
        "metricIds": ["gtv"],
        "merchantQuery": merchant_query,
        "sql": sql,
        "resultType": "users",
        "sources": ["transactions", "marketplace_order", "profiles"],
        "dateWindow": f"{window['from']} to {window['toExclusive']}",
        "timezone": window["timezone"],
        "assumptions": (
            f"{assumption} Merchant is matched when merchant_name or "
            f"description contains `{merchant_query}`."
        ),
        "caveats": (
            "Merchant text matching may miss alternate descriptors. Amount is "
            "gross successful card spend; wallet loads, reward reconciliation "
            "rows, deleted users, and non-card spend are excluded."
        ),
        "freshness": f"Live Supabase read at query runtime on {window['today']}.",
        "resultSummary": (
            "Returns matched merchant spenders ranked by gross card spend with "
            f"transaction count and latest card transaction timestamp for {label}."
        ),
        "friction": "none",
        "conventionAdded": "none",
        "repeatPromoteCandidate": True,
    }


def _swiggy_spend_trend_10d_request(question: str) -> dict[str, Any]:
    window = _india_last_days_window(10)
    sql = f"""
    with
    {_classified_transactions_cte_sql(window["from"], window["toExclusive"])},
    dates as (
      select generate_series(
        '{_escape_sql_literal(window["from"])}'::date,
        ('{_escape_sql_literal(window["toExclusive"])}'::date - interval '1 day')::date,
        interval '1 day'
      )::date as business_date
    ),
    daily as (
      select
        ct.business_transaction_timestamp::date as business_date,
        coalesce(sum(ct.debit_amount), 0)::float as swiggy_gtv_inr,
        count(*)::int as txn_count,
        count(distinct ct.user_id)::int as user_count
      from classified_transactions ct
      left join profiles p on p.id = ct.user_id
      where ct.is_card_spend = true
        and ct.is_reward_reconciliation = false
        and ct.user_id is not null
        and coalesce(p.is_deleted, false) = false
        and (
          coalesce(ct.merchant_name, '') ilike '%swiggy%'
          or coalesce(ct.description, '') ilike '%swiggy%'
        )
      group by ct.business_transaction_timestamp::date
    )
    select
      d.business_date::text as business_date,
      coalesce(daily.swiggy_gtv_inr, 0)::float as swiggy_gtv_inr,
      coalesce(daily.txn_count, 0)::int as txn_count,
      coalesce(daily.user_count, 0)::int as user_count
    from dates d
    left join daily on daily.business_date = d.business_date
    order by d.business_date
  """

    return {
        "question": question,
        "interpretedDefinition": (
            "Daily gross Elixir card spend at merchants matching Swiggy over "
            "the last 10 Asia/Kolkata days, including today-to-date."
        ),
        "metricIds": ["gtv"],
        "sql": sql,
        "resultType": "trend",
        "sources": ["transactions", "marketplace_order", "profiles"],
        "dateWindow": f"{window['from']} to {window['toExclusive']}",
        "timezone": window["timezone"],
        "assumptions": (
            "Last 10 days includes today-to-date and the previous 9 India "
            "business days. Swiggy spend is matched on merchant name or "
            "transaction description containing 'swiggy'."
        ),
        "caveats": (
            "Merchant text matching may miss alternate descriptors. Amount is "
            "gross successful card spend; wallet loads, reward reconciliation "
            "rows, deleted users, and non-card spend are excluded."
        ),
        "freshness": f"Live Supabase read at query runtime on {window['today']}.",
        "resultSummary": (
            "Returns daily Swiggy GTV, transaction count, and unique spender count."
        ),
        "friction": "none",
        "conventionAdded": "none",
        "repeatPromoteCandidate": True,
    }


def _gym_milestone_avg_monthly_spend_3mo_request(question: str) -> dict[str, Any]:
    window = _india_completed_months_window(3)
    sql = f"""
    with
    gym_users as (
      select distinct mpi.user_id
      from milestone_program_instances mpi
      join customer_vouchers cv on cv.id = mpi.program_id
      join profiles p on p.id = mpi.user_id
      where mpi.status = 'active'
        and coalesce(p.is_deleted, false) = false
        and mpi.user_id is not null
    ),
    months as (
      select generate_series(
        '{_escape_sql_literal(window["from"])}'::date,
        '{_escape_sql_literal(window["lastMonth"])}'::date,
        interval '1 month'
      )::date as month_start
    ),
    {_classified_transactions_cte_sql(window["from"], window["toExclusive"])},
    monthly as (
      select
        date_trunc('month', ct.business_transaction_timestamp)::date as month_start,
        coalesce(sum(ct.debit_amount), 0)::float as gtv,
        count(*)::int as transactions,
        count(distinct ct.user_id)::int as spending_users,
        max(coalesce(
          ct.updated_at,
          ct.created_at at time zone 'UTC',
          ct.transaction_timestamp at time zone 'UTC'
        ))::text as source_freshness
      from classified_transactions ct
      join gym_users gu on gu.user_id = ct.user_id
      where ct.is_card_spend = true
        and ct.is_reward_reconciliation = false
      group by 1
    ),
    cohort_size as (
      select count(*)::int as gym_users from gym_users
    ),
    monthly_with_zeroes as (
      select
        m.month_start,
        cs.gym_users,
        coalesce(mon.gtv, 0)::float as gtv,
        coalesce(mon.transactions, 0)::int as transactions,
        coalesce(mon.spending_users, 0)::int as spending_users,
        case
          when cs.gym_users > 0 then (coalesce(mon.gtv, 0) / cs.gym_users)::float
          else null
        end as avg_spend_per_gym_user,
        case
          when coalesce(mon.spending_users, 0) > 0
            then (coalesce(mon.gtv, 0) / mon.spending_users)::float
          else null
        end as avg_spend_per_spending_user,
        mon.source_freshness
      from months m
      cross join cohort_size cs
      left join monthly mon on mon.month_start = m.month_start
    )
    select
      'overall_3mo_avg'::text as period,
      null::date as month_start,
      max(gym_users)::int as gym_users,
      sum(gtv)::float as total_gtv,
      sum(transactions)::int as transactions,
      null::int as spending_users,
      avg(avg_spend_per_gym_user)::float as avg_monthly_spend_per_gym_user,
      avg(avg_spend_per_spending_user)::float as avg_monthly_spend_per_spending_user,
      max(source_freshness)::text as source_freshness
    from monthly_with_zeroes
    union all
    select
      to_char(month_start, 'YYYY-MM')::text as period,
      month_start,
      gym_users,
      gtv as total_gtv,
      transactions,
      spending_users,
      avg_spend_per_gym_user as avg_monthly_spend_per_gym_user,
      avg_spend_per_spending_user as avg_monthly_spend_per_spending_user,
      source_freshness
    from monthly_with_zeroes
    order by month_start nulls first
  """

    return {
        "question": question,
        "interpretedDefinition": (
            "Gym milestone users are current active milestone-program users; "
            "spend is gross successful card spend / GTV."
        ),
        "metricIds": ["gym_milestone_users", "gtv"],
        "sql": sql,
        "resultType": "timeseries",
        "sources": [
            "milestone_program_instances",
            "customer_vouchers",
            "profiles",
            "transactions",
            "marketplace_order",
        ],
        "dateWindow": f"{window['from']} to {window['toExclusive']}",
        "timezone": window["timezone"],
        "assumptions": (
            "Gym milestone users are current active milestone users with "
            "non-deleted profiles. Monthly spend is gross successful card "
            "spend by that cohort, averaged per completed calendar month. "
            "The window is the last 3 completed calendar months before the "
            "current partial month."
        ),
        "caveats": (
            "Cohort membership is current active status, not historical "
            "membership as-of each month. Users with zero card spend in a "
            "month are included in avg_spend_per_gym_user; "
            "avg_spend_per_spending_user excludes zero-spend users."
        ),
        "freshness": f"Live Supabase read at query runtime on {window['today']}.",
        "resultSummary": (
            "Returns the 3-month average monthly card spend per active gym "
            "milestone user, plus monthly GTV, transaction count, spending "
            "users, and source freshness."
        ),
        "friction": "none",
        "conventionAdded": "none",
        "repeatPromoteCandidate": True,
    }
