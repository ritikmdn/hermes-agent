"""Profile-owned Hermes tools for Elixir analytics runner calls."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


TOOLSET = "elixir-analytics-runner"
ANALYTICS_REPO_ENV = "ELIXIR_ANALYTICS_REPO"
DEFAULT_ANALYTICS_REPO = "/Users/ritik/Coding/claude-analytics"
DEFAULT_ANALYTICS_BASE_URL = "https://analytics.joinelixir.club"
DEFAULT_TIMEOUT_SECONDS = 300
MAX_TIMEOUT_SECONDS = 900
DEFAULT_FAST_PATH_TIMEOUT_SECONDS = 8
DEFAULT_MAX_ROWS = 500
DEFAULT_QUESTION_MAX_ROWS = 100
DEFAULT_FAST_PATH_MAX_ROWS = 25
MAX_ROWS = 5000
MAX_COMPACT_SLACK_TEXT_CHARS = 2200
MAX_DIRECT_FINAL_SLACK_TEXT_CHARS = 1400
LOGGER = logging.getLogger("hermes.elixir_analytics_runner")
RITIK_ONLY_MODES = {
    "source_change_plan",
    "source_change_scope_check",
    "self_improvement_plan",
}
DEFAULT_SOURCE_CHANGE_ALLOWED_IDENTITIES = {"ritik", "ritik madan"}
SOURCE_CONTROL_TOOL_NAMES = {
    "execute_code",
    "terminal",
    "run_command",
    "shell_command",
}
SOURCE_CONTROL_PATTERNS = (
    re.compile(r"\bgit\s+(add|commit|push|merge|rebase|reset|switch|checkout|tag)\b", re.I),
    re.compile(r"\bgh\s+pr\s+(create|edit|merge|ready|close|reopen)\b", re.I),
)
REPLYING_PREFIX_RE = re.compile(r'\A\s*\[Replying to: "[\s\S]*?"\]\s*', re.I)
THREAD_CONTEXT_BLOCK_RE = re.compile(
    r"\A\s*\[Thread context[^\n]*\]\n[\s\S]*?\n\[End of thread context\]\s*",
    re.I,
)
THREAD_CONTEXT_CAPTURE_RE = re.compile(
    r"\A\s*\[Thread context[^\n]*\]\n(?P<context>[\s\S]*?)\n\[End of thread context\]\s*",
    re.I,
)
SLACK_BLOCK_KIT_SUFFIX_RE = re.compile(
    r"\s*\*Sent using\* ChatGPT[\s\S]*\Z",
    re.I,
)
RITIK_ONLY_MESSAGE = "Elixir analytics source-control actions are Ritik-only in Slack."
READ_ONLY_GUARD_MESSAGE = (
    "I cannot run destructive writes from Slack. Chandler is read-only for "
    "analytics questions; ask a read-only version instead."
)
DANGEROUS_ANALYTICS_MUTATION_RE = re.compile(
    r"\b("
    r"delete\s+from|drop\s+table|truncate\s+table|update\s+\w+\s+set|"
    r"insert\s+into|alter\s+table|create\s+table|grant\s+|revoke\s+"
    r")\b",
    re.I,
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
COMPACT_ELIXIR_ANALYTICS_SKILL = """# Elixir Analytics Runtime Brief

## Mandatory Slack Fast Path

For every plain Slack analytics data question, call `elixir_analytics_runner`
with mode='answer_question' and the exact raw Slack question before planning,
querying manually, inspecting files, or editing source.

If the completed result includes `payload.slackText`, use that text as the
Slack-facing answer. Add at most one short caveat sentence. Do not add a
dashboard link merely because `dashboardUrl` or `dashboardUrlPath` exists; the
runner's `slackText` decides whether a link belongs. Do not expose hidden
SQL/HogQL, raw rows, or source-maintenance work before replying.

Use `max_rows: 25` for user lists, merchant lists, rankings, and breakdowns
unless the user explicitly asks for a larger export.

## Routing

- Saved business metrics: prefer `answer_question`; it promotes known topics
  such as `show GTV last 30 days by week` to saved query dashboards.
