"""Profile-owned Hermes tools for Elixir analytics runner calls."""

from __future__ import annotations

import json
import logging
import subprocess
import time
from typing import Any

from . import answer_payloads as _answer_payloads
from . import card_shortcut_requests as _card_shortcut_requests
from . import commerce_shortcut_requests as _commerce_shortcut_requests
from . import commerce_slack_formatters as _commerce_slack_formatters
from . import runner_modes as _runner_modes
from . import slack_handoff as _slack_handoff
from . import slack_payload_router as _slack_payload_router
from . import shortcut_requests as _shortcut_requests
from . import slack_formatters as _slack_formatters
from . import source_control_guard as _source_control_guard


TOOLSET = "elixir-analytics-runner"
ANALYTICS_REPO_ENV = _runner_modes.ANALYTICS_REPO_ENV
DEFAULT_ANALYTICS_REPO = _runner_modes.DEFAULT_ANALYTICS_REPO
DEFAULT_ANALYTICS_BASE_URL = _runner_modes.DEFAULT_ANALYTICS_BASE_URL
DEFAULT_TIMEOUT_SECONDS = _runner_modes.DEFAULT_TIMEOUT_SECONDS
MAX_TIMEOUT_SECONDS = _runner_modes.MAX_TIMEOUT_SECONDS
DEFAULT_FAST_PATH_TIMEOUT_SECONDS = 8
DEFAULT_MAX_ROWS = _runner_modes.DEFAULT_MAX_ROWS
DEFAULT_QUESTION_MAX_ROWS = _runner_modes.DEFAULT_QUESTION_MAX_ROWS
DEFAULT_FAST_PATH_MAX_ROWS = _shortcut_requests.DEFAULT_FAST_PATH_MAX_ROWS
MAX_ROWS = _runner_modes.MAX_ROWS
MAX_COMPACT_SLACK_TEXT_CHARS = _answer_payloads.MAX_COMPACT_SLACK_TEXT_CHARS
MAX_DIRECT_FINAL_SLACK_TEXT_CHARS = _answer_payloads.MAX_DIRECT_FINAL_SLACK_TEXT_CHARS
LOGGER = logging.getLogger("hermes.elixir_analytics_runner")
RITIK_ONLY_MODES = _source_control_guard.RITIK_ONLY_MODES
DEFAULT_SOURCE_CHANGE_ALLOWED_IDENTITIES = (
    _source_control_guard.DEFAULT_SOURCE_CHANGE_ALLOWED_IDENTITIES
)
SOURCE_CONTROL_TOOL_NAMES = _source_control_guard.SOURCE_CONTROL_TOOL_NAMES
SOURCE_CONTROL_PATTERNS = _source_control_guard.SOURCE_CONTROL_PATTERNS
RITIK_ONLY_MESSAGE = _source_control_guard.RITIK_ONLY_MESSAGE
MERCHANT_QUERY_STOPWORDS = _commerce_shortcut_requests.MERCHANT_QUERY_STOPWORDS
COMPACT_ELIXIR_ANALYTICS_SKILL = """# Elixir Analytics Runtime Brief

## Mandatory Slack Fast Path

For every plain Slack analytics data question, call `analytics_answer` with
the exact raw Slack question before planning, querying manually, inspecting
files, or editing source.

If the completed result includes `answerText`, use that text as the Slack-facing
answer. Add at most one short caveat sentence. Do not add a dashboard link
merely because a nested source result has a dashboard URL; the analytics answer
text decides whether a link belongs. Do not expose hidden SQL/HogQL, raw rows,
or source-maintenance work before replying.

Use `max_rows: 25` for user lists, merchant lists, rankings, and breakdowns
unless the user explicitly asks for a larger export.

## Routing

- Saved business metrics: prefer `analytics_answer`; it promotes known topics
  such as `show GTV last 30 days by week` to saved query dashboards.
- Self-serve help: use `analytics_answer` for "what can Chandler do?" so the
  runner returns a fast Slack-ready help menu without querying data.
- Supabase business questions: use `analytics_answer` first, then
  `supabase_ad_hoc` only if the runner asks for a model-built request.
- Wearable questions are strict: use wearable-identified totals only when
  source evidence identifies hardware or a dedicated wearable ecosystem.
  APPLE_HEALTH and TERRA are sync rails; APPLE_HEALTH, generic GOOGLE,
  Google Fit, Health Connect, and generic TERRA are health-sync provider
  evidence, not wearable proof by themselves. Accepted Samsung/Huawei watch
  assumptions and curated Terra device source strings/model evidence can be
  wearable evidence. Default wearable denominator is non-deleted card-issued
  users. Apple Health device attribution comes from profiles.onboardstatus
  healthDeviceManufacturer and does not require an Apple Health provider row by
  default. Apple Watch requires exact WatchN,M hardware tokens. Brand rollups
  are business-facing; source labels are QA/debug. Wearable active-user
  questions default to combined active users over the rolling last 30
  Asia/Kolkata calendar days; do not answer those percentages from the
  single-source wearable shortcut because the denominator is card activity OR
  app activity.
- App-active/PostHog questions: use `analytics_answer` first; "app active users"
  is not ambiguous. Keep app active separate unless asked to combine.
- Top spender questions default to card GTV by user. Do not clarify spend
  source; call `analytics_answer` and state the card-spend assumption.
- Ambiguous "active users": use `clarify` to ask whether the user means card
  active, app active, or combined active before querying.
- Definition/glossary/query/dashboard change requests: use
  `source_change_plan`, then `source_change_scope_check` before committing.
- Self-improvement reviews: use `self_improvement_check`, then
  `self_improvement_plan` only when due or explicitly requested.

## Answer Rules

Use Chandler's Slack voice: answer first, compact facts second, proof/source
only when asked. Do not dump metric contracts, source tables, freshness, SQL,
or fine print in Slack answers. Single-number KPIs usually stay
Slack-only. Include dashboard links only when present in `answerText`
and useful for trends, rankings, breakdowns, or larger tables. Never mutate
analytics source tables.
Source-control actions, commits, pushes, and PRs are Ritik-only in
Slack unless `ELIXIR_ANALYTICS_SOURCE_CHANGE_ALLOWED_USERS` explicitly allowlists
others.
"""


