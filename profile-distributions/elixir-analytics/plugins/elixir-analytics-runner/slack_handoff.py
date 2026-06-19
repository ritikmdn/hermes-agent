"""Slack pre-gateway handoff helpers for the Elixir analytics profile."""

from __future__ import annotations

import logging
import re
from typing import Any


LOGGER = logging.getLogger("hermes.elixir_analytics_runner")

SOURCE_CONTROL_PATTERNS = (
    re.compile(
        r"\bgit\s+(add|commit|push|merge|rebase|reset|switch|checkout|tag)\b",
        re.I,
    ),
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


def _normalize_question(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


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
    """Build an agent-owned Slack profile handoff message."""
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
        content = re.sub(
            r"\s+",
            " ",
            _message_content_text(message.get("content")),
        ).strip()
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


def pre_gateway_elixir_analytics_agent_handoff(
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
