"""Tests for the pre_gateway_dispatch plugin hook.

The hook allows plugins to intercept incoming messages before auth and
agent dispatch. It runs in _handle_message and acts on returned action
dicts: {"action": "skip"|"rewrite"|"annotate"|"respond"|"allow"}.
Text mutation requires an explicit transport-normalization type; semantic
interpretation belongs to the agent runtime.
"""

import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _clear_auth_env(monkeypatch) -> None:
    for key in (
        "TELEGRAM_ALLOWED_USERS",
        "WHATSAPP_ALLOWED_USERS",
        "GATEWAY_ALLOWED_USERS",
        "TELEGRAM_ALLOW_ALL_USERS",
        "WHATSAPP_ALLOW_ALL_USERS",
        "GATEWAY_ALLOW_ALL_USERS",
    ):
        monkeypatch.delenv(key, raising=False)


def _make_event(
    text: str = "hello",
    platform: Platform = Platform.WHATSAPP,
    thread_id: str | None = None,
) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_id="m1",
        source=SessionSource(
            platform=platform,
            user_id="15551234567@s.whatsapp.net",
            chat_id="15551234567@s.whatsapp.net",
            user_name="tester",
            chat_type="dm",
            thread_id=thread_id,
        ),
    )


def _make_runner(platform: Platform):
    from gateway.run import GatewayRunner

    config = GatewayConfig(
        platforms={platform: PlatformConfig(enabled=True)},
    )
    runner = object.__new__(GatewayRunner)
    runner.config = config
    adapter = SimpleNamespace(send=AsyncMock())
    runner.adapters = {platform: adapter}
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = False
    runner.pairing_store._is_rate_limited.return_value = False
    runner.session_store = MagicMock()
    runner._running_agents = {}
    runner._update_prompt_pending = {}
    runner._runtime_fingerprint = {"source": "test"}
    return runner, adapter


@pytest.mark.asyncio
async def test_hook_skip_short_circuits_dispatch(monkeypatch):
    """A plugin returning {'action': 'skip'} drops the message before auth."""
    _clear_auth_env(monkeypatch)

    def _fake_hook(name, **kwargs):
        if name == "pre_gateway_dispatch":
            return [{"action": "skip", "reason": "plugin-handled"}]
        return []

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", _fake_hook)

    runner, adapter = _make_runner(Platform.WHATSAPP)

    result = await runner._handle_message(_make_event("hi"))

    assert result is None
    adapter.send.assert_not_awaited()
    runner.pairing_store.generate_code.assert_not_called()


@pytest.mark.asyncio
async def test_hook_skip_reply_sends_reply_then_short_circuits(monkeypatch):
    """A plugin can drop dispatch while sending a visible reply."""
    _clear_auth_env(monkeypatch)

    def _fake_hook(name, **kwargs):
        if name == "pre_gateway_dispatch":
            return [{"action": "skip_reply", "text": "Approval denied."}]
        return []

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", _fake_hook)

    runner, adapter = _make_runner(Platform.SLACK)

    result = await runner._handle_message(
        _make_event("approved, fix it", platform=Platform.SLACK)
    )

    assert result is None
    adapter.send.assert_awaited_once_with(
        "15551234567@s.whatsapp.net",
        "Approval denied.",
        metadata=None,
    )
    runner.pairing_store.generate_code.assert_not_called()


@pytest.mark.asyncio
async def test_hook_skip_reply_preserves_thread_metadata(monkeypatch):
    """Visible hook replies stay in the originating Slack thread."""
    _clear_auth_env(monkeypatch)

    def _fake_hook(name, **kwargs):
        if name == "pre_gateway_dispatch":
            return [{"action": "skip_reply", "text": "Approval denied."}]
        return []

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", _fake_hook)

    runner, adapter = _make_runner(Platform.SLACK)

    result = await runner._handle_message(
        _make_event(
            "approved, fix it",
            platform=Platform.SLACK,
            thread_id="1710000000.000100",
        )
    )

    assert result is None
    adapter.send.assert_awaited_once_with(
        "15551234567@s.whatsapp.net",
        "Approval denied.",
        metadata={"thread_id": "1710000000.000100"},
    )
    runner.pairing_store.generate_code.assert_not_called()


