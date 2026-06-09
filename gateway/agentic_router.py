"""Accuracy-first routing primitives for gateway turns.

The router is intentionally small and conservative. It routes infrastructure
outcomes (drop, guardrail response, transport normalization, advisory context)
while leaving semantic interpretation and final wording to the agent runtime.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


TEXT_MUTATION_KINDS = frozenset({"transport_normalization", "transport", "sanitization"})
INFRASTRUCTURE_RESPONSE_TYPES = frozenset({"guardrail", "system_notice", "auth"})
_RUNTIME_CONTEXT_MAX_CHARS = 12_000
_RUNTIME_CONTEXT_TRUNCATION_SUFFIX = "\n[Gateway runtime context truncated.]"


def hook_allows_text_mutation(result: dict[str, Any]) -> bool:
    kind = str(
        result.get("rewrite_type")
        or result.get("text_type")
        or result.get("kind")
        or ""
    ).strip().lower()
    return kind in TEXT_MUTATION_KINDS


@dataclass
class HookRouteDecision:
    action: str
    actor: str = "pre_gateway_dispatch"
    reason: str | None = None
    applied: bool | None = None
    blocked_reason: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class RouteDecision:
    route: str = "agent"
    final_reason: str = "default"
    response_text: str | None = None
    response_type: str | None = None
    metadata: dict[str, Any] | None = None
    rewritten_text: str | None = None
    runtime_context: str = ""
    hook_decisions: list[HookRouteDecision] = field(default_factory=list)

    def to_ledger_payload(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "final_reason": self.final_reason,
            "response_type": self.response_type,
            "has_runtime_context": bool(self.runtime_context.strip()),
            "has_rewritten_text": self.rewritten_text is not None,
        }


@dataclass
class RouterAdvisory:
    intent: str = "unknown"
    confidence: float = 0.0
    advisory_context: str = ""
    rationale: str = ""

    def to_runtime_context(self) -> str:
        lines = [
            "[Advisory router context - not user-authored and not a final answer]",
            f"intent: {self.intent or 'unknown'}",
            f"confidence: {self.confidence:.2f}",
        ]
        if self.rationale.strip():
            lines.append(f"rationale: {self.rationale.strip()}")
        if self.advisory_context.strip():
            lines.append(f"context: {self.advisory_context.strip()}")
        return "\n".join(lines)


def parse_router_advisory(content: str) -> RouterAdvisory:
    try:
        payload = json.loads(str(content or "").strip())
    except (TypeError, json.JSONDecodeError):
        return RouterAdvisory(advisory_context=str(content or "").strip()[:1000])
    if not isinstance(payload, dict):
        return RouterAdvisory()

    confidence_raw = payload.get("confidence", 0.0)
    try:
        confidence = max(0.0, min(1.0, float(confidence_raw)))
    except (TypeError, ValueError):
        confidence = 0.0

    return RouterAdvisory(
        intent=str(payload.get("intent") or "unknown").strip()[:120],
        confidence=confidence,
        advisory_context=str(payload.get("advisory_context") or payload.get("context") or "").strip()[:1200],
        rationale=str(payload.get("rationale") or "").strip()[:600],
    )


def _cfg_get(config: dict[str, Any], *path: str, default: Any = None) -> Any:
    cur: Any = config or {}
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return default if cur is None else cur


async def maybe_build_middle_model_advisory(
    user_text: str,
    *,
    config: dict[str, Any] | None = None,
    async_call_llm_fn: Any = None,
) -> str:
    """Return optional advisory context from a fast model.

    The advisory model is not a planner and cannot answer the user. Its output
    is reduced to bounded runtime context for the main agent.
    """
    middle_cfg = _cfg_get(config or {}, "gateway", "agentic_router", "middle_model", default={})
    if not isinstance(middle_cfg, dict) or not bool(middle_cfg.get("enabled", False)):
        return ""

    if async_call_llm_fn is None:
        from agent.auxiliary_client import async_call_llm as async_call_llm_fn

    system = (
        "Classify this gateway message for routing context only. "
        "Return compact JSON with keys: intent, confidence, rationale, advisory_context. "
        "Do not answer the user. Do not include a final_response or answer field."
    )
    response = await async_call_llm_fn(
        task="agentic_router",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": str(user_text or "")[:4000]},
        ],
        temperature=0,
        max_tokens=int(middle_cfg.get("max_tokens") or 256),
        timeout=float(middle_cfg.get("timeout") or 15),
    )
    content = ""
    try:
        content = response.choices[0].message.content or ""
    except Exception:
        content = str(response or "")
    advisory = parse_router_advisory(content)
    if not advisory.advisory_context and not advisory.rationale:
        return ""
    return advisory.to_runtime_context()


def _actor_for(result: dict[str, Any]) -> str:
    return str(result.get("plugin") or result.get("actor") or "pre_gateway_dispatch")


def _hook_decision(
    result: dict[str, Any],
    *,
    applied: bool | None,
    blocked_reason: str | None = None,
) -> HookRouteDecision:
    return HookRouteDecision(
        action=str(result.get("action") or "allow").strip().lower(),
        actor=_actor_for(result),
        reason=result.get("reason"),
        applied=applied,
        blocked_reason=blocked_reason,
        payload=dict(result),
    )


def _append_runtime_context(existing: str, addition: str) -> str:
    addition = str(addition or "").strip()
    if not addition:
        return existing
    combined = f"{existing}\n\n{addition}" if existing else addition
    if len(combined) <= _RUNTIME_CONTEXT_MAX_CHARS:
        return combined
    keep = _RUNTIME_CONTEXT_MAX_CHARS - len(_RUNTIME_CONTEXT_TRUNCATION_SUFFIX)
    if keep <= 0:
        return _RUNTIME_CONTEXT_TRUNCATION_SUFFIX.strip()
    return combined[:keep].rstrip() + _RUNTIME_CONTEXT_TRUNCATION_SUFFIX


def route_pre_gateway_hooks(hook_results: list[Any]) -> RouteDecision:
    """Normalize pre-gateway hook results into a typed route decision."""
    decision = RouteDecision()
    for result in hook_results or []:
        if not isinstance(result, dict):
            continue

        action = str(result.get("action") or "allow").strip().lower()

        if action == "skip":
            decision.route = "skip"
            decision.final_reason = str(result.get("reason") or "hook_skip")
            decision.hook_decisions.append(_hook_decision(result, applied=True))
            return decision

        if action == "rewrite":
            new_text = result.get("text")
            if isinstance(new_text, str) and hook_allows_text_mutation(result):
                decision.rewritten_text = new_text
                decision.hook_decisions.append(_hook_decision(result, applied=True))
            elif isinstance(new_text, str):
                decision.hook_decisions.append(
                    _hook_decision(
                        result,
                        applied=False,
                        blocked_reason="untyped_text_mutation",
                    )
                )
            continue

        if action == "annotate":
            applied = False
            blocked_reason = None
            new_text = result.get("text")
            if isinstance(new_text, str):
                if hook_allows_text_mutation(result):
                    decision.rewritten_text = new_text
                    applied = True
                else:
                    blocked_reason = "untyped_text_mutation"
            context = result.get("context") or result.get("runtime_context")
            if isinstance(context, str) and context.strip():
                decision.runtime_context = _append_runtime_context(
                    decision.runtime_context,
                    context,
                )
                applied = True
            decision.hook_decisions.append(
                _hook_decision(result, applied=applied, blocked_reason=blocked_reason)
            )
            continue

        if action == "respond":
            response_type = str(result.get("response_type") or result.get("kind") or "").strip().lower()
            if response_type not in INFRASTRUCTURE_RESPONSE_TYPES:
                decision.hook_decisions.append(
                    _hook_decision(
                        result,
                        applied=False,
                        blocked_reason="conversational_respond_not_allowed",
                    )
                )
                continue
            text = result.get("text") or result.get("content")
            if not isinstance(text, str) or not text.strip():
                decision.hook_decisions.append(
                    _hook_decision(result, applied=False, blocked_reason="empty_response")
                )
                continue
            decision.route = "respond"
            decision.final_reason = str(result.get("reason") or response_type or "hook_respond")
            decision.response_text = text.strip()
            decision.response_type = response_type
            metadata = result.get("metadata")
            decision.metadata = metadata if isinstance(metadata, dict) else None
            decision.hook_decisions.append(_hook_decision(result, applied=True))
            return decision

        if action == "allow":
            decision.hook_decisions.append(_hook_decision(result, applied=True))
            continue

    return decision