def _json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


_coerce_bool = _runner_modes.coerce_bool
_coerce_int = _runner_modes.coerce_int
_profile_env = _runner_modes.profile_env
_analytics_repo = _runner_modes.analytics_repo
_parse_json_stdout = _runner_modes.parse_json_stdout
_bounded_tail = _runner_modes.bounded_tail
_runner_command = _runner_modes.runner_command
_safe_payload_summary = _answer_payloads.safe_payload_summary
_has_answer_artifact = _answer_payloads.has_answer_artifact
_compact_slack_text = _answer_payloads.compact_slack_text
_dashboard_label_for_url = _answer_payloads.dashboard_label_for_url
_slack_dashboard_line_from_text = _answer_payloads.slack_dashboard_line_from_text
_compact_direct_final_slack_text = _answer_payloads.compact_direct_final_slack_text
_compact_answer_question_payload = _answer_payloads.compact_answer_question_payload
_direct_dashboard_link = _answer_payloads.direct_dashboard_link
_slack_dashboard_link = _answer_payloads.slack_dashboard_link
_direct_final_response_for_answer_payload = (
    _answer_payloads.direct_final_response_for_answer_payload
)
_clarify_instruction_for_answer_payload = _answer_payloads.clarify_instruction_for_answer_payload
_session_platform_identity = _source_control_guard.session_platform_identity
_source_change_allowed_identities = _source_control_guard.source_change_allowed_identities
_is_ritik_source_change_request = _source_control_guard.is_ritik_source_change_request
_permission_denied_result = _source_control_guard.permission_denied_result
_tool_args_text = _source_control_guard.tool_args_text
_is_source_control_tool_call = _source_control_guard.is_source_control_tool_call
_block_non_ritik_source_control_tool = (
    _source_control_guard.block_non_ritik_source_control_tool
)