@pytest.mark.asyncio
async def test_hook_transport_rewrite_replaces_event_text(monkeypatch):
    """A plugin can normalize transport wrappers without changing meaning."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("WHATSAPP_ALLOWED_USERS", "*")

    seen_text = {}

    def _fake_hook(name, **kwargs):
        if name == "pre_gateway_dispatch":
            return [
                {
                    "action": "rewrite",
                    "text": "REWRITTEN",
                    "rewrite_type": "transport_normalization",
                }
            ]
        return []

    async def _capture(event, source, _quick_key, _run_generation):
        seen_text["value"] = event.text
        return "ok"

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", _fake_hook)

    runner, _adapter = _make_runner(Platform.WHATSAPP)
    runner._handle_message_with_agent = _capture  # noqa: SLF001

    await runner._handle_message(_make_event("original"))

    assert seen_text.get("value") == "REWRITTEN"


@pytest.mark.asyncio
async def test_hook_bare_rewrite_is_not_allowed_to_change_user_text(monkeypatch):
    """Untyped rewrites are ignored so hooks cannot recast user intent."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("WHATSAPP_ALLOWED_USERS", "*")

    seen_text = {}

    def _fake_hook(name, **kwargs):
        if name == "pre_gateway_dispatch":
            return [{"action": "rewrite", "text": "Use option 1"}]
        return []

    async def _capture(event, source, _quick_key, _run_generation):
        seen_text["value"] = event.text
        return "ok"

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", _fake_hook)

    runner, _adapter = _make_runner(Platform.WHATSAPP)
    runner._handle_message_with_agent = _capture  # noqa: SLF001

    await runner._handle_message(_make_event("original user words"))

    assert seen_text.get("value") == "original user words"


@pytest.mark.asyncio
async def test_hook_annotate_adds_agent_runtime_context_without_user_text(monkeypatch):
    """A plugin can annotate the runtime without injecting notes into user text."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("WHATSAPP_ALLOWED_USERS", "*")

    seen = {}

    def _fake_hook(name, **kwargs):
        if name == "pre_gateway_dispatch":
            return [
                {
                    "action": "annotate",
                    "text": "canonical user request",
                    "text_type": "transport_normalization",
                    "context": "agent-only runtime note",
                    "reason": "agent-runtime-handoff",
                }
            ]
        return []

    async def _capture(event, source, _quick_key, _run_generation):
        seen["text"] = event.text
        seen["runtime_context"] = event.runtime_context
        return "ok"

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", _fake_hook)

    runner, _adapter = _make_runner(Platform.WHATSAPP)
    runner._handle_message_with_agent = _capture  # noqa: SLF001

    await runner._handle_message(_make_event("original"))

    assert seen == {
        "text": "canonical user request",
        "runtime_context": "agent-only runtime note",
    }


@pytest.mark.asyncio
async def test_hook_annotations_accumulate_and_are_ledgered(monkeypatch):
    """Non-terminal hooks all run, reach the agent, and appear in the ledger."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("WHATSAPP_ALLOWED_USERS", "*")

    seen = {}

    def _fake_hook(name, **kwargs):
        if name == "pre_gateway_dispatch":
            return [
                {"action": "allow", "reason": "noop", "plugin": "noop"},
                {
                    "action": "annotate",
                    "context": "first runtime note",
                    "reason": "first-note",
                    "plugin": "profile",
                },
                {
                    "action": "annotate",
                    "context": "second runtime note",
                    "reason": "second-note",
                    "plugin": "memory",
                },
            ]
        return []

    async def _capture(event, source, _quick_key, _run_generation):
        seen["runtime_context"] = event.runtime_context
        return "ok"

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", _fake_hook)

    runner, _adapter = _make_runner(Platform.WHATSAPP)
    runner.session_store.append_gateway_turn = MagicMock(return_value=101)
    runner._handle_message_with_agent = _capture  # noqa: SLF001

    result = await runner._handle_message(_make_event("original"))

    assert result == "ok"
    assert "first runtime note" in seen["runtime_context"]
    assert "second runtime note" in seen["runtime_context"]
    _session_id, summary = runner.session_store.append_gateway_turn.call_args.args
    assert summary["final_action"] == "continue"
    assert [entry["actor"] for entry in summary["decisions"]] == [
        "noop",
        "profile",
        "memory",
    ]


@pytest.mark.asyncio
async def test_hook_guardrail_respond_sends_message_and_short_circuits_dispatch(monkeypatch):
    """A guardrail {'action': 'respond'} sends text before auth."""
    _clear_auth_env(monkeypatch)

    def _fake_hook(name, **kwargs):
        if name == "pre_gateway_dispatch":
            return [
                {
                    "action": "respond",
                    "text": "FAST",
                    "response_type": "guardrail",
                    "reason": "deterministic-fast-path",
                }
            ]
        return []

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", _fake_hook)

    runner, adapter = _make_runner(Platform.WHATSAPP)

    result = await runner._handle_message(_make_event("hi"))

    assert result is None
    adapter.send.assert_awaited_once_with(
        "15551234567@s.whatsapp.net",
        "FAST",
        metadata=None,
    )
    runner.pairing_store.generate_code.assert_not_called()