- Self-serve help: use `answer_question` for "what can Chandler do?" so the
  runner returns a fast Slack-ready help menu without querying data.
- Supabase business questions: use `answer_question` first, then
  `supabase_ad_hoc` only if the runner asks for a model-built request.
- PostHog app questions: use `answer_question` first, then `posthog_ad_hoc`
  only if needed. Keep app active users separate from card active users unless
  the user asks to combine definitions.
- Ambiguous "active users": use `clarify` to ask whether the user means card
  active, app active, or combined active before querying.
- Definition/glossary/query/dashboard change requests: use
  `source_change_plan`, then `source_change_scope_check` before committing.
- Self-improvement reviews: use `self_improvement_check`, then
  `self_improvement_plan` only when due or explicitly requested.

## Answer Rules

Include rows or a compact summary, date window, freshness, assumptions/caveats,
and any direct dashboard link already present in `payload.slackText`. Single-number
KPI answers usually stay Slack-only. Never mutate analytics source tables. Keep
generic Hermes tools available for debugging, source changes, repo edits, and
runner gaps. Source-control actions, commits, pushes, and PRs are Ritik-only in
Slack unless `ELIXIR_ANALYTICS_SOURCE_CHANGE_ALLOWED_USERS` explicitly allowlists
another requester.
"""


def _json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _coerce_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(parsed, maximum))


def _platform_value(platform: Any) -> str:
    return str(getattr(platform, "value", platform) or "").strip().lower()


def _clean_slack_question_text(text: str) -> str:
    cleaned = str(text or "")
    while True:
        next_value = REPLYING_PREFIX_RE.sub("", cleaned)
        next_value = THREAD_CONTEXT_BLOCK_RE.sub("", next_value)
        if next_value == cleaned:
            break
        cleaned = next_value
    cleaned = SLACK_BLOCK_KIT_SUFFIX_RE.sub("", cleaned)
    return cleaned.strip()


def _is_plain_slack_analytics_question(question: str) -> bool:
    stripped = question.strip()
    if not stripped or len(stripped) > 2000:
        return False
    if stripped.startswith("/"):
        return False
    if any(pattern.search(stripped) for pattern in SOURCE_CONTROL_PATTERNS):
        return False
    return True


def _is_destructive_analytics_mutation(question: str) -> bool:
    return bool(DANGEROUS_ANALYTICS_MUTATION_RE.search(question or ""))


def _is_ambiguous_active_users_question(question: str) -> bool:
    normalized = _normalize_question(question)
    if not re.search(r"\b(active\s+users?|most\s+active\s+users?)\b", normalized):
        return False
    return not bool(
        re.search(
            r"\b(app|card|spend|spender|spenders|spent|gtv|combined|"
            r"transaction|transactions|event|events|session|sessions)\b",
            normalized,
        )
    )


def _agent_runtime_handoff_text(
    *,
    question: str,
    raw_text: str = "",
    active_user_ambiguity: bool = False,
) -> str:
    """Build an agent-owned Slack profile handoff message.

    The pre-gateway hook may annotate or rewrite transport text, but it must
    not conduct the conversation or call analytics tools on its own. This
    note exposes profile capabilities while leaving intent, follow-up handling,
    tool choice, and final user-facing output inside the Hermes agent runtime.
    """
    lines = [
        "[Hermes runtime note: This Slack message is being handled in an "
        "analytics-capable Hermes profile. Do not treat this note as "
        "user-authored text, and do not assume the user is asking an analytics "
        "question solely because this note exists. Preserve corrections, "
        "redirects, definition changes, and follow-ups from the conversation "
        "context. Call `elixir_analytics_runner` with mode `answer_question` "
        "when the user's request requires a source-backed analytics answer. "
        "You do not need `skill_view` before that first runner call; if the "
        "runner asks for model-built work, continue with the normal agentic "
        "tool loop.]",
        "",
        f"User request: {question.strip()}",
    ]

    if active_user_ambiguity:
        lines.extend(
            [
                "",
                "Runtime context: the active user definition is ambiguous. "
                "If the conversation does not already resolve it, ask the user "
                "which definition to use. Offer these options:",
                "1. Most card-active user: highest count of successful card spend transactions",
                "2. Highest card spender: highest GTV",
                "3. Most app-active user: most app events/sessions",
                "4. Combined active: app + card activity",
            ]
        )

    context = _thread_context_text(raw_text)
    if context:
        lines.extend(
            [
                "",
                "Thread context available to interpret the user request:",
                context,
            ]
        )

    return "\n".join(lines)


def _thread_context_text(raw_text: str) -> str:
    without_reply_prefix = REPLYING_PREFIX_RE.sub("", str(raw_text or ""))
    match = THREAD_CONTEXT_CAPTURE_RE.match(without_reply_prefix)
    if not match:
        return ""
    return match.group("context").strip()


def _message_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text") or part.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def _recent_session_context_text(
    *,
    session_store: Any,
    source: Any,
    limit: int = 12,
) -> str:
    if session_store is None or source is None:
        return ""

    try:
        key_builder = getattr(session_store, "_generate_session_key", None)
        session_key = key_builder(source) if callable(key_builder) else None
        if not session_key:
            return ""

        ensure_loaded = getattr(session_store, "_ensure_loaded", None)
        if callable(ensure_loaded):
            ensure_loaded()

        entries = getattr(session_store, "_entries", {})
        entry = entries.get(session_key) if isinstance(entries, dict) else None
        session_id = getattr(entry, "session_id", None)
        if not session_id:
            return ""

        loader = getattr(session_store, "load_transcript", None)
        transcript = loader(session_id) if callable(loader) else []
    except Exception:
        LOGGER.debug("failed to load session context for Slack fast path", exc_info=True)
        return ""

    if not isinstance(transcript, list):
        return ""

    user_name = str(getattr(source, "user_name", "") or "User").strip() or "User"
    lines: list[str] = []
    for message in transcript[-limit:]:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        content = re.sub(r"\s+", " ", _message_content_text(message.get("content"))).strip()
        if not content:
            continue
        if len(content) > 700:
            content = f"{content[:697].rstrip()}..."
        speaker = user_name if role == "user" else "chandler"
        lines.append(f"{speaker}: {content}")

    return "\n".join(lines)


def _contextual_raw_text(
    *,
    raw_text: str,
    question: str,
    session_store: Any,
    source: Any,
) -> str:
    if _thread_context_text(raw_text):
        return raw_text

    session_context = _recent_session_context_text(
        session_store=session_store,
        source=source,
    )
    if not session_context:
        return raw_text

    return (
        "[Thread context - prior messages in this thread (not yet in conversation history):]\n"
        f"{session_context}\n"
        "[End of thread context]\n\n"
        f"{question}"
    )


def _gateway_authorizes_event(gateway: Any, event: Any) -> bool:
    checker = getattr(gateway, "_is_user_authorized", None)
    if not callable(checker):
        return False
    try:
        return bool(checker(event.source))
    except Exception:
        LOGGER.debug("pre_gateway_dispatch auth check failed", exc_info=True)
        return False


def _profile_env() -> dict[str, str]:
    env = dict(os.environ)
    hermes_home = env.get("HERMES_HOME")
    if hermes_home:
        env_file = Path(hermes_home) / ".env"
        if env_file.is_file():
            for raw_line in env_file.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if not key or key in env:
                    continue
                value = value.strip().strip("'\"")
                env[key] = value
    env.setdefault("ANALYTICS_BASE_URL", DEFAULT_ANALYTICS_BASE_URL)
    return env


def _session_platform_identity() -> tuple[str, str, str]:
    try:
        from gateway.session_context import get_session_env

        platform = get_session_env("HERMES_SESSION_PLATFORM", "")
        user_id = get_session_env("HERMES_SESSION_USER_ID", "")
        user_name = get_session_env("HERMES_SESSION_USER_NAME", "")
    except Exception:
        platform = os.getenv("HERMES_SESSION_PLATFORM", "")
        user_id = os.getenv("HERMES_SESSION_USER_ID", "")
        user_name = os.getenv("HERMES_SESSION_USER_NAME", "")

    return (
        str(platform or "").strip().lower(),
        str(user_id or "").strip().lower(),
        str(user_name or "").strip().lower(),
    )


def _source_change_allowed_identities() -> set[str]:
    raw = os.getenv("ELIXIR_ANALYTICS_SOURCE_CHANGE_ALLOWED_USERS", "")
    configured = {
        value.strip().lower()
        for value in re.split(r"[,\n]", raw)
        if value.strip()
    }
    return DEFAULT_SOURCE_CHANGE_ALLOWED_IDENTITIES | configured


def _is_ritik_source_change_request() -> bool:
    platform, user_id, user_name = _session_platform_identity()
    if platform != "slack":
        return True

    allowed = _source_change_allowed_identities()
    return any(identity and identity in allowed for identity in (user_id, user_name))


def _permission_denied_result(mode: str) -> dict[str, Any] | None:
    if mode not in RITIK_ONLY_MODES or _is_ritik_source_change_request():
        return None

    return {
        "ok": False,
        "mode": mode,
        "errorType": "permission_denied",
        "message": RITIK_ONLY_MESSAGE,
    }


def _analytics_repo(env: dict[str, str]) -> Path:
    return Path(env.get(ANALYTICS_REPO_ENV) or DEFAULT_ANALYTICS_REPO).expanduser()


def _parse_json_stdout(stdout: str) -> Any:
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return stdout


def _bounded_tail(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]


def _request_json(args: dict[str, Any]) -> str | None:
    request = args.get("request")
    if request is None:
        return None
    if isinstance(request, str):
        return request
    return json.dumps(request, ensure_ascii=False)


def _changed_files_json(args: dict[str, Any]) -> str:
    changed_files = args.get("changed_files") or args.get("changedFiles") or []
    if isinstance(changed_files, str):
        changed_files = [
            line.strip()
            for line in changed_files.replace(",", "\n").splitlines()
            if line.strip()
        ]
    if not isinstance(changed_files, list) or not all(
        isinstance(item, str) for item in changed_files
    ):
        raise ValueError(
            "changed_files must be a list of file paths or a newline/comma separated string."
        )
    return json.dumps(changed_files, ensure_ascii=False)


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


def _merchant_card_spend_7d_query(question: str) -> str | None:
    normalized = _normalize_question(question)
    if not re.search(r"\blast\s*7\s*days?\b", normalized):
        return None
    if not re.search(r"\b(spend|spends|spent|spending|gtv)\b", normalized):
        return None
    return _extract_merchant_query(question)


def _merchant_card_spend_period_query(question: str) -> tuple[str, str] | None:
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
    normalized = _normalize_question(question)
    if not re.search(r"\bthis\s+week\b", normalized):
        return None
    if not re.search(r"\b(which\s+users?|who)\b", normalized):
        return None
    if not re.search(r"\b(spend|spends|spent|spending)\b", normalized):
        return None
    return _extract_merchant_query(question)


def _merchant_users_period_query(question: str) -> tuple[str, str] | None:
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


def _profile_answer_question_shortcut(
    args: dict[str, Any],
    *,
    mode: str,
) -> tuple[list[str], str, str, dict[str, Any]] | None:
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


def _safe_payload_summary(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"payloadType": type(payload).__name__}

    summary: dict[str, Any] = {}
    route = payload.get("route")
    shortcut = payload.get("shortcut")
    if route:
        summary["route"] = route
    if shortcut:
        summary["shortcut"] = shortcut

    nested_payload = payload.get("payload")
    row_payload = nested_payload if isinstance(nested_payload, dict) else payload

    topic_id = row_payload.get("topicId")
    result_type = row_payload.get("resultType")
    if not result_type and isinstance(row_payload.get("metadata"), dict):
        result_type = row_payload["metadata"].get("resultType")

    row_count = row_payload.get("rowCount")
    if row_count is None and isinstance(row_payload.get("rows"), list):
        row_count = len(row_payload["rows"])

    fields = {
        "topicId": topic_id,
        "kind": row_payload.get("kind"),
        "requiresClarification": row_payload.get("requiresClarification"),
        "prRequired": row_payload.get("prRequired"),
        "entriesReviewed": row_payload.get("entriesReviewed"),
        "latestQueryNumber": row_payload.get("latestQueryNumber"),
        "reviewDue": row_payload.get("reviewDue"),
        "status": row_payload.get("status"),
        "suggestionCount": (
            row_payload.get("suggestionCount")
            if row_payload.get("suggestionCount") is not None
            else len(row_payload["suggestions"])
            if isinstance(row_payload.get("suggestions"), list)
            else None
        ),
        "resultType": result_type,
        "rowCount": row_count,
        "truncated": row_payload.get("truncated"),
        "dryRun": row_payload.get("dryRun"),
        "dashboard": bool(
            row_payload.get("dashboardUrl") or row_payload.get("dashboardUrlPath")
        ),
    }
    summary.update({key: value for key, value in fields.items() if value is not None})
    return summary


def _compact_slack_text(text: str, *, limit: int = MAX_COMPACT_SLACK_TEXT_CHARS) -> str:
    lines = [
        line
        for line in text.splitlines()
        if not line.strip().lower().startswith("dashboard:")
    ]
    compact_lines: list[str] = []
    length = 0
    truncated = False
    for line in lines:
        next_length = length + len(line) + (1 if compact_lines else 0)
        if next_length > limit:
            truncated = True
            break
        compact_lines.append(line)
        length = next_length

    compact_text = "\n".join(compact_lines).strip()
    if truncated:
        compact_text = (
            f"{compact_text}\n"
            "Slack handoff truncated; use payload.dashboardUrl for the full visualization."
        ).strip()
    return compact_text or text[:limit]


def _dashboard_label_for_url(url: str) -> str:
    return "Open visualization" if "payload=" in url or "result=" in url else "Open dashboard"


def _slack_dashboard_line_from_text(text: str) -> str | None:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.lower().startswith("dashboard:"):
            continue

        value = stripped.split(":", 1)[1].strip()
        if not value:
            return None
        if value.startswith("<") and "|" in value and value.endswith(">"):
            return f"Dashboard: {value}"
        if value.startswith("http://") or value.startswith("https://"):
            return f"Dashboard: <{value}|{_dashboard_label_for_url(value)}>"
        return stripped
    return None


def _compact_direct_final_slack_text(
    text: str,
    *,
    limit: int = MAX_DIRECT_FINAL_SLACK_TEXT_CHARS,
) -> str:
    lines = [
        line
        for line in text.splitlines()
        if not line.strip().lower().startswith("dashboard:")
    ]
    compact_lines: list[str] = []
    length = 0
    truncated = False
    for line in lines:
        next_length = length + len(line) + (1 if compact_lines else 0)
        if next_length > limit:
            truncated = True
            break
        compact_lines.append(line)
        length = next_length

    compact_text = "\n".join(compact_lines).strip()
    if truncated:
        suffix = "More rows in the dashboard."
        while compact_lines:
            candidate = "\n".join(compact_lines).strip()
            suffix_length = len(suffix) + (1 if candidate else 0)
            if len(candidate) + suffix_length <= limit:
                compact_text = candidate
                break
            compact_lines.pop()
        else:
            compact_text = ""
        compact_text = f"{compact_text}\n{suffix}".strip()
    return compact_text or text[:limit].strip()


def _compact_answer_question_payload(payload: Any) -> Any:
    """Keep Slack answer handoff compact without hiding route evidence."""
    if not isinstance(payload, dict):
        return payload

    nested_payload = payload.get("payload")
    answer_payload = nested_payload if isinstance(nested_payload, dict) else payload
    metadata = answer_payload.get("metadata")
    safe_metadata_keys = {
        "interpretedDefinition",
        "metricIds",
        "resultType",
        "sources",
        "dateWindow",
        "timezone",
        "assumptions",
        "caveats",
        "freshness",
        "resultSummary",
        "friction",
        "conventionAdded",
        "repeatPromoteCandidate",
    }

    compact: dict[str, Any] = {}
    for key in ("ok", "route", "shortcut", "error", "errorType", "message"):
        if key in payload:
            compact[key] = payload[key]

    compact_payload: dict[str, Any] = {}
    for key in (
        "ok",
        "topicId",
        "title",
        "kind",
        "resultType",
        "requiresClarification",
        "clarificationQuestion",
        "choices",
        "rowCount",
        "truncated",
        "dryRun",
        "dashboardUrl",
        "dateWindow",
    ):
        if key in answer_payload:
            compact_payload[key] = answer_payload[key]

    if "dashboardUrl" not in compact_payload and "dashboardUrlPath" in answer_payload:
        compact_payload["dashboardUrlPath"] = answer_payload["dashboardUrlPath"]

    slack_text = answer_payload.get("slackText")
    if isinstance(slack_text, str) and slack_text.strip():
        slack_dashboard_line = _slack_dashboard_line_from_text(slack_text)
        compact_payload["slackText"] = _compact_slack_text(slack_text)
        if slack_dashboard_line:
            compact_payload["slackDashboardLine"] = slack_dashboard_line

    if isinstance(metadata, dict):
        safe_metadata = {
            key: value
            for key, value in metadata.items()
            if key in safe_metadata_keys and value is not None
        }
        if safe_metadata:
            compact_payload["metadata"] = safe_metadata

    if compact_payload:
        compact["payload"] = compact_payload

    return compact


def _direct_dashboard_link(answer_payload: dict[str, Any]) -> str | None:
    dashboard_url = answer_payload.get("dashboardUrl")
    if isinstance(dashboard_url, str) and dashboard_url.strip():
        return dashboard_url.strip()

    dashboard_path = answer_payload.get("dashboardUrlPath")
    if isinstance(dashboard_path, str) and dashboard_path.strip():
        if dashboard_path.startswith("http://") or dashboard_path.startswith("https://"):
            return dashboard_path.strip()
        return f"{DEFAULT_ANALYTICS_BASE_URL}{dashboard_path}"
    return None


def _slack_dashboard_link(answer_payload: dict[str, Any]) -> str | None:
    dashboard_link = _direct_dashboard_link(answer_payload)
    if not dashboard_link:
        return None

    label = (
        "Open visualization"
        if "payload=" in dashboard_link or "result=" in dashboard_link
        else "Open dashboard"
    )
    return f"<{dashboard_link}|{label}>"


def _direct_final_response_for_answer_payload(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None

    nested_payload = payload.get("payload")
    answer_payload = nested_payload if isinstance(nested_payload, dict) else payload
    slack_text = answer_payload.get("slackText")
    if not isinstance(slack_text, str) or not slack_text.strip():
        return None

    final_text = _compact_direct_final_slack_text(slack_text)
    dashboard_line = answer_payload.get("slackDashboardLine")
    if not isinstance(dashboard_line, str) or not dashboard_line.strip():
        dashboard_line = _slack_dashboard_line_from_text(slack_text)
    if dashboard_line and "dashboard:" not in final_text.lower():
        final_text = f"{final_text}\n\n{dashboard_line.strip()}"
    return final_text


def _clarify_instruction_for_answer_payload(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None

    nested_payload = payload.get("payload")
    answer_payload = nested_payload if isinstance(nested_payload, dict) else payload
    clarification = answer_payload.get("clarificationQuestion")
    if not isinstance(clarification, str) or not clarification.strip():
        return None

    return (
        "Call `clarify` with `payload.payload.clarificationQuestion` before "
        "answering or querying. Do not send the clarification as a final "
        "assistant response."
    )


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
        fields.update(_safe_payload_summary(result.get("payload")))
        LOGGER.info(
            "analytics runner completed %s",
            " ".join(f"{key}={value}" for key, value in fields.items() if value is not None),
        )
    else:
        LOGGER.warning(
            "analytics runner failed %s",
            " ".join(f"{key}={value}" for key, value in fields.items() if value is not None),
        )


def _tool_args_text(args: dict[str, Any]) -> str:
    values: list[str] = []
    for value in (args or {}).values():
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, (list, tuple, dict)):
            try:
                values.append(json.dumps(value, ensure_ascii=False))
            except Exception:
                values.append(str(value))
        elif value is not None:
            values.append(str(value))
    return "\n".join(values)


def _is_source_control_tool_call(tool_name: str, args: dict[str, Any]) -> bool:
    if str(tool_name or "").strip() not in SOURCE_CONTROL_TOOL_NAMES:
        return False

    text = _tool_args_text(args)
    normalized = re.sub(r"['\"`\[\](),]+", " ", text)
    return any(
        pattern.search(candidate)
        for candidate in (text, normalized)
        for pattern in SOURCE_CONTROL_PATTERNS
    )


def _block_non_ritik_source_control_tool(
    *,
    tool_name: str,
    args: dict[str, Any],
    **_: Any,
) -> dict[str, str] | None:
    if _is_ritik_source_change_request():
        return None
    if not _is_source_control_tool_call(tool_name, args):
        return None
    return {"action": "block", "message": RITIK_ONLY_MESSAGE}


def _pre_gateway_elixir_analytics_agent_handoff(
    *,
    event: Any,
    gateway: Any,
    session_store: Any = None,
    **_: Any,
) -> dict[str, str] | None:
    source = getattr(event, "source", None)
    if _platform_value(getattr(source, "platform", "")) != "slack":
        return None
    if getattr(source, "is_bot", False):
        return {"action": "allow"}
    if not _gateway_authorizes_event(gateway, event):
        return {"action": "allow"}

    raw_text = str(getattr(event, "text", "") or "")
    question = _clean_slack_question_text(raw_text)
    if not _is_plain_slack_analytics_question(question):
        return {"action": "allow"}

    if _is_destructive_analytics_mutation(question):
        return {
            "action": "respond",
            "text": READ_ONLY_GUARD_MESSAGE,
            "response_type": "guardrail",
            "reason": "elixir_analytics_read_only_guard",
        }

    context_raw_text = _contextual_raw_text(
        raw_text=raw_text,
        question=question,
        session_store=session_store,
        source=source,
    )

    if _is_ambiguous_active_users_question(question):
        return {
            "action": "annotate",
            "text": question,
            "text_type": "transport_normalization",
            "context": _agent_runtime_handoff_text(
                question=question,
                raw_text=context_raw_text,
                active_user_ambiguity=True,
            ),
            "reason": "elixir_analytics_agent_runtime_handoff",
        }

    return {
        "action": "annotate",
        "text": question,
        "text_type": "transport_normalization",
        "context": _agent_runtime_handoff_text(
            question=question,
            raw_text=context_raw_text,
        ),
        "reason": "elixir_analytics_agent_runtime_handoff",
    }


def _runner_command(args: dict[str, Any]) -> tuple[list[str], str | None]:
    mode = str(args.get("mode") or "").strip()
    dry_run = _coerce_bool(args.get("dry_run"))
    max_rows = _coerce_int(args.get("max_rows"), DEFAULT_MAX_ROWS, minimum=1, maximum=MAX_ROWS)

    if not mode and str(args.get("question") or "").strip():
        mode = "answer_question"

    if mode == "plan":
        question = str(args.get("question") or "").strip()
        if not question:
            raise ValueError("mode='plan' requires question.")
        return [
            "node",
            "--import",
            "tsx",
            "scripts/plan-analytics-question.ts",
        ], question

    if mode == "answer_question":
        question = str(args.get("question") or "").strip()
        if not question:
            raise ValueError("mode='answer_question' requires question.")
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
            "scripts/run-analytics-question.ts",
            "--max-rows",
            str(max_rows),
        ]
        if dry_run:
            command.append("--dry-run")
        return command, question

    if mode == "saved_topic":
        topic_id = str(args.get("topic_id") or args.get("topicId") or "").strip()
        if not topic_id:
            raise ValueError("mode='saved_topic' requires topic_id.")
        range_key = str(args.get("range") or "30d").strip()
        command = [
            "node",
            "--import",
            "tsx",
            "scripts/run-saved-query-topic.ts",
            topic_id,
            "--range",
            range_key,
        ]
        if dry_run:
            command.append("--dry-run")
        return command, None

    if mode == "supabase_ad_hoc":
        request_json = _request_json(args)
        if not request_json:
            raise ValueError("mode='supabase_ad_hoc' requires request JSON.")
        command = [
            "node",
            "--import",
            "tsx",
            "scripts/run-ad-hoc-query.ts",
            "--max-rows",
            str(max_rows),
        ]
        if dry_run:
            command.append("--dry-run")
        return command, request_json

    if mode == "posthog_ad_hoc":
        request_json = _request_json(args)
        if not request_json:
            raise ValueError("mode='posthog_ad_hoc' requires request JSON.")
        command = [
            "node",
            "--import",
            "tsx",
            "scripts/run-posthog-query.ts",
            "--max-rows",
            str(max_rows),
        ]
        if dry_run:
            command.append("--dry-run")
        return command, request_json

    if mode == "source_change_plan":
        request = str(args.get("request") or args.get("question") or "").strip()
        if not request:
            raise ValueError("mode='source_change_plan' requires request.")
        return [
            "node",
            "--import",
            "tsx",
            "scripts/plan-source-change.ts",
        ], request

    if mode == "source_change_scope_check":
        request = str(args.get("request") or args.get("question") or "").strip()
        if not request:
            raise ValueError("mode='source_change_scope_check' requires request.")
        command = [
            "node",
            "--import",
            "tsx",
            "scripts/check-source-change-scope.ts",
            "--changed-files-json",
            _changed_files_json(args),
        ]
        if _coerce_bool(args.get("allow_unexpected_files") or args.get("allowUnexpectedFiles")):
            command.append("--allow-unexpected-files")
        return command, request

    if mode in {"self_improvement_check", "self_improvement_plan"}:
        query_log = str(args.get("query_log") or args.get("queryLog") or "QUERY_LOG.md").strip()
        if not query_log:
            raise ValueError(f"mode='{mode}' requires query_log.")
        script = (
            "scripts/check-self-improvement-cadence.ts"
            if mode == "self_improvement_check"
            else "scripts/plan-self-improvement.ts"
        )
        return [
            "node",
            "--import",
            "tsx",
            script,
            "--query-log",
            query_log,
        ], None

    raise ValueError(
        "mode must be one of plan, answer_question, saved_topic, supabase_ad_hoc, posthog_ad_hoc, source_change_plan, source_change_scope_check, self_improvement_check, self_improvement_plan."
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
    if mode == "answer_question":
        direct_final_response = _direct_final_response_for_answer_payload(payload)
        if direct_final_response:
            result["hermes_direct_final_response"] = direct_final_response
        clarify_instruction = _clarify_instruction_for_answer_payload(payload)
        if clarify_instruction:
            result["hermes_agent_instruction"] = clarify_instruction
    _log_runner_result(result)
    return result


def _handler(args: dict[str, Any], **_: Any) -> str:
    return _json_dumps(run_elixir_analytics_runner(args))


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _block_non_ritik_source_control_tool)
    ctx.register_hook("transform_tool_result", _transform_tool_result)
    ctx.register_hook("pre_gateway_dispatch", _pre_gateway_elixir_analytics_agent_handoff)
    ctx.register_tool(
        name="elixir_analytics_runner",
        toolset=TOOLSET,
        schema={
            "name": "elixir_analytics_runner",
            "description": (
                "Run Elixir analytics common-question shortcuts, planner, "
                "saved-topic, Supabase ad hoc, PostHog ad hoc, source-change, "
                "or self-improvement runners without shell/code-execution setup. "
                "Use mode='answer_question' first for plain Slack analytics "
                "questions. If a completed answer_question payload includes "
                "payload.slackText, use it as the Slack-facing final answer. "
                "For ambiguous business terms, use `clarify` so the next Slack "
                "reply is captured inside the same agent run."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {
                        "description": (
                            "Runner mode. For plain Slack analytics questions, "
                            "use answer_question first with the exact raw question."
                        ),
                        "type": "string",
                        "enum": [
                            "plan",
                            "answer_question",
                            "saved_topic",
                            "supabase_ad_hoc",
                            "posthog_ad_hoc",
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
