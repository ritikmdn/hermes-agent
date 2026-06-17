import pytest

from gateway.agentic_router import (
    RouteDecision,
    maybe_build_middle_model_advisory,
    parse_router_advisory,
    route_pre_gateway_hooks,
)


def test_router_blocks_conversational_hook_responds_without_answering():
    decision = route_pre_gateway_hooks(
        [
            {
                "action": "respond",
                "text": "Which option should I use?",
                "reason": "legacy_fast_path",
            }
        ]
    )

    assert decision.route == "agent"
    assert decision.response_text is None
    assert decision.hook_decisions[0].applied is False
    assert decision.hook_decisions[0].blocked_reason == "conversational_respond_not_allowed"


def test_router_allows_guardrail_responds_only_as_infrastructure():
    decision = route_pre_gateway_hooks(
        [
            {
                "action": "respond",
                "text": "Read-only mode.",
                "response_type": "guardrail",
                "reason": "read_only_guard",
            }
        ]
    )

    assert decision.route == "respond"
    assert decision.response_text == "Read-only mode."
    assert decision.final_reason == "read_only_guard"
    assert decision.hook_decisions[0].applied is True


def test_router_treats_annotations_as_agent_context_not_answers():
    decision = route_pre_gateway_hooks(
        [
            {
                "action": "annotate",
                "text": "what changed most last week",
                "text_type": "transport_normalization",
                "context": "Analytics profile is available; preserve definition changes.",
                "reason": "analytics_handoff",
            }
        ]
    )

    assert decision.route == "agent"
    assert decision.rewritten_text == "what changed most last week"
    assert decision.runtime_context == "Analytics profile is available; preserve definition changes."
    assert decision.response_text is None


def test_router_accumulates_non_terminal_hook_decisions():
    decision = route_pre_gateway_hooks(
        [
            {"action": "allow", "reason": "first_hook_noop", "plugin": "noop"},
            {
                "action": "annotate",
                "context": "First runtime hint.",
                "reason": "first_annotation",
                "plugin": "analytics",
            },
            {
                "action": "annotate",
                "context": "Second runtime hint.",
                "reason": "second_annotation",
                "plugin": "memory",
            },
        ]
    )

    assert decision.route == "agent"
    assert decision.response_text is None
    assert "First runtime hint." in decision.runtime_context
    assert "Second runtime hint." in decision.runtime_context
    assert [hook.actor for hook in decision.hook_decisions] == [
        "noop",
        "analytics",
        "memory",
    ]


def test_router_later_guardrail_can_stop_after_annotation():
    decision = route_pre_gateway_hooks(
        [
            {
                "action": "annotate",
                "context": "This should be ledgered before the guardrail.",
                "reason": "profile_context",
                "plugin": "profile",
            },
            {
                "action": "respond",
                "text": "This workspace is read-only.",
                "response_type": "guardrail",
                "reason": "read_only_guard",
                "plugin": "guard",
            },
        ]
    )

    assert decision.route == "respond"
    assert decision.response_text == "This workspace is read-only."
    assert "ledgered before the guardrail" in decision.runtime_context
    assert [hook.actor for hook in decision.hook_decisions] == ["profile", "guard"]


def test_router_bounds_accumulated_runtime_context():
    decision = route_pre_gateway_hooks(
        [
            {
                "action": "annotate",
                "context": "x" * 20_000,
                "reason": "large_context",
            }
        ]
    )

    assert len(decision.runtime_context) <= 12_000
    assert "Gateway runtime context truncated" in decision.runtime_context


def test_router_preserves_redirects_and_definition_changes_for_agent():
    decision = route_pre_gateway_hooks(
        [
            {
                "action": "annotate",
                "text": "make that app-active instead",
                "text_type": "transport_normalization",
                "context": "The user may be redirecting the prior definition.",
                "reason": "analytics_handoff",
            }
        ]
    )

    assert decision.route == "agent"
    assert "app-active" in decision.rewritten_text
    assert "redirect" in decision.runtime_context


def test_route_decision_summary_is_json_ready():
    decision = RouteDecision(route="agent", final_reason="default")
    assert decision.to_ledger_payload() == {
        "route": "agent",
        "final_reason": "default",
        "response_type": None,
        "has_runtime_context": False,
        "has_rewritten_text": False,
    }


def test_router_advisory_ignores_attempted_final_answers():
    advisory = parse_router_advisory(
        '{"intent":"analytics_followup","confidence":0.82,'
        '"advisory_context":"May refer to the previous refunds answer.",'
        '"answer":"The answer is Amazon."}'
    )

    assert advisory.intent == "analytics_followup"
    assert advisory.confidence == 0.82
    assert "refunds" in advisory.advisory_context
    assert "Amazon" not in advisory.to_runtime_context()


@pytest.mark.asyncio
async def test_middle_model_advisory_disabled_by_default():
    calls = {"count": 0}

    async def _fake_call(**kwargs):
        calls["count"] += 1
        raise AssertionError("should not be called")

    context = await maybe_build_middle_model_advisory(
        "what changed most?",
        config={},
        async_call_llm_fn=_fake_call,
    )

    assert context == ""
    assert calls["count"] == 0


@pytest.mark.asyncio
async def test_middle_model_advisory_is_context_only_when_enabled():
    async def _fake_call(**kwargs):
        class _Message:
            content = (
                '{"intent":"definition_redirect","confidence":0.91,'
                '"advisory_context":"User is changing active-user definition to app-active.",'
                '"answer":"Do not use this as final text."}'
            )

        class _Choice:
            message = _Message()

        class _Response:
            choices = [_Choice()]

        assert kwargs["task"] == "agentic_router"
        return _Response()

    context = await maybe_build_middle_model_advisory(
        "make that app-active instead",
        config={
            "gateway": {"agentic_router": {"middle_model": {"enabled": True}}},
        },
        async_call_llm_fn=_fake_call,
    )

    assert "definition_redirect" in context
    assert "app-active" in context
    assert "Do not use this" not in context
