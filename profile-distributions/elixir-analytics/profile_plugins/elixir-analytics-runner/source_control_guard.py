"""Source-control permission guard for Chandler's analytics profile."""

from __future__ import annotations

import json
import os
import re
from typing import Any


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
RITIK_ONLY_MESSAGE = "Elixir analytics source-control actions are Ritik-only in Slack."


def session_platform_identity() -> tuple[str, str, str]:
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


def source_change_allowed_identities() -> set[str]:
    raw = os.getenv("ELIXIR_ANALYTICS_SOURCE_CHANGE_ALLOWED_USERS", "")
    configured = {
        value.strip().lower()
        for value in re.split(r"[,\n]", raw)
        if value.strip()
    }
    return DEFAULT_SOURCE_CHANGE_ALLOWED_IDENTITIES | configured


def is_ritik_source_change_request() -> bool:
    platform, user_id, user_name = session_platform_identity()
    if platform != "slack":
        return True

    allowed = source_change_allowed_identities()
    return any(identity and identity in allowed for identity in (user_id, user_name))


def permission_denied_result(mode: str) -> dict[str, Any] | None:
    if mode not in RITIK_ONLY_MODES or is_ritik_source_change_request():
        return None

    return {
        "ok": False,
        "mode": mode,
        "errorType": "permission_denied",
        "message": RITIK_ONLY_MESSAGE,
    }


def tool_args_text(args: dict[str, Any]) -> str:
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


def is_source_control_tool_call(tool_name: str, args: dict[str, Any]) -> bool:
    if str(tool_name or "").strip() not in SOURCE_CONTROL_TOOL_NAMES:
        return False

    text = tool_args_text(args)
    normalized = re.sub(r"['\"`\[\](),]+", " ", text)
    return any(
        pattern.search(candidate)
        for candidate in (text, normalized)
        for pattern in SOURCE_CONTROL_PATTERNS
    )


def block_non_ritik_source_control_tool(
    *,
    tool_name: str,
    args: dict[str, Any],
    **_: Any,
) -> dict[str, str] | None:
    if is_ritik_source_change_request():
        return None
    if not is_source_control_tool_call(tool_name, args):
        return None
    return {"action": "block", "message": RITIK_ONLY_MESSAGE}
