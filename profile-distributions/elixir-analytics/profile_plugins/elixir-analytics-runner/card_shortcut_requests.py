"""Card shortcut request builders for Chandler analytics."""

from __future__ import annotations

from typing import Any

from .shortcut_requests import (
    _classified_transactions_cte_sql,
    _escape_sql_literal,
    _india_completed_days_window,
    _india_last_days_window,
    _relative_period_assumption,
    _relative_period_label,
    _relative_period_window,
)


def _card_gtv_7d_request(question: str) -> dict[str, Any]:
    window = _india_completed_days_window(7)
    sql = f"""
    with
    {_classified_transactions_cte_sql(window["from"], window["toExclusive"])}
    select
      coalesce(sum(ct.debit_amount), 0)::float as gtv,
      count(*)::int as transactions,
      count(distinct ct.user_id) filter (
        where ct.user_id is not null and coalesce(p.is_deleted, false) = false
      )::int as users,
      max(coalesce(
        ct.updated_at,
        ct.created_at at time zone 'UTC',
        ct.transaction_timestamp at time zone 'UTC'
      ))::text as source_freshness
    from classified_transactions ct
    left join profiles p on p.id = ct.user_id
    where ct.is_card_spend = true
      and ct.is_reward_reconciliation = false
  """

    return {
        "question": question,
        "interpretedDefinition": (
            "Card GTV over the last 7 completed Asia/Kolkata business days, "
            "excluding today."
        ),
        "metricIds": ["gtv"],
        "sql": sql,
        "resultType": "kpi",
        "sources": ["transactions", "marketplace_order", "profiles"],
        "dateWindow": f"{window['from']} to {window['toExclusive']}",
        "timezone": window["timezone"],
        "assumptions": (
            "Last 7 days follows the completed dashboard window convention: "
            "the 7 India business days ending before today."
        ),
        "caveats": (
            "GTV is gross successful card spend only; wallet loads, refunds, "
            "and marketplace reward reconciliation rows are excluded."
        ),
        "freshness": f"Live Supabase read at query runtime on {window['today']}.",
        "resultSummary": (
            "Returns total GTV, successful card transaction count, card users, "
            "and source freshness for the completed 7-day window."
        ),
        "friction": "none",
        "conventionAdded": "none",
        "repeatPromoteCandidate": True,
    }


def _card_period_kpi_sql(window: dict[str, str]) -> str:
    return f"""
    with
    {_classified_transactions_cte_sql(window["from"], window["toExclusive"])}
    select
      coalesce(sum(ct.debit_amount), 0)::float as gtv,
      count(*)::int as transactions,
      count(distinct ct.user_id) filter (
        where ct.user_id is not null and coalesce(p.is_deleted, false) = false
      )::int as users,
      max(coalesce(
        ct.updated_at,
        ct.created_at at time zone 'UTC',
        ct.transaction_timestamp at time zone 'UTC'
      ))::text as source_freshness
    from classified_transactions ct
    left join profiles p on p.id = ct.user_id
    where ct.is_card_spend = true
      and ct.is_reward_reconciliation = false
  """


def _card_gtv_completed_days_request(question: str, *, days: int) -> dict[str, Any]:
    window = _india_completed_days_window(days)
    sql = _card_period_kpi_sql(window)

    return {
        "question": question,
        "interpretedDefinition": (
            f"Card GTV over the last {days} completed Asia/Kolkata days, "
            "excluding today."
        ),
        "metricIds": ["gtv"],
        "sql": sql,
        "resultType": "kpi",
        "sources": ["transactions", "marketplace_order", "profiles"],
        "dateWindow": f"{window['from']} to {window['toExclusive']}",
        "timezone": window["timezone"],
        "assumptions": (
            f"Last {days} days follows the completed dashboard window convention: "
            f"the {days} India days ending before today."
        ),
        "caveats": (
            "GTV is gross successful card spend only; wallet loads, refunds, "
            "and marketplace reward reconciliation rows are excluded."
        ),
        "freshness": f"Live Supabase read at query runtime on {window['today']}.",
        "resultSummary": (
            "Returns total GTV, successful card transaction count, card users, "
            f"and source freshness for the completed {days}-day window."
        ),
        "friction": "none",
        "conventionAdded": "none",
        "repeatPromoteCandidate": True,
    }