_normalize_question = _shortcut_requests._normalize_question
_escape_sql_literal = _shortcut_requests._escape_sql_literal
_escape_sql_like = _shortcut_requests._escape_sql_like
_clean_merchant_candidate = _commerce_shortcut_requests._clean_merchant_candidate
_india_last_days_window = _shortcut_requests._india_last_days_window
_india_week_to_date_window = _shortcut_requests._india_week_to_date_window
_india_yesterday_window = _shortcut_requests._india_yesterday_window
_india_completed_days_window = _shortcut_requests._india_completed_days_window
_shift_month = _shortcut_requests._shift_month
_india_completed_months_window = _shortcut_requests._india_completed_months_window
_classified_transactions_cte_sql = _shortcut_requests._classified_transactions_cte_sql
_relative_period_key = _shortcut_requests._relative_period_key
_relative_period_window = _shortcut_requests._relative_period_window
_relative_period_label = _shortcut_requests._relative_period_label
_relative_period_assumption = _shortcut_requests._relative_period_assumption
_extract_simple_merchant_query = _commerce_shortcut_requests._extract_simple_merchant_query
_extract_leading_merchant_query = _commerce_shortcut_requests._extract_leading_merchant_query
_extract_merchant_query = _commerce_shortcut_requests._extract_merchant_query
_merchant_display_name = _commerce_shortcut_requests._merchant_display_name
_merchant_match_filter_sql = _commerce_shortcut_requests._merchant_match_filter_sql
_merchant_card_spend_7d_query = _shortcut_requests._merchant_card_spend_7d_query
_merchant_card_spend_period_query = _shortcut_requests._merchant_card_spend_period_query
_merchant_users_this_week_query = _shortcut_requests._merchant_users_this_week_query
_merchant_users_period_query = _shortcut_requests._merchant_users_period_query
_top_merchants_card_spend_period_key = _shortcut_requests._top_merchants_card_spend_period_key
_card_gtv_daily_days = _shortcut_requests._card_gtv_daily_days
_definition_change_intent = _shortcut_requests._definition_change_intent
_card_gtv_completed_days = _shortcut_requests._card_gtv_completed_days
_card_gtv_period_key = _shortcut_requests._card_gtv_period_key
_matches_card_gtv_weekly_30d = _shortcut_requests._matches_card_gtv_weekly_30d
_matches_card_gtv_7d = _shortcut_requests._matches_card_gtv_7d
_matches_card_transaction_count_7d = _shortcut_requests._matches_card_transaction_count_7d
_card_transaction_count_period_key = _shortcut_requests._card_transaction_count_period_key
_is_top_card_spenders_rank_intent = _shortcut_requests._is_top_card_spenders_rank_intent
_is_top_card_spender_singular_intent = _shortcut_requests._is_top_card_spender_singular_intent
_matches_top_card_spenders_7d = _shortcut_requests._matches_top_card_spenders_7d
_top_card_spender_period_match = _shortcut_requests._top_card_spender_period_match
_matches_top_card_spender_7d = _shortcut_requests._matches_top_card_spender_7d
_matches_top_card_spender_7d_spend_breakdown = _shortcut_requests._matches_top_card_spender_7d_spend_breakdown
_matches_top_merchants_card_spend_7d = _shortcut_requests._matches_top_merchants_card_spend_7d
_matches_swiggy_spend_trend_10d = _shortcut_requests._matches_swiggy_spend_trend_10d
_matches_gym_milestone_avg_monthly_spend_3mo = _shortcut_requests._matches_gym_milestone_avg_monthly_spend_3mo
_matches_wearable_identified_customer_count = _shortcut_requests._matches_wearable_identified_customer_count
_requires_combined_active_wearable_user_denominator = (
    _shortcut_requests._requires_combined_active_wearable_user_denominator
)
_wearable_identified_customer_count_request = _shortcut_requests._wearable_identified_customer_count_request
_card_gtv_7d_request = _card_shortcut_requests._card_gtv_7d_request
_card_period_kpi_sql = _card_shortcut_requests._card_period_kpi_sql
_card_gtv_completed_days_request = _card_shortcut_requests._card_gtv_completed_days_request
_card_gtv_period_request = _card_shortcut_requests._card_gtv_period_request
_card_transaction_count_period_request = _card_shortcut_requests._card_transaction_count_period_request
_card_gtv_daily_request = _card_shortcut_requests._card_gtv_daily_request
_card_transaction_count_7d_request = _card_shortcut_requests._card_transaction_count_7d_request
_card_gtv_weekly_30d_request = _card_shortcut_requests._card_gtv_weekly_30d_request
_top_card_spenders_request_for_window = _card_shortcut_requests._top_card_spenders_request_for_window
_top_card_spenders_7d_request = _card_shortcut_requests._top_card_spenders_7d_request
_top_card_spender_7d_request = _card_shortcut_requests._top_card_spender_7d_request
_top_card_spenders_period_request = _card_shortcut_requests._top_card_spenders_period_request
_top_card_spender_7d_spend_breakdown_request = _card_shortcut_requests._top_card_spender_7d_spend_breakdown_request
_top_merchants_card_spend_7d_request = _commerce_shortcut_requests._top_merchants_card_spend_7d_request
_top_merchants_card_spend_period_request = _commerce_shortcut_requests._top_merchants_card_spend_period_request
_merchant_card_spend_7d_request = _commerce_shortcut_requests._merchant_card_spend_7d_request
_merchant_card_spend_period_request = _commerce_shortcut_requests._merchant_card_spend_period_request
_merchant_users_this_week_request = _commerce_shortcut_requests._merchant_users_this_week_request
_merchant_users_period_request = _commerce_shortcut_requests._merchant_users_period_request
_swiggy_spend_trend_10d_request = _commerce_shortcut_requests._swiggy_spend_trend_10d_request
_gym_milestone_avg_monthly_spend_3mo_request = _commerce_shortcut_requests._gym_milestone_avg_monthly_spend_3mo_request
_profile_answer_question_shortcut = _shortcut_requests._profile_answer_question_shortcut

