"""Shortcut payload router for Chandler Slack answer rendering."""

from __future__ import annotations

from typing import Any

from .commerce_slack_formatters import (
    _gym_milestone_avg_monthly_spend_3mo_slack_text,
    _merchant_card_spend_7d_slack_text,
    _merchant_card_spend_period_slack_text,
    _merchant_users_period_slack_text,
    _merchant_users_this_week_slack_text,
    _swiggy_spend_trend_10d_slack_text,
    _top_merchants_card_spend_7d_slack_text,
    _top_merchants_card_spend_period_slack_text,
)
from .runner_modes import coerce_int
from .slack_formatters import (
    _card_gtv_7d_slack_text,
    _card_gtv_completed_days_slack_text,
    _card_gtv_daily_slack_text,
    _card_gtv_period_slack_text,
    _card_gtv_weekly_30d_slack_text,
    _card_transaction_count_7d_slack_text,
    _card_transaction_count_period_slack_text,
    _top_card_spender_7d_slack_text,
    _top_card_spender_7d_spend_breakdown_slack_text,
    _top_card_spender_period_slack_text,
    _top_card_spenders_period_slack_text,
)


_coerce_int = coerce_int


def _profile_answer_question_payload(
    *,
    shortcut: str,
    payload: Any,
    request: dict[str, Any],
) -> Any:
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return payload

    answer_payload = dict(payload)
    metadata = answer_payload.get("metadata")
    if not isinstance(metadata, dict) or not metadata:
        answer_payload["metadata"] = request

    if shortcut == "top_card_spender_7d":
        answer_payload["slackText"] = _top_card_spender_7d_slack_text(answer_payload)
    elif shortcut == "top_card_spenders_7d":
        answer_payload["slackText"] = _top_card_spenders_period_slack_text(
            answer_payload,
            period_key="last_7d",
        )
    elif shortcut == "top_card_spender_today":
        answer_payload["slackText"] = _top_card_spender_period_slack_text(
            answer_payload,
            period_key="today",
        )
    elif shortcut == "top_card_spender_yesterday":
        answer_payload["slackText"] = _top_card_spender_period_slack_text(
            answer_payload,
            period_key="yesterday",
        )
    elif shortcut == "top_card_spender_this_week":
        answer_payload["slackText"] = _top_card_spender_period_slack_text(
            answer_payload,
            period_key="this_week",
        )
    elif shortcut == "top_card_spenders_today":
        answer_payload["slackText"] = _top_card_spenders_period_slack_text(
            answer_payload,
            period_key="today",
        )
    elif shortcut == "top_card_spenders_yesterday":
        answer_payload["slackText"] = _top_card_spenders_period_slack_text(
            answer_payload,
            period_key="yesterday",
        )
    elif shortcut == "top_card_spenders_this_week":
        answer_payload["slackText"] = _top_card_spenders_period_slack_text(
            answer_payload,
            period_key="this_week",
        )
    elif shortcut == "top_merchants_card_spend_7d":
        answer_payload["slackText"] = _top_merchants_card_spend_7d_slack_text(answer_payload)
    elif shortcut == "top_merchants_card_spend_today":
        answer_payload["slackText"] = _top_merchants_card_spend_period_slack_text(
            answer_payload,
            period_key="today",
        )
    elif shortcut == "top_merchants_card_spend_yesterday":
        answer_payload["slackText"] = _top_merchants_card_spend_period_slack_text(
            answer_payload,
            period_key="yesterday",
        )
    elif shortcut == "top_merchants_card_spend_this_week":
        answer_payload["slackText"] = _top_merchants_card_spend_period_slack_text(
            answer_payload,
            period_key="this_week",
        )
    elif shortcut == "top_card_spender_7d_spend_breakdown":
        answer_payload["slackText"] = _top_card_spender_7d_spend_breakdown_slack_text(
            answer_payload
        )
    elif shortcut == "swiggy_spend_trend_10d":
        answer_payload["slackText"] = _swiggy_spend_trend_10d_slack_text(answer_payload)
    elif shortcut == "merchant_card_spend_7d":
        answer_payload["slackText"] = _merchant_card_spend_7d_slack_text(answer_payload)
    elif shortcut == "merchant_card_spend_today":
        answer_payload["slackText"] = _merchant_card_spend_period_slack_text(
            answer_payload,
            period_key="today",
        )
    elif shortcut == "merchant_card_spend_yesterday":
        answer_payload["slackText"] = _merchant_card_spend_period_slack_text(
            answer_payload,
            period_key="yesterday",
        )
    elif shortcut == "merchant_card_spend_this_week":
        answer_payload["slackText"] = _merchant_card_spend_period_slack_text(
            answer_payload,
            period_key="this_week",
        )
    elif shortcut == "merchant_users_this_week":
        answer_payload["slackText"] = _merchant_users_this_week_slack_text(answer_payload)
    elif shortcut == "merchant_users_today":
        answer_payload["slackText"] = _merchant_users_period_slack_text(
            answer_payload,
            period_key="today",
        )
    elif shortcut == "merchant_users_yesterday":
        answer_payload["slackText"] = _merchant_users_period_slack_text(
            answer_payload,
            period_key="yesterday",
        )
    elif shortcut == "card_transaction_count_7d":
        answer_payload["slackText"] = _card_transaction_count_7d_slack_text(answer_payload)
    elif shortcut == "card_transaction_count_today":
        answer_payload["slackText"] = _card_transaction_count_period_slack_text(
            answer_payload,
            period_key="today",
        )
    elif shortcut == "card_transaction_count_yesterday":
        answer_payload["slackText"] = _card_transaction_count_period_slack_text(
            answer_payload,
            period_key="yesterday",
        )
    elif shortcut == "card_transaction_count_this_week":
        answer_payload["slackText"] = _card_transaction_count_period_slack_text(
            answer_payload,
            period_key="this_week",
        )
    elif shortcut.startswith("card_gtv_last_") and shortcut.endswith("d"):
        days_text = shortcut.removeprefix("card_gtv_last_").removesuffix("d")
        answer_payload["slackText"] = _card_gtv_completed_days_slack_text(
            answer_payload,
            days=_coerce_int(days_text, 0, minimum=1, maximum=31),
        )
    elif shortcut == "card_gtv_7d":
        answer_payload["slackText"] = _card_gtv_7d_slack_text(answer_payload)
    elif shortcut == "card_gtv_today":
        answer_payload["slackText"] = _card_gtv_period_slack_text(
            answer_payload,
            period_key="today",
        )
    elif shortcut == "card_gtv_yesterday":
        answer_payload["slackText"] = _card_gtv_period_slack_text(
            answer_payload,
            period_key="yesterday",
        )
    elif shortcut == "card_gtv_this_week":
        answer_payload["slackText"] = _card_gtv_period_slack_text(
            answer_payload,
            period_key="this_week",
        )
    elif shortcut == "card_gtv_daily_7d":
        answer_payload["slackText"] = _card_gtv_daily_slack_text(
            answer_payload,
            days=7,
        )
    elif shortcut == "card_gtv_daily_30d":
        answer_payload["slackText"] = _card_gtv_daily_slack_text(
            answer_payload,
            days=30,
        )
    elif shortcut == "card_gtv_weekly_30d":
        answer_payload["slackText"] = _card_gtv_weekly_30d_slack_text(answer_payload)
    elif shortcut == "gym_milestone_avg_monthly_spend_3mo":
        answer_payload["slackText"] = _gym_milestone_avg_monthly_spend_3mo_slack_text(
            answer_payload
        )

    return {
        "ok": True,
        "route": "supabase_ad_hoc",
        "shortcut": shortcut,
        "payload": answer_payload,
    }