def _card_gtv_period_request(question: str, *, period_key: str) -> dict[str, Any]:
    window = _relative_period_window(period_key)
    label = _relative_period_label(period_key)
    assumption = _relative_period_assumption(period_key)
    sql = _card_period_kpi_sql(window)

    return {
        "question": question,
        "interpretedDefinition": (
            f"Card GTV for {label} in Asia/Kolkata, using gross successful "
            "card spend."
        ),
        "metricIds": ["gtv"],
        "sql": sql,
        "resultType": "kpi",
        "sources": ["transactions", "marketplace_order", "profiles"],
        "dateWindow": f"{window['from']} to {window['toExclusive']}",
        "timezone": window["timezone"],
        "assumptions": assumption,
        "caveats": (
            "GTV is gross successful card spend only; wallet loads, refunds, "
            "and marketplace reward reconciliation rows are excluded."
        ),
        "freshness": f"Live Supabase read at query runtime on {window['today']}.",
        "resultSummary": (
            "Returns total GTV, successful card transaction count, card users, "
            f"and source freshness for {label}."
        ),
        "friction": "none",
        "conventionAdded": "none",
        "repeatPromoteCandidate": True,
    }


def _card_transaction_count_period_request(
    question: str,
    *,
    period_key: str,
) -> dict[str, Any]:
    window = _relative_period_window(period_key)
    label = _relative_period_label(period_key)
    assumption = _relative_period_assumption(period_key)
    sql = _card_period_kpi_sql(window)

    return {
        "question": question,
        "interpretedDefinition": (
            f"Successful card transaction count for {label} in Asia/Kolkata."
        ),
        "metricIds": ["card_transactions", "gtv"],
        "sql": sql,
        "resultType": "kpi",
        "sources": ["transactions", "marketplace_order", "profiles"],
        "dateWindow": f"{window['from']} to {window['toExclusive']}",
        "timezone": window["timezone"],
        "assumptions": assumption,
        "caveats": (
            "Transaction count includes successful card spend rows only; wallet "
            "loads, refunds, and marketplace reward reconciliation rows are excluded."
        ),
        "freshness": f"Live Supabase read at query runtime on {window['today']}.",
        "resultSummary": (
            "Returns successful card transaction count, gross card GTV, card "
            f"users, and source freshness for {label}."
        ),
        "friction": "none",
        "conventionAdded": "none",
        "repeatPromoteCandidate": True,
    }