@pytest.mark.asyncio
async def test_hook_bare_respond_is_not_allowed_to_conduct_dialogue(monkeypatch, caplog):
    """Bare pre-gateway responses must fall through to the agent runtime."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("WHATSAPP_ALLOWED_USERS", "*")
    caplog.set_level(logging.INFO, logger="gateway.run")

    seen_text = {}

    def _fake_hook(name, **kwargs):
        if name == "pre_gateway_dispatch":
            return [
                {
                    "action": "respond",
                    "text": "Which option should I use?",
                    "reason": "legacy-conversational-fast-path",
                }
            ]
        return []

    async def _capture(event, source, _quick_key, _run_generation):
        seen_text["value"] = event.text
        return "agent handled"

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", _fake_hook)

    runner, adapter = _make_runner(Platform.WHATSAPP)
    runner.session_store.append_gateway_turn = MagicMock(return_value=101)
    runner._handle_message_with_agent = _capture  # noqa: SLF001

    result = await runner._handle_message(_make_event("hi"))

    assert result == "agent handled"
    assert seen_text["value"] == "hi"
    adapter.send.assert_not_awaited()
    runner.session_store.append_gateway_turn.assert_called_once()
    _session_id, summary = runner.session_store.append_gateway_turn.call_args.args
    assert summary["final_action"] == "continue"
    assert summary["decisions"][0]["capability"] == "respond"
    assert summary["decisions"][0]["applied"] is False
    assert summary["decisions"][0]["blocked_reason"] == "conversational_respond_not_allowed"
    assert "gateway turn decision:" in caplog.text
    assert summary["turn_id"] in caplog.text


@pytest.mark.asyncio
async def test_slash_command_turn_is_persisted_to_session_db(monkeypatch):
    """Gateway slash commands return early but still get a decision ledger."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("WHATSAPP_ALLOWED_USERS", "*")
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda name, **kwargs: [])

    runner, _adapter = _make_runner(Platform.WHATSAPP)
    runner.session_store.get_or_create_session.return_value = SimpleNamespace(
        session_id="s1",
        session_key="k1",
    )
    runner._session_db = MagicMock()
    runner._session_db.append_gateway_turn = MagicMock(return_value=101)
    runner._handle_status_command = AsyncMock(return_value="status ok")  # noqa: SLF001

    result = await runner._handle_message(_make_event("/status"))

    assert result == "status ok"
    runner._session_db.append_gateway_turn.assert_called_once()
    _session_id, summary = runner._session_db.append_gateway_turn.call_args.args
    assert _session_id == "s1"
    assert summary["final_action"] == "command"
    assert summary["final_reason"] == "/status"
    assert summary["decisions"][0]["capability"] == "slash_command"
    assert summary["decisions"][0]["payload"]["command"] == "status"


