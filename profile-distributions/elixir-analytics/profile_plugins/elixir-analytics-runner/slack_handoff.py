"""Slack pre-gateway handoff helpers for the Elixir analytics profile."""

from __future__ import annotations

import json
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
    if re.search(
        r"\b(how\s+many|count|number\s+of|percent(?:age)?|share)\b|%",
        normalized,
    ):
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
    answer_artifact_context: str = "",
) -> str:
    """Build an agent-owned Slack profile handoff message."""
    lines = [
        "[Hermes runtime note: This Slack message is being handled in an "
        "analytics-capable Hermes profile. Do not treat this note as "
        "user-authored text, and do not assume the user is asking an analytics "
        "question solely because this note exists. Preserve corrections, "
        "redirects, definition changes, and follow-ups from the conversation "
        "context. Call `analytics_answer` when the user's request requires a "
        "source-backed analytics answer. "
        "For plain top-spender or top-spenders questions, default spend source "
        "to successful card spend by user ranked by gross GTV; do not clarify "
        "that spend source before calling `analytics_answer`. "
        "If the request explicitly says app active users, app events, or app "
        "sessions, the active-user definition is already resolved; do not "
        "clarify it before calling `analytics_answer`. "
        "If the request asks how many active users have a wearable, use the "
        "active-users-with-wearable default: combined active users over the "
        "rolling last 30 days with wearable evidence from the governed "
        "Answer Contract; do not clarify before calling `analytics_answer`. "
        "For artifact-backed follow-ups such as 'these 5 users', resolve the "
        "phrase from Recent analytics AnswerArtifacts and pass a resolved "
        "question with the relevant handle or IDs to `analytics_answer`; do "
        "not repeat the previous answer. "
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
                "1. Card-active users: users with successful card spend transactions",
                "2. Card spenders by GTV: users ranked by successful card spend amount",
                "3. App-active users: users with app events or sessions",
                "4. Combined active users: users with app activity or card activity",
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

    if answer_artifact_context.strip():
        lines.extend(
            [
                "",
                answer_artifact_context.strip(),
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


def _recent_session_transcript(
    *,
    session_store: Any,
    source: Any,
) -> list[Any]:
    if session_store is None or source is None:
        return []

    try:
        key_builder = getattr(session_store, "_generate_session_key", None)
        session_key = key_builder(source) if callable(key_builder) else None
        if not session_key:
            return []

        ensure_loaded = getattr(session_store, "_ensure_loaded", None)
        if callable(ensure_loaded):
            ensure_loaded()

        entries = getattr(session_store, "_entries", {})
        entry = entries.get(session_key) if isinstance(entries, dict) else None
        session_id = getattr(entry, "session_id", None)
        if not session_id:
            return []

        loader = getattr(session_store, "load_transcript", None)
        transcript = loader(session_id) if callable(loader) else []
    except Exception:
        LOGGER.debug("failed to load session transcript for Slack handoff", exc_info=True)
        return []

    return transcript if isinstance(transcript, list) else []


def _answer_artifacts_from_value(value: Any) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if value.get("version") == "answer-artifact.v1":
            artifacts.append(value)
        for nested in value.values():
            artifacts.extend(_answer_artifacts_from_value(nested))
    elif isinstance(value, list):
        for nested in value:
            artifacts.extend(_answer_artifacts_from_value(nested))
    return artifacts


def _answer_artifacts_from_message(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = _message_content_text(message.get("content"))
    if not content.strip():
        return []
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return []
    return _answer_artifacts_from_value(payload)


def _artifact_entity_summary(artifact: dict[str, Any]) -> str:
    entities = artifact.get("entities")
    if not isinstance(entities, dict):
        return ""

    handles = artifact.get("followUpHandles")
    ordered_handles = [
        handle
        for handle in handles
        if isinstance(handle, str) and isinstance(entities.get(handle), dict)
    ] if isinstance(handles, list) else []
    for handle in entities:
        if isinstance(handle, str) and handle not in ordered_handles:
            ordered_handles.append(handle)

    chunks: list[str] = []
    for handle in ordered_handles[:5]:
        entity = entities.get(handle)
        if not isinstance(entity, dict):
            continue
        ids = entity.get("ids")
        id_values = [str(item) for item in ids[:10]] if isinstance(ids, list) else []
        id_suffix = ", ".join(id_values)
        if isinstance(ids, list) and len(ids) > len(id_values):
            id_suffix = f"{id_suffix}, ..." if id_suffix else "..."
        chunks.append(f"{handle}: ids=[{id_suffix}]")
    return "; ".join(chunks)


def _recent_answer_artifact_context_text(
    *,
    session_store: Any,
    source: Any,
    limit: int = 3,
) -> str:
    transcript = _recent_session_transcript(
        session_store=session_store,
        source=source,
    )
    if not transcript:
        return ""

    artifacts: list[dict[str, Any]] = []
    for message in reversed(transcript):
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "").strip().lower() != "tool":
            continue
        artifacts.extend(_answer_artifacts_from_message(message))
        if len(artifacts) >= limit:
            break

    if not artifacts:
        return ""

    lines = ["Recent analytics AnswerArtifacts:"]
    for artifact in artifacts[:limit]:
        artifact_id = str(artifact.get("artifactId") or "unknown")
        question = re.sub(r"\s+", " ", str(artifact.get("userQuestion") or "")).strip()
        intent = artifact.get("intent")
        kind = intent.get("kind") if isinstance(intent, dict) else None
        plan = artifact.get("plan")
        time_window = plan.get("timeWindow") if isinstance(plan, dict) else None
        entity_summary = _artifact_entity_summary(artifact)
        parts = [f"artifactId={artifact_id}"]
        if question:
            parts.append(f'question="{question[:180]}"')
        if kind:
            parts.append(f"intent={kind}")
        if time_window:
            parts.append(f"timeWindow={time_window}")
        if entity_summary:
            parts.append(f"entities: {entity_summary}")
        lines.append(f"- {'; '.join(parts)}")
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
    answer_artifact_context = _recent_answer_artifact_context_text(
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
                answer_artifact_context=answer_artifact_context,
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
            answer_artifact_context=answer_artifact_context,
        ),
        "reason": "elixir_analytics_agent_runtime_handoff",
    }