_format_inr = _slack_formatters._format_inr
_format_inr_decimal = _slack_formatters._format_inr_decimal
_format_number = _slack_formatters._format_number
_as_float = _slack_formatters._as_float
_format_percent_change = _slack_formatters._format_percent_change
_profile_shortcut_dashboard_line = _slack_formatters._profile_shortcut_dashboard_line
_metadata_value = _slack_formatters._metadata_value
_card_gtv_7d_slack_text = _slack_formatters._card_gtv_7d_slack_text
_card_gtv_completed_days_slack_text = _slack_formatters._card_gtv_completed_days_slack_text
_card_transaction_count_7d_slack_text = _slack_formatters._card_transaction_count_7d_slack_text
_period_copula = _slack_formatters._period_copula
_card_gtv_period_slack_text = _slack_formatters._card_gtv_period_slack_text
_card_transaction_count_period_slack_text = _slack_formatters._card_transaction_count_period_slack_text
_card_gtv_daily_slack_text = _slack_formatters._card_gtv_daily_slack_text
_card_gtv_weekly_30d_slack_text = _slack_formatters._card_gtv_weekly_30d_slack_text
_top_card_spender_7d_slack_text = _slack_formatters._top_card_spender_7d_slack_text
_slack_table_cell = _slack_formatters._slack_table_cell
_top_card_spender_period_title = _slack_formatters._top_card_spender_period_title
_top_card_spender_period_slack_text = _slack_formatters._top_card_spender_period_slack_text
_top_card_spenders_period_slack_text = _slack_formatters._top_card_spenders_period_slack_text
_top_card_spender_7d_spend_breakdown_slack_text = _slack_formatters._top_card_spender_7d_spend_breakdown_slack_text
_top_merchants_card_spend_7d_slack_text = _commerce_slack_formatters._top_merchants_card_spend_7d_slack_text
_top_merchants_card_spend_period_slack_text = _commerce_slack_formatters._top_merchants_card_spend_period_slack_text
_merchant_query_from_payload = _commerce_slack_formatters._merchant_query_from_payload
_merchant_card_spend_7d_slack_text = _commerce_slack_formatters._merchant_card_spend_7d_slack_text
_merchant_card_spend_period_slack_text = _commerce_slack_formatters._merchant_card_spend_period_slack_text
_merchant_users_this_week_slack_text = _commerce_slack_formatters._merchant_users_this_week_slack_text
_merchant_users_period_slack_text = _commerce_slack_formatters._merchant_users_period_slack_text
_swiggy_spend_trend_10d_slack_text = _commerce_slack_formatters._swiggy_spend_trend_10d_slack_text
_gym_milestone_avg_monthly_spend_3mo_slack_text = _commerce_slack_formatters._gym_milestone_avg_monthly_spend_3mo_slack_text
_profile_answer_question_payload = _slack_payload_router._profile_answer_question_payload