@pytest.mark.asyncio
async def test_unknown_slash_command_turn_is_persisted_to_session_db(monkeypatch):
    """Unknown slash commands return early but still get a decision ledger."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("WHATSAPP_ALLOWED_USERS", "*")
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda name, **kwargs: [])
    monkeypatch.setattr("agent.skill_commands.get_skill_commands", lambda: {})
    monkeypatch.setattr("agent.skill_commands.resolve_skill_command_key", lambda command: None)
    monkeypatch.setattr("gateway.run._check_unavailable_skill", lambda command: None)

    runner, _adapter = _make_runner(Platform.WHATSAPP)
    runner.session_store.get_or_create_session.return_value = SimpleNamespace(
        session_id="s1",
        session_key="k1",
    )
    runner._session_db = MagicMock()
    runner._session_db.append_gateway_turn = MagicMock(return_value=101)

    result = await runner._handle_message(_make_event("/definitely-not-real"))

    assert "Unknown command `/definitely-not-real`" in result
    runner._session_db.append_gateway_turn.assert_called_once()
    _session_id, summary = runner._session_db.append_gateway_turn.call_args.args
    assert _session_id == "s1"
    assert summary["final_action"] == "command_error"
    assert summary["final_reason"] == "unknown_command:/definitely-not-real"
    assert summary["decisions"][0]["capability"] == "slash_command"


@pytest.mark.asyncio
async def test_turns_command_renders_recent_gateway_ledgers(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("WHATSAPP_ALLOWED_USERS", "*")
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda name, **kwargs: [])

    runner, _adapter = _make_runner(Platform.WHATSAPP)
    runner.session_store.get_or_create_session.return_value = SimpleNamespace(
        session_id="s1",
        session_key="k1",
    )
    runner._session_db = MagicMock()
    runner._session_db.get_gateway_turns.return_value = [
        {
            "recorded_at": time.mktime((2026, 6, 9, 0, 1, 2, 0, 0, -1)),
            "turn_id": "abcdef1234567890",
            "final_action": "continue",
            "final_reason": "agent_dispatch",
            "decision_count": 1,
            "summary": {
                "runtime_fingerprint": {"digest": "digest123"},
                "decisions": [
                    {
                        "capability": "respond",
                        "applied": False,
                        "blocked_reason": "conversational_respond_not_allowed",
                    }
                ],
            },
        }
    ]
    runner._session_db.append_gateway_turn = MagicMock(return_value=102)

    result = await runner._handle_message(_make_event("/turns 3"))

    assert "Gateway turn ledger" in result
    assert "Session: `s1`" in result
    assert "turn=`abcdef123456`" in result
    assert "action=`continue`" in result
    assert "respond:blocked:conversational_respond_not_allowed" in result
    runner._session_db.get_gateway_turns.assert_called_once_with("s1", limit=3)
    # The /turns command itself is also ledgered after reading prior turns.
    assert runner._session_db.append_gateway_turn.called


def test_turns_command_is_gateway_known_and_in_help():
    from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS, gateway_help_lines

    assert "turns" in GATEWAY_KNOWN_COMMANDS
    assert "gateway-turns" in GATEWAY_KNOWN_COMMANDS
    assert any("`/turns [limit]`" in line for line in gateway_help_lines())


@pytest.mark.asyncio
async def test_hook_allow_falls_through_to_auth(monkeypatch):
    """A plugin returning {'action': 'allow'} continues to normal dispatch."""
    _clear_auth_env(monkeypatch)
    # No allowed users set → auth fails → pairing flow triggers.
    monkeypatch.delenv("WHATSAPP_ALLOWED_USERS", raising=False)

    def _fake_hook(name, **kwargs):
        if name == "pre_gateway_dispatch":
            return [{"action": "allow"}]
        return []

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", _fake_hook)

    runner, adapter = _make_runner(Platform.WHATSAPP)
    runner._session_db = MagicMock()
    runner._session_db.append_gateway_turn = MagicMock(return_value=201)
    runner.pairing_store.generate_code.return_value = "12345"

    result = await runner._handle_message(_make_event("hi"))

    # auth chain ran → pairing code was generated
    assert result is None
    runner.pairing_store.generate_code.assert_called_once()
    runner._session_db.append_gateway_turn.assert_called_once()
    _session_id, summary = runner._session_db.append_gateway_turn.call_args.args
    assert summary["final_action"] == "auth_pairing"
    assert summary["final_reason"] == "pairing_code_sent"
    assert summary["decisions"][-1]["capability"] == "authorization"


@pytest.mark.asyncio
async def test_hook_exception_does_not_break_dispatch(monkeypatch):
    """A raising plugin hook does not break the gateway."""
    _clear_auth_env(monkeypatch)
    monkeypatch.delenv("WHATSAPP_ALLOWED_USERS", raising=False)

    def _fake_hook(name, **kwargs):
        raise RuntimeError("plugin blew up")

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", _fake_hook)

    runner, _adapter = _make_runner(Platform.WHATSAPP)
    runner.pairing_store.generate_code.return_value = None

    # Should not raise; falls through to auth chain.
    result = await runner._handle_message(_make_event("hi"))
    assert result is None


@pytest.mark.asyncio
async def test_internal_events_bypass_hook(monkeypatch):
    """Internal events (event.internal=True) skip the plugin hook entirely."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("WHATSAPP_ALLOWED_USERS", "*")

    called = {"count": 0}

    def _fake_hook(name, **kwargs):
        called["count"] += 1
        return [{"action": "skip"}]

    async def _capture(event, source, _quick_key, _run_generation):
        return "ok"

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", _fake_hook)

    runner, _adapter = _make_runner(Platform.WHATSAPP)
    runner._handle_message_with_agent = _capture  # noqa: SLF001

    event = _make_event("hi")
    event.internal = True

    # Even though the hook would say skip, internal events bypass it.
    await runner._handle_message(event)
    assert called["count"] == 0