def _card_gtv_daily_request(question: str, *, days: int) -> dict[str, Any]:
    window = _india_last_days_window(days)
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
        coalesce(sum(ct.debit_amount), 0)::float as gtv,
        count(*)::int as transactions,
        count(distinct ct.user_id) filter (
          where ct.user_id is not null and coalesce(p.is_deleted, false) = false
        )::int as users,
        max(coalesce(
          ct.updated_at,
          ct.created_at at time zone 'UTC',
          ct.transaction_timestamp at time zone 'UTC'
        ))::text as source_freshness
      from classified_transactions ct
      left join profiles p on p.id = ct.user_id
      where ct.is_card_spend = true
        and ct.is_reward_reconciliation = false
      group by 1
    )
    select
      d.business_date::text as business_date,
      coalesce(daily.gtv, 0)::float as gtv,
      coalesce(daily.transactions, 0)::int as transactions,
      coalesce(daily.users, 0)::int as users,
      daily.source_freshness
    from dates d
    left join daily on daily.business_date = d.business_date
    order by d.business_date
  """

    return {
        "question": question,
        "interpretedDefinition": (
            f"Daily Card GTV over the last {days} Asia/Kolkata days, "
            "including today-to-date."
        ),
        "metricIds": ["gtv"],
        "sql": sql,
        "resultType": "trend",
        "sources": ["transactions", "marketplace_order", "profiles"],
        "dateWindow": f"{window['from']} to {window['toExclusive']}",
        "timezone": window["timezone"],
        "assumptions": (
            f"Last {days} days includes the current India business day "
            "to date, so the latest row can be partial."
        ),
        "caveats": (
            "Daily GTV is gross successful card spend; wallet loads, refunds, "
            "and marketplace reward reconciliation rows are excluded."
        ),
        "freshness": f"Live Supabase read at query runtime on {window['today']}.",
        "resultSummary": (
            "Returns one row per India business date with GTV, successful card "
            "transaction count, card users, and source freshness."
        ),
        "friction": "none",
        "conventionAdded": "none",
        "repeatPromoteCandidate": True,
    }


def _card_transaction_count_7d_request(question: str) -> dict[str, Any]:
    window = _india_completed_days_window(7)
    sql = f"""
    with
    {_classified_transactions_cte_sql(window["from"], window["toExclusive"])}
    select
      count(*)::int as transactions,
      coalesce(sum(ct.debit_amount), 0)::float as gtv,
      count(distinct ct.user_id) filter (
        where ct.user_id is not null and coalesce(p.is_deleted, false) = false
      )::int as users,
      max(coalesce(
        ct.updated_at,
        ct.created_at at time zone 'UTC',
        ct.transaction_timestamp at time zone 'UTC'
      ))::text as source_freshness
    from classified_transactions ct
    left join profiles p on p.id = ct.user_id
    where ct.is_card_spend = true
      and ct.is_reward_reconciliation = false
  """

    return {
        "question": question,
        "interpretedDefinition": (
            "Successful card transaction count over the last 7 completed "
            "Asia/Kolkata business days, excluding today."
        ),
        "metricIds": ["card_transactions", "gtv"],
        "sql": sql,
        "resultType": "kpi",
        "sources": ["transactions", "marketplace_order", "profiles"],
        "dateWindow": f"{window['from']} to {window['toExclusive']}",
        "timezone": window["timezone"],
        "assumptions": (
            "Last 7 days follows the completed dashboard window convention: "
            "the 7 India business days ending before today."
        ),
        "caveats": (
            "Transaction count includes successful card spend rows only; wallet "
            "loads, refunds, and marketplace reward reconciliation rows are excluded."
        ),
        "freshness": f"Live Supabase read at query runtime on {window['today']}.",
        "resultSummary": (
            "Returns successful card transaction count, gross card GTV, card "
            "users, and source freshness for the completed 7-day window."
        ),
        "friction": "none",
        "conventionAdded": "none",
        "repeatPromoteCandidate": True,
    }


def _card_gtv_weekly_30d_request(question: str) -> dict[str, Any]:
    window = _india_completed_days_window(30)
    sql = f"""
    with
    {_classified_transactions_cte_sql(window["from"], window["toExclusive"])}
    select
      date_trunc('week', ct.business_transaction_timestamp)::date::text as week_start,
      coalesce(sum(ct.debit_amount), 0)::float as gtv,
      count(*)::int as transactions,
      count(distinct ct.user_id) filter (
        where ct.user_id is not null and coalesce(p.is_deleted, false) = false
      )::int as users,
      max(coalesce(
        ct.updated_at,
        ct.created_at at time zone 'UTC',
        ct.transaction_timestamp at time zone 'UTC'
      ))::text as source_freshness
    from classified_transactions ct
    left join profiles p on p.id = ct.user_id
    where ct.is_card_spend = true
      and ct.is_reward_reconciliation = false
    group by 1
    order by week_start
  """

    return {
        "question": question,
        "interpretedDefinition": (
            "Weekly Card GTV over the last 30 completed Asia/Kolkata business "
            "days, excluding today."
        ),
        "metricIds": ["gtv"],
        "sql": sql,
        "resultType": "trend",
        "sources": ["transactions", "marketplace_order", "profiles"],
        "dateWindow": f"{window['from']} to {window['toExclusive']}",
        "timezone": window["timezone"],
        "assumptions": (
            "Last 30 days follows the completed dashboard window convention: "
            "the 30 India business days ending before today. Weeks are grouped "
            "by Asia/Kolkata business week."
        ),
        "caveats": (
            "Weekly GTV is gross successful card spend; wallet loads, refunds, "
            "and marketplace reward reconciliation rows are excluded."
        ),
        "freshness": f"Live Supabase read at query runtime on {window['today']}.",
        "resultSummary": (
            "Returns weekly GTV, successful card transaction count, card users, "
            "and source freshness for the completed 30-day window."
        ),
        "friction": "none",
        "conventionAdded": "none",
        "repeatPromoteCandidate": True,
    }


def _top_card_spenders_request_for_window(
    question: str,
    *,
    window: dict[str, str],
    period_label: str,
    assumptions: str,
) -> dict[str, Any]:
    sql = f"""
    with
    {_classified_transactions_cte_sql(window["from"], window["toExclusive"])},
    spenders as (
      select
        nullif(trim(concat_ws(' ', p.first_name, p.middle_name, p.last_name)), '') as user_name,
        ct.user_id::text as user_id,
        count(*)::int as txn_count,
        coalesce(sum(ct.debit_amount), 0)::float as gross_spend_inr,
        (coalesce(sum(ct.debit_amount), 0) / nullif(count(*), 0))::float as avg_txn_value_inr,
        min(ct.business_transaction_timestamp)::text as first_card_txn_at,
        max(ct.business_transaction_timestamp)::text as last_card_txn_at,
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
      group by ct.user_id, p.first_name, p.middle_name, p.last_name
    )
    select
      row_number() over (
        order by gross_spend_inr desc, txn_count desc, coalesce(user_name, user_id)
      )::int as rank,
      user_name,
      user_id,
      txn_count,
      gross_spend_inr,
      avg_txn_value_inr,
      first_card_txn_at,
      last_card_txn_at,
      source_freshness
    from spenders
    order by gross_spend_inr desc, txn_count desc, coalesce(user_name, user_id)
  """

    return {
        "question": question,
        "interpretedDefinition": (
            f"Card spenders ranked by gross Elixir card spend (GTV) for {period_label} "
            "in Asia/Kolkata."
        ),
        "metricIds": ["gtv", "active_spender"],
        "sql": sql,
        "resultType": "users",
        "sources": ["transactions", "marketplace_order", "profiles"],
        "dateWindow": f"{window['from']} to {window['toExclusive']}",
        "timezone": window["timezone"],
        "assumptions": assumptions,
        "caveats": (
            "Wallet loads, marketplace reward reconciliation debits, deleted users, "
            "and non-card spend rows are excluded. Refunds/reversals are not netted."
        ),
        "freshness": f"Live Supabase read at query runtime on {window['today']}.",
        "resultSummary": (
            "Returns card spenders ranked by gross spend, transaction count, "
            "average transaction value, and first/latest card transaction timestamp."
        ),
        "friction": "none",
        "conventionAdded": "none",
        "repeatPromoteCandidate": True,
    }


def _top_card_spenders_7d_request(question: str) -> dict[str, Any]:
    window = _india_last_days_window(7)
    return _top_card_spenders_request_for_window(
        question,
        window=window,
        period_label="the last 7 IST days, including today-to-date",
        assumptions=(
            "Last 7 days includes today-to-date and the previous 6 India business days. "
            "Spend means GTV: successful card spend only."
        ),
    )


def _top_card_spender_7d_request(question: str) -> dict[str, Any]:
    return _top_card_spenders_7d_request(question)


def _top_card_spenders_period_request(
    question: str,
    *,
    period_key: str,
) -> dict[str, Any]:
    window = _relative_period_window(period_key)
    label = _relative_period_label(period_key)
    assumption = _relative_period_assumption(period_key)
    return _top_card_spenders_request_for_window(
        question,
        window=window,
        period_label=label,
        assumptions=f"{assumption} Spend means GTV: successful card spend only.",
    )


def _top_card_spender_7d_spend_breakdown_request(question: str) -> dict[str, Any]:
    window = _india_last_days_window(7)
    sql = f"""
    with
    {_classified_transactions_cte_sql(window["from"], window["toExclusive"])},
    ranked_spenders as (
      select
        ct.user_id,
        concat_ws(' ', p.first_name, p.middle_name, p.last_name) as user_name,
        count(*)::int as txn_count,
        coalesce(sum(ct.debit_amount), 0)::float as gross_spend_inr
      from classified_transactions ct
      left join profiles p on p.id = ct.user_id
      where ct.is_card_spend = true
        and ct.is_reward_reconciliation = false
        and ct.user_id is not null
        and coalesce(p.is_deleted, false) = false
      group by ct.user_id, p.first_name, p.middle_name, p.last_name
    ),
    top_spender as (
      select *
      from ranked_spenders
      order by gross_spend_inr desc, txn_count desc, user_name
      limit 1
    )
    select
      ts.user_name as top_user_name,
      coalesce(nullif(ct.merchant_name, ''), 'Unknown merchant') as merchant_name,
      coalesce(nullif(ct.description, ''), 'No description') as description,
      count(*)::int as txn_count,
      coalesce(sum(ct.debit_amount), 0)::float as gross_spend_inr,
      max(ct.business_transaction_timestamp)::text as latest_card_txn_at
    from classified_transactions ct
    join top_spender ts on ts.user_id = ct.user_id
    where ct.is_card_spend = true
      and ct.is_reward_reconciliation = false
    group by
      ts.user_name,
      coalesce(nullif(ct.merchant_name, ''), 'Unknown merchant'),
      coalesce(nullif(ct.description, ''), 'No description')
    order by gross_spend_inr desc, txn_count desc, merchant_name, description
  """

    return {
        "question": question,
        "interpretedDefinition": (
            "Merchant and description breakdown for the highest gross Elixir "
            "card spender over the last 7 Asia/Kolkata days, including today-to-date."
        ),
        "metricIds": ["gtv", "active_spender"],
        "sql": sql,
        "resultType": "breakdown",
        "sources": ["transactions", "marketplace_order", "profiles"],
        "dateWindow": f"{window['from']} to {window['toExclusive']}",
        "timezone": window["timezone"],
        "assumptions": (
            "Highest spender is selected by gross successful card spend / GTV "
            "over the last 7 India days, then that user's spend is grouped by "
            "merchant and transaction description."
        ),
        "caveats": (
            "Wallet loads, marketplace reward reconciliation debits, deleted users, "
            "and non-card spend rows are excluded. Refunds/reversals are not netted."
        ),
        "freshness": f"Live Supabase read at query runtime on {window['today']}.",
        "resultSummary": (
            "Returns the top spender's merchant-description spend breakdown, "
            "transaction count, and latest card transaction timestamp."
        ),
        "friction": "none",
        "conventionAdded": "none",
        "repeatPromoteCandidate": True,
    }