def _is_elixir_analytics_skill_request(args: dict[str, Any]) -> bool:
    name = str(args.get("name") or "").strip().lower()
    if str(args.get("file_path") or args.get("filePath") or "").strip():
        return False
    return name in {"elixir-analytics", "analytics/elixir-analytics"}


def _compact_skill_view_result(args: dict[str, Any], result: str) -> str | None:
    if not _is_elixir_analytics_skill_request(args):
        return None

    try:
        payload = json.loads(result)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("success") is not True:
        return None

    result_name = str(payload.get("name") or "").strip().lower()
    if result_name not in {"elixir-analytics", "analytics/elixir-analytics"}:
        return None

    compact_payload = dict(payload)
    compact_payload["content"] = COMPACT_ELIXIR_ANALYTICS_SKILL
    compact_payload["contentCompacted"] = True
    return _json_dumps(compact_payload)


def _transform_tool_result(
    *,
    tool_name: str,
    args: dict[str, Any],
    result: str,
    **_: Any,
) -> str | None:
    if tool_name == "skill_view" and isinstance(args, dict) and isinstance(result, str):
        return _compact_skill_view_result(args, result)
    return None


def _log_runner_result(result: dict[str, Any]) -> None:
    fields = {
        "mode": result.get("mode"),
        "ok": result.get("ok"),
        "elapsedSeconds": result.get("elapsedSeconds"),
        "errorType": result.get("errorType"),
    }
    if result.get("ok"):
        summary_source = result.get("payload")
        if summary_source is None:
            summary_source = result
        fields.update(_safe_payload_summary(summary_source))
        LOGGER.info(
            "analytics runner completed %s",
            " ".join(f"{key}={value}" for key, value in fields.items() if value is not None),
        )
    else:
        LOGGER.warning(
            "analytics runner failed %s",
            " ".join(f"{key}={value}" for key, value in fields.items() if value is not None),
        )


