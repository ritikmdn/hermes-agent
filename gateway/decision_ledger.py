"""Per-turn gateway decision ledger.

The gateway may normalize transport wrappers, enforce guardrails, and attach
agent-only runtime context before a user turn reaches the agent. This module
keeps those infrastructure decisions explicit and auditable so hook code cannot
quietly become an untracked conversational bot.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

try:
    from agent.redact import redact_sensitive_text
except Exception:  # pragma: no cover - import cycle/early startup fallback
    def redact_sensitive_text(text: str, *, force: bool = False, code_file: bool = False) -> str:
        return text


_TEXT_KEYS = {
    "content",
    "context",
    "message",
    "raw_text",
    "runtime_context",
    "text",
}


def _bounded_text(value: Any, *, max_chars: int) -> Any:
    if not isinstance(value, str):
        return value
    text = redact_sensitive_text(value, force=True)
    if len(text) <= max_chars:
        return text
    suffix = f"... [truncated {len(text) - max_chars} chars]"
    keep = max(0, max_chars - len(suffix))
    return text[:keep].rstrip() + suffix


def _bounded_payload(value: Any, *, max_text_chars: int) -> Any:
    if isinstance(value, dict):
        bounded: dict[str, Any] = {}
        for key, child in value.items():
            if key in _TEXT_KEYS:
                bounded[key] = _bounded_text(child, max_chars=max_text_chars)
            elif isinstance(child, (dict, list, tuple)):
                bounded[key] = _bounded_payload(child, max_text_chars=max_text_chars)
            elif isinstance(child, str):
                bounded[key] = _bounded_text(child, max_chars=max_text_chars)
            else:
                bounded[key] = child
        return bounded
    if isinstance(value, (list, tuple)):
        return [_bounded_payload(child, max_text_chars=max_text_chars) for child in value]
    if isinstance(value, str):
        return _bounded_text(value, max_chars=max_text_chars)
    return value


@dataclass
class GatewayDecisionLedger:
    platform: str
    chat_id: str
    user_id: str | None = None
    message_id: str | None = None
    original_text: str = ""
    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)
    runtime_fingerprint: dict[str, Any] | None = None
    max_text_chars: int = 512
    decisions: list[dict[str, Any]] = field(default_factory=list)
    final_action: str = "continue"
    final_reason: str = "not_finalized"
    session_id: str | None = None
    persisted: bool = field(default=False, repr=False, compare=False)

    def record_hook_result(
        self,
        actor: str,
        result: dict[str, Any],
        *,
        applied: bool | None = None,
        blocked_reason: str | None = None,
    ) -> None:
        action = str(result.get("action") or "allow").strip().lower() if isinstance(result, dict) else "unknown"
        entry = {
            "kind": "hook_result",
            "actor": actor,
            "capability": action,
            "reason": result.get("reason") if isinstance(result, dict) else None,
            "applied": applied,
            "blocked_reason": blocked_reason,
            "payload": _bounded_payload(result, max_text_chars=self.max_text_chars)
            if isinstance(result, dict)
            else _bounded_payload({"value": result}, max_text_chars=self.max_text_chars),
            "timestamp": time.time(),
        }
        self.decisions.append(entry)

    def record_gateway_decision(
        self,
        capability: str,
        *,
        reason: str,
        applied: bool = True,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.decisions.append(
            {
                "kind": "gateway_decision",
                "actor": "gateway",
                "capability": capability,
                "reason": reason,
                "applied": applied,
                "payload": _bounded_payload(details or {}, max_text_chars=self.max_text_chars),
                "timestamp": time.time(),
            }
        )

    def set_final_action(
        self,
        action: str,
        *,
        reason: str,
        session_id: str | None = None,
    ) -> None:
        self.final_action = action
        self.final_reason = reason
        if session_id:
            self.session_id = session_id

    def summary(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "created_at": self.created_at,
            "platform": self.platform,
            "chat_id": self.chat_id,
            "user_id": self.user_id,
            "message_id": self.message_id,
            "session_id": self.session_id,
            "original_text_preview": _bounded_text(
                self.original_text,
                max_chars=min(self.max_text_chars, 96),
            ),
            "final_action": self.final_action,
            "final_reason": self.final_reason,
            "decision_count": len(self.decisions),
            "decisions": list(self.decisions),
            "runtime_fingerprint": _bounded_payload(
                self.runtime_fingerprint or {},
                max_text_chars=self.max_text_chars,
            ),
        }