def _payload_with_contract_slack_text(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    if isinstance(payload.get("slackText"), str) and payload["slackText"].strip():
        return payload

    answer_payload = payload.get("answerPayload")
    if not isinstance(answer_payload, dict):
        return payload
    final_text = answer_payload.get("finalText")
    if not isinstance(final_text, str) or not final_text.strip():
        return payload

    normalized = dict(payload)
    normalized["slackText"] = final_text
    return normalized


def _combined_active_wearable_instruction(question: str) -> str | None:
    normalized = _normalize_question(question)
    if not _requires_combined_active_wearable_user_denominator(normalized):
        return None
    return (
        "This is a combined-source wearable percentage question. Do not answer "
        "from the single-source wearable shortcut. Use the wearable_identified_users "
        "contract for Supabase wearable evidence and card-issued eligibility, then "
        "combine it with PostHog authenticated app activity to build the rolling "
        "last 30 Asia/Kolkata day active-user denominator. The denominator is "
        "distinct users with card activity OR app activity. Report requested brand "
        "rollups, including Whoop, Samsung Watch, and Ultrahuman, as distinct users "
        "and percentage of that combined-active denominator."
    )


def run_elixir_analytics_runner(args: dict[str, Any]) -> dict[str, Any]:
    mode = str(args.get("mode") or "").strip()
    if not mode and str(args.get("question") or "").strip():
        mode = "answer_question"
    profile_shortcut: str | None = None
    profile_shortcut_request: dict[str, Any] | None = None
    started = time.monotonic()
    timeout_seconds = _coerce_int(
        args.get("timeout_seconds"),
        DEFAULT_TIMEOUT_SECONDS,
        minimum=1,
        maximum=MAX_TIMEOUT_SECONDS,
    )

    try:
        denied = _permission_denied_result(mode)
        if denied is not None:
            denied["elapsedSeconds"] = round(time.monotonic() - started, 3)
            _log_runner_result(denied)
            return denied

        shortcut_command = _profile_answer_question_shortcut(args, mode=mode)
        if shortcut_command is None:
            command, stdin = _runner_command(args)
        else:
            command, stdin, profile_shortcut, profile_shortcut_request = shortcut_command
        env = _profile_env()
        repo = _analytics_repo(env)
        if not repo.is_dir():
            result = {
                "ok": False,
                "mode": mode,
                "errorType": "missing_repo",
                "message": f"Analytics repo not found at {repo}",
            }
            _log_runner_result(result)
            return result

        completed = subprocess.run(
            command,
            cwd=str(repo),
            env=env,
            input=stdin,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        result = {
            "ok": False,
            "mode": mode,
            "errorType": "timeout",
            "timeoutSeconds": timeout_seconds,
            "elapsedSeconds": round(time.monotonic() - started, 3),
            "stdout": _bounded_tail(str(exc.output or "")),
            "stderr": _bounded_tail(str(exc.stderr or "")),
        }
        _log_runner_result(result)
        return result
    except Exception as exc:
        result = {
            "ok": False,
            "mode": mode,
            "errorType": exc.__class__.__name__,
            "message": str(exc),
            "elapsedSeconds": round(time.monotonic() - started, 3),
        }
        _log_runner_result(result)
        return result

    payload = _parse_json_stdout(completed.stdout)
    if completed.returncode == 0 and profile_shortcut and profile_shortcut_request:
        payload = _profile_answer_question_payload(
            shortcut=profile_shortcut,
            payload=payload,
            request=profile_shortcut_request,
        )
    if completed.returncode == 0 and mode == "run_contract":
        payload = _payload_with_contract_slack_text(payload)
    if completed.returncode == 0 and mode == "answer_question":
        payload = _compact_answer_question_payload(payload)

    if completed.returncode != 0:
        result = {
            "ok": False,
            "mode": mode,
            "errorType": "runner_failed",
            "exitCode": completed.returncode,
            "elapsedSeconds": round(time.monotonic() - started, 3),
            "stdout": _bounded_tail(completed.stdout),
            "stderr": _bounded_tail(completed.stderr),
            "payload": payload,
        }
        _log_runner_result(result)
        return result

    result = {
        "ok": True,
        "mode": mode,
        "elapsedSeconds": round(time.monotonic() - started, 3),
        "payload": payload,
    }
    if mode in {"answer_question", "run_contract"}:
        direct_final_response = _direct_final_response_for_answer_payload(payload)
        if direct_final_response:
            result["hermes_direct_final_response"] = direct_final_response
    if mode == "answer_question":
        clarify_instruction = _clarify_instruction_for_answer_payload(payload)
        if clarify_instruction:
            result["hermes_agent_instruction"] = clarify_instruction
        combined_active_wearable_instruction = _combined_active_wearable_instruction(
            str(args.get("question") or "")
        )
        if combined_active_wearable_instruction and not direct_final_response:
            result["hermes_agent_instruction"] = combined_active_wearable_instruction
    _log_runner_result(result)
    return result


def _answer_runner_command(args: dict[str, Any]) -> tuple[list[str], str]:
    question = str(args.get("question") or "").strip()
    if not question:
        raise ValueError("analytics_answer requires question.")
    max_rows = _coerce_int(
        args.get("max_rows"),
        DEFAULT_QUESTION_MAX_ROWS,
        minimum=1,
        maximum=MAX_ROWS,
    )
    command = [
        "node",
        "--import",
        "tsx",
        "scripts/run-analytics-answer.ts",
        "--max-rows",
        str(max_rows),
    ]
    if _coerce_bool(args.get("dry_run")):
        command.append("--dry-run")
    return command, question


def run_analytics_answer(args: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    timeout_seconds = _coerce_int(
        args.get("timeout_seconds"),
        DEFAULT_TIMEOUT_SECONDS,
        minimum=1,
        maximum=MAX_TIMEOUT_SECONDS,
    )

    try:
        command, stdin = _answer_runner_command(args)
        env = _profile_env()
        repo = _analytics_repo(env)
        if not repo.is_dir():
            result = {
                "ok": False,
                "status": "blocked",
                "errorType": "missing_repo",
                "message": f"Analytics repo not found at {repo}",
            }
            _log_runner_result({"ok": False, "mode": "analytics_answer", **result})
            return result

        completed = subprocess.run(
            command,
            cwd=str(repo),
            env=env,
            input=stdin,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        result = {
            "ok": False,
            "status": "blocked",
            "errorType": "timeout",
            "timeoutSeconds": timeout_seconds,
            "elapsedSeconds": round(time.monotonic() - started, 3),
            "stdout": _bounded_tail(str(exc.output or "")),
            "stderr": _bounded_tail(str(exc.stderr or "")),
        }
        _log_runner_result({"mode": "analytics_answer", **result})
        return result
    except Exception as exc:
        result = {
            "ok": False,
            "status": "blocked",
            "errorType": exc.__class__.__name__,
            "message": str(exc),
            "elapsedSeconds": round(time.monotonic() - started, 3),
        }
        _log_runner_result({"mode": "analytics_answer", **result})
        return result

    payload = _parse_json_stdout(completed.stdout)
    if completed.returncode != 0:
        result = {
            "ok": False,
            "status": "blocked",
            "errorType": "runner_failed",
            "exitCode": completed.returncode,
            "elapsedSeconds": round(time.monotonic() - started, 3),
            "stdout": _bounded_tail(completed.stdout),
            "stderr": _bounded_tail(completed.stderr),
            "payload": payload,
        }
        _log_runner_result({"mode": "analytics_answer", **result})
        return result

    if not isinstance(payload, dict):
        result = {
            "ok": False,
            "status": "blocked",
            "errorType": "invalid_payload",
            "message": "analytics_answer runner returned non-JSON payload.",
            "elapsedSeconds": round(time.monotonic() - started, 3),
            "payload": payload,
        }
        _log_runner_result({"mode": "analytics_answer", **result})
        return result

    result = {
        **payload,
        "mode": "analytics_answer",
        "elapsedSeconds": round(time.monotonic() - started, 3),
    }
    answer_text = payload.get("answerText")
    if (
        isinstance(answer_text, str)
        and answer_text.strip()
        and _has_answer_artifact(payload)
    ):
        result["hermes_direct_final_response"] = _compact_direct_final_slack_text(
            answer_text
        )
    _log_runner_result(result)
    return result


def _handler(args: dict[str, Any], **_: Any) -> str:
    return _json_dumps(run_elixir_analytics_runner(args))


def _answer_handler(args: dict[str, Any], **_: Any) -> str:
    return _json_dumps(run_analytics_answer(args))


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _block_non_ritik_source_control_tool)
    ctx.register_hook("transform_tool_result", _transform_tool_result)
    ctx.register_hook(
        "pre_gateway_dispatch",
        _slack_handoff.pre_gateway_elixir_analytics_agent_handoff,
    )
    ctx.register_tool(
        name="analytics_answer",
        toolset=TOOLSET,
        schema={
            "name": "analytics_answer",
            "description": (
                "Answer a Slack business analytics question through the primary "
                "analytics AnswerArtifact boundary. Use this for ordinary "
                "read-only quantitative questions. It returns answerText for "
                "Slack and answerArtifact for provenance and follow-up grounding."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "Exact user analytics question or resolved follow-up question.",
                    },
                    "max_rows": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_ROWS,
                        "default": DEFAULT_QUESTION_MAX_ROWS,
                    },
                    "dry_run": {"type": "boolean", "default": False},
                    "timeout_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_TIMEOUT_SECONDS,
                        "default": DEFAULT_TIMEOUT_SECONDS,
                    },
                },
                "required": ["question"],
                "additionalProperties": False,
            },
        },
        handler=_answer_handler,
        description="Answer Elixir analytics questions with an AnswerArtifact.",
    )
    ctx.register_tool(
        name="elixir_analytics_runner",
        toolset=TOOLSET,
        schema={
            "name": "elixir_analytics_runner",
            "description": (
                "Run Elixir analytics common-question shortcuts, planner, "
                            "saved-topic, Supabase ad hoc, PostHog ad hoc, source-change, "
                "Answer Contract validation/execution, or self-improvement runners "
                "without shell/code-execution setup. "
                "For plain Slack analytics questions, use `analytics_answer` "
                "first. Use this legacy runner for source changes, contract "
                "validation/execution, self-improvement, and advanced fallback "
                "paths. If a deliberate fallback run returns Slack-ready text "
                "or an AnswerArtifact, preserve that analytics-owned output "
                "instead of recomputing the number. "
                "For ambiguous business terms, use `clarify` so the next Slack "
                "reply is captured inside the same agent run."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {
                        "description": (
                            "Runner mode. Use analytics_answer for plain Slack "
                            "analytics answers; use these modes for advanced "
                            "fallbacks, source changes, and contract operations."
                        ),
                        "type": "string",
                        "enum": [
                            "plan",
                            "answer_question",
                            "saved_topic",
                            "supabase_ad_hoc",
                            "posthog_ad_hoc",
                            "validate_contract",
                            "run_contract",
                            "source_change_plan",
                            "source_change_scope_check",
                            "self_improvement_check",
                            "self_improvement_plan",
                        ],
                    },
                    "question": {"type": "string"},
                    "topic_id": {"type": "string"},
                    "range": {
                        "type": "string",
                        "enum": ["7d", "30d", "90d"],
                        "default": "30d",
                    },
                    "request": {
                        "description": (
                            "AdHocQueryRequest/PostHogQueryRequest object for "
                            "ad hoc modes, or source-change request text for "
                            "source_change_plan/source_change_scope_check mode."
                        ),
                        "anyOf": [
                            {"type": "object"},
                            {"type": "string"},
                        ],
                    },
                    "contract": {
                        "description": (
                            "Answer Contract JSON for validate_contract or "
                            "run_contract modes."
                        ),
                        "anyOf": [
                            {"type": "object"},
                            {"type": "string"},
                        ],
                    },
                    "changed_files": {
                        "description": (
                            "Changed repo file paths for source_change_scope_check."
                        ),
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "allow_unexpected_files": {
                        "description": (
                            "For source_change_scope_check: downgrade files outside "
                            "the source-change plan from blockers to warnings."
                        ),
                        "type": "boolean",
                        "default": False,
                    },
                    "query_log": {
                        "description": (
                            "Query log path for self_improvement_check or "
                            "self_improvement_plan. Defaults to QUERY_LOG.md "
                            "in the analytics repo."
                        ),
                        "type": "string",
                    },
                    "max_rows": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_ROWS,
                        "default": DEFAULT_MAX_ROWS,
                    },
                    "dry_run": {"type": "boolean", "default": False},
                    "timeout_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_TIMEOUT_SECONDS,
                        "default": DEFAULT_TIMEOUT_SECONDS,
                    },
                },
                "required": ["mode"],
                "additionalProperties": False,
            },
        },
        handler=_handler,
        description="Run Elixir analytics deterministic runners.",
    )
