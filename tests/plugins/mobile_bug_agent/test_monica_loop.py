from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource

from plugins.mobile_bug_agent.config import (
    LinearConfig,
    LoopConfig,
    MonicaConfig,
    ProofConfig,
    RepoConfig,
    RuntimeConfig,
    SlackConfig,
    VerificationConfig,
)
import plugins.mobile_bug_agent.loop as monica_loop
from plugins.mobile_bug_agent.loop import MonicaLoop, MonicaLoopSkills
from plugins.mobile_bug_agent.slack_flow import MonicaSlackFlow
from plugins.mobile_bug_agent.state import MonicaState


def _slack_event(
    text: str,
    *,
    raw_text: str | None = None,
    raw_type: str = "message",
    raw_channel_type: str | None = None,
    raw_files: list[dict[str, Any]] | None = None,
    channel_id: str = "C_MOBILE",
    chat_type: str = "channel",
    user_id: str = "U_TAGGER",
    thread_ts: str = "1710000000.000100",
    message_ts: str = "1710000000.000200",
) -> MessageEvent:
    raw_message = {
        "type": raw_type,
        "text": raw_text if raw_text is not None else text,
        "channel": channel_id,
        "user": user_id,
        "thread_ts": thread_ts,
        "ts": message_ts,
        "permalink": "https://example.slack.com/archives/C_MOBILE/p1710000000000200",
        "files": raw_files or [],
    }
    if raw_channel_type is not None:
        raw_message["channel_type"] = raw_channel_type

    return MessageEvent(
        text=text,
        source=SessionSource(
            platform=Platform.SLACK,
            chat_id=channel_id,
            chat_type=chat_type,
            user_id=user_id,
            thread_id=thread_ts,
            message_id=message_ts,
        ),
        raw_message=raw_message,
        message_id=message_ts,
    )


def _approved_pr_raw_event() -> dict[str, Any]:
    return {
        "type": "app_mention",
        "channel": "C_MOBILE",
        "text": "<@U_MONICA> can you fix this marketplace PDP copy bug?",
        "ts": "1710000000.000200",
        "permalink": "https://example.slack.com/archives/C_MOBILE/p1710000000000200",
    }


def test_slack_flow_ignores_unmentioned_messages(tmp_path):
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("BMONICA",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=lambda run_id: None)

    result = flow.handle_gateway_event(
        _slack_event(
            "checkout crashes on Android",
            raw_text="checkout crashes on Android",
        )
    )

    assert result is None
    assert state.list_runs() == []


def test_slack_flow_swallow_tagged_monica_message_when_config_disabled(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=False,
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("BMONICA",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    flow = MonicaSlackFlow(
        config=config,
        state=state,
        loop_launcher=launched.append,
        approval_readiness_checker=lambda: (True, ""),
    )

    result = flow.handle_gateway_event(
        _slack_event(
            "checkout crashes on Android",
            raw_text="<@BMONICA> checkout crashes on Android",
        )
    )

    assert result == {"action": "skip", "reason": "monica_disabled"}
    assert state.list_runs() == []
    assert launched == []


def test_slack_flow_treats_tagged_natural_language_as_agentic_work(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("BMONICA",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    flow = MonicaSlackFlow(
        config=config,
        state=state,
        loop_launcher=launched.append,
        approval_readiness_checker=lambda: (True, ""),
    )

    result = flow.handle_gateway_event(
        _slack_event(
            "this checkout crash looks like the same Android issue from last week, can you clean it up?",
            raw_text="<@BMONICA> this checkout crash looks like the same Android issue from last week, can you clean it up?",
        )
    )

    assert result == {"action": "skip", "reason": "monica_loop_queued"}
    runs = state.list_runs()
    assert len(runs) == 1
    assert launched == [runs[0].id]
    assert runs[0].status == "queued"
    assert runs[0].intent == "agentic_triage"
    assert "checkout crash" in runs[0].request_text
    assert runs[0].raw_event is not None
    assert runs[0].raw_event["permalink"].startswith("https://example.slack.com/")


def test_slack_flow_strips_monica_mention_from_normalized_text(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("BMONICA",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    flow = MonicaSlackFlow(
        config=config,
        state=state,
        loop_launcher=launched.append,
        approval_readiness_checker=lambda: (True, ""),
    )

    result = flow.handle_gateway_event(
        _slack_event(
            "<@BMONICA> checkout crashes after promo on Android",
            raw_text="<@BMONICA> checkout crashes after promo on Android",
        )
    )

    runs = state.list_runs()
    assert result == {"action": "skip", "reason": "monica_loop_queued"}
    assert len(runs) == 1
    assert launched == [runs[0].id]
    assert runs[0].request_text == "checkout crashes after promo on Android"


def test_slack_flow_accepts_and_strips_display_label_mention(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("UMONICA",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    flow = MonicaSlackFlow(
        config=config,
        state=state,
        loop_launcher=launched.append,
        approval_readiness_checker=lambda: (True, ""),
    )

    result = flow.handle_gateway_event(
        _slack_event(
            "<@UMONICA|monica> checkout crashes after promo on Android",
            raw_text="<@UMONICA|monica> checkout crashes after promo on Android",
        )
    )

    runs = state.list_runs()
    assert result == {"action": "skip", "reason": "monica_loop_queued"}
    assert len(runs) == 1
    assert launched == [runs[0].id]
    assert runs[0].request_text == "checkout crashes after promo on Android"


def test_slack_flow_removes_gateway_block_kit_payload_from_request_text(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(
            allowed_channels=("D_MONICA",),
            bot_user_ids=("UMONICA",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    flow = MonicaSlackFlow(
        config=config,
        state=state,
        loop_launcher=launched.append,
        approval_readiness_checker=lambda: (True, ""),
    )
    human_text = (
        "Monica live ingress smoke test. This is not a mobile app bug and should not "
        "create a Linear issue or PR."
    )
    gateway_text = (
        f"{human_text} *Sent using* ChatGPT\n\n"
        "[Slack Block Kit payload for this message]\n"
        "```json\n"
        '[{"type":"context","elements":[{"type":"mrkdwn","text":"*Sent using* ChatGPT"}]}]\n'
        "```"
    )

    result = flow.handle_gateway_event(
        _slack_event(
            gateway_text,
            raw_text=gateway_text,
            raw_channel_type="im",
            channel_id="D_MONICA",
            chat_type="dm",
        )
    )

    runs = state.list_runs()
    assert result == {"action": "skip", "reason": "monica_loop_queued"}
    assert len(runs) == 1
    assert launched == [runs[0].id]
    assert runs[0].request_text == human_text


def test_slack_flow_without_bot_user_ids_does_not_read_generic_channel_messages(tmp_path):
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(allowed_channels=("C_MOBILE",)),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=lambda run_id: None)

    result = flow.handle_gateway_event(
        _slack_event(
            "checkout crashes on Android",
            raw_text="checkout crashes on Android",
            raw_type="message",
        )
    )

    assert result is None
    assert state.list_runs() == []


def test_slack_flow_treats_direct_message_as_explicit_monica_intent(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(bot_user_ids=("U_MONICA",)),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    flow = MonicaSlackFlow(
        config=config,
        state=state,
        loop_launcher=launched.append,
        approval_readiness_checker=lambda: (True, ""),
    )

    result = flow.handle_gateway_event(
        _slack_event(
            "checkout crashes on Android after applying a promo code",
            raw_text="checkout crashes on Android after applying a promo code",
            raw_channel_type="im",
            channel_id="D_MONICA",
            chat_type="dm",
        )
    )

    runs = state.list_runs()
    assert result == {"action": "skip", "reason": "monica_loop_queued"}
    assert len(runs) == 1
    assert launched == [runs[0].id]
    assert runs[0].channel_id == "D_MONICA"
    assert runs[0].request_text == "checkout crashes on Android after applying a promo code"


def test_slack_flow_side_effect_rollout_allows_direct_message_with_channel_allowlist(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("U_MONICA",),
            approver_user_ids=("U_APPROVER",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    flow = MonicaSlackFlow(
        config=config,
        state=state,
        loop_launcher=launched.append,
        approval_readiness_checker=lambda: (True, ""),
    )

    result = flow.handle_gateway_event(
        _slack_event(
            "marketplace PDP copy is wrong",
            raw_text="marketplace PDP copy is wrong",
            raw_channel_type="im",
            channel_id="D_MONICA",
            chat_type="dm",
        )
    )

    runs = state.list_runs()
    assert result == {"action": "skip", "reason": "monica_loop_queued"}
    assert len(runs) == 1
    assert launched == [runs[0].id]
    assert runs[0].channel_id == "D_MONICA"
    assert runs[0].raw_event is not None
    assert runs[0].raw_event["channel_type"] == "im"
    assert runs[0].request_text == "marketplace PDP copy is wrong"


def test_slack_flow_ignores_unmentioned_group_dm_messages(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(bot_user_ids=("U_MONICA",)),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    flow = MonicaSlackFlow(
        config=config,
        state=state,
        loop_launcher=launched.append,
        approval_readiness_checker=lambda: (True, ""),
    )

    result = flow.handle_gateway_event(
        _slack_event(
            "checkout crashes on Android after applying a promo code",
            raw_text="checkout crashes on Android after applying a promo code",
            raw_channel_type="mpim",
            channel_id="G_MONICA",
            chat_type="dm",
        )
    )

    assert result is None
    assert state.list_runs() == []
    assert launched == []


def test_slack_flow_allows_app_mention_event_before_bot_user_id_is_configured(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(allowed_channels=("C_MOBILE",)),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    flow = MonicaSlackFlow(
        config=config,
        state=state,
        loop_launcher=launched.append,
        approval_readiness_checker=lambda: (True, ""),
    )

    result = flow.handle_gateway_event(
        _slack_event(
            "checkout crash on Android",
            raw_text="<@BMONICA> checkout crash on Android",
            raw_type="app_mention",
        )
    )

    runs = state.list_runs()
    assert result == {"action": "skip", "reason": "monica_loop_queued"}
    assert len(runs) == 1
    assert launched == [runs[0].id]


def test_slack_flow_dry_run_ignores_app_mention_for_different_configured_bot_user_id(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("U_STALE",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    flow = MonicaSlackFlow(
        config=config,
        state=state,
        loop_launcher=launched.append,
        approval_readiness_checker=lambda: (True, ""),
    )

    result = flow.handle_gateway_event(
        _slack_event(
            "checkout crash on Android",
            raw_text="<@U_ACTUAL> checkout crash on Android",
            raw_type="app_mention",
        )
    )

    assert result is None
    assert state.list_runs() == []
    assert launched == []


def test_slack_flow_side_effect_mode_ignores_app_mention_for_different_bot_user_id(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        rollout_mode="linear_only",
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("U_MONICA",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)

    result = flow.handle_gateway_event(
        _slack_event(
            "checkout crash on Android",
            raw_text="<@U_CHANDLER> checkout crash on Android",
            raw_type="app_mention",
        )
    )

    assert result is None
    assert state.list_runs() == []
    assert launched == []


def test_slack_flow_side_effect_mode_requires_configured_bot_user_id(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        rollout_mode="linear_only",
        slack=SlackConfig(allowed_channels=("C_MOBILE",)),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)

    result = flow.handle_gateway_event(
        _slack_event(
            "checkout crash on Android",
            raw_text="<@U_MONICA> checkout crash on Android",
            raw_type="app_mention",
        )
    )

    assert result == {
        "action": "skip_reply",
        "reason": "monica_bot_user_ids_required",
        "text": (
            "I cannot start Monica here yet. "
            "Configure mobile_bug_agent.slack.bot_user_ids with Monica's Slack user ID "
            "before enabling real side effects."
        ),
    }
    assert state.list_runs() == []
    assert launched == []


def test_slack_flow_side_effect_mode_rejects_bot_id_values_as_mentions(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        rollout_mode="linear_only",
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("BMONICA",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)

    result = flow.handle_gateway_event(
        _slack_event(
            "checkout crash on Android",
            raw_text="<@BMONICA> checkout crash on Android",
            raw_type="app_mention",
        )
    )

    assert result == {
        "action": "skip_reply",
        "reason": "monica_bot_user_ids_invalid",
        "text": (
            "I cannot start Monica here yet. "
            "mobile_bug_agent.slack.bot_user_ids must contain Slack mention user IDs "
            "like U123, not bot_id values like BMONICA."
        ),
    }
    assert state.list_runs() == []
    assert launched == []


def test_slack_flow_side_effect_mode_rejects_bot_id_config_on_real_app_mention(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        rollout_mode="linear_only",
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("BMONICA",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)

    result = flow.handle_gateway_event(
        _slack_event(
            "checkout crash on Android",
            raw_text="<@U_MONICA> checkout crash on Android",
            raw_type="app_mention",
        )
    )

    assert result == {
        "action": "skip_reply",
        "reason": "monica_bot_user_ids_invalid",
        "text": (
            "I cannot start Monica here yet. "
            "mobile_bug_agent.slack.bot_user_ids must contain Slack mention user IDs "
            "like U123, not bot_id values like BMONICA."
        ),
    }
    assert state.list_runs() == []
    assert launched == []


def test_slack_flow_side_effect_mode_rejects_malformed_bot_user_ids_on_app_mention(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        rollout_mode="linear_only",
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("monica",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)

    result = flow.handle_gateway_event(
        _slack_event(
            "checkout crash on Android",
            raw_text="<@U_MONICA> checkout crash on Android",
            raw_type="app_mention",
        )
    )

    assert result == {
        "action": "skip_reply",
        "reason": "monica_bot_user_ids_invalid",
        "text": (
            "I cannot start Monica here yet. "
            "mobile_bug_agent.slack.bot_user_ids must contain Slack mention user IDs "
            "like U123, not invalid values like monica."
        ),
    }
    assert state.list_runs() == []
    assert launched == []


def test_slack_flow_side_effect_mode_rejects_handle_style_bot_user_ids_on_app_mention(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        rollout_mode="linear_only",
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("@monica",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)

    result = flow.handle_gateway_event(
        _slack_event(
            "checkout crash on Android",
            raw_text="<@U_MONICA> checkout crash on Android",
            raw_type="app_mention",
        )
    )

    assert result == {
        "action": "skip_reply",
        "reason": "monica_bot_user_ids_invalid",
        "text": (
            "I cannot start Monica here yet. "
            "mobile_bug_agent.slack.bot_user_ids must contain Slack mention user IDs "
            "like U123, not handles like @monica."
        ),
    }
    assert state.list_runs() == []
    assert launched == []


def test_slack_flow_refuses_side_effect_rollout_when_allowed_channels_are_empty(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        rollout_mode="linear_only",
        slack=SlackConfig(bot_user_ids=("BMONICA",)),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)

    result = flow.handle_gateway_event(
        _slack_event(
            "checkout crash on Android",
            raw_text="<@BMONICA> checkout crash on Android",
            raw_type="app_mention",
        )
    )

    assert result == {
        "action": "skip_reply",
        "reason": "monica_allowed_channels_required",
        "text": (
            "I cannot start Monica here yet. "
            "Configure mobile_bug_agent.slack.allowed_channels before enabling real side effects."
        ),
    }
    assert state.list_runs() == []
    assert launched == []


def test_slack_flow_empty_allowlist_guard_still_ignores_unmentioned_messages(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        rollout_mode="linear_only",
        slack=SlackConfig(bot_user_ids=("BMONICA",)),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)

    result = flow.handle_gateway_event(
        _slack_event(
            "checkout crash on Android",
            raw_text="checkout crash on Android",
            raw_type="message",
        )
    )

    assert result is None
    assert state.list_runs() == []
    assert launched == []


def test_slack_flow_refuses_side_effect_rollout_with_channel_names_in_allowlist(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        rollout_mode="linear_only",
        slack=SlackConfig(
            allowed_channels=("#mobile-bugs",),
            bot_user_ids=("U_MONICA",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)

    result = flow.handle_gateway_event(
        _slack_event(
            "checkout crash on Android",
            raw_text="<@U_MONICA> checkout crash on Android",
            raw_type="app_mention",
        )
    )

    assert result == {
        "action": "skip_reply",
        "reason": "monica_allowed_channels_invalid",
        "text": (
            "I cannot start Monica here yet. "
            "mobile_bug_agent.slack.allowed_channels must contain Slack channel IDs "
            "like C123 or G123, not names like #mobile-bugs."
        ),
    }
    assert state.list_runs() == []
    assert launched == []


def test_slack_flow_refuses_dry_run_with_channel_names_in_allowlist(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        rollout_mode="dry_run",
        slack=SlackConfig(
            allowed_channels=("#mobile-bugs",),
            bot_user_ids=("U_MONICA",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)

    result = flow.handle_gateway_event(
        _slack_event(
            "checkout crash on Android",
            raw_text="<@U_MONICA> checkout crash on Android",
            raw_type="app_mention",
        )
    )

    assert result == {
        "action": "skip_reply",
        "reason": "monica_allowed_channels_invalid",
        "text": (
            "I cannot start Monica here yet. "
            "mobile_bug_agent.slack.allowed_channels must contain Slack channel IDs "
            "like C123 or G123, not names like #mobile-bugs."
        ),
    }
    assert state.list_runs() == []
    assert launched == []


def test_slack_flow_tagged_message_in_disallowed_channel_does_not_fall_through(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        rollout_mode="linear_only",
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("BMONICA",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)

    result = flow.handle_gateway_event(
        _slack_event(
            "checkout crash on Android",
            raw_text="<@BMONICA> checkout crash on Android",
            raw_type="app_mention",
            channel_id="C_OTHER",
        )
    )

    assert result == {
        "action": "skip_reply",
        "reason": "monica_channel_not_allowed",
        "text": "I cannot run Monica in this channel. Ask an admin to add this channel to mobile_bug_agent.slack.allowed_channels.",
    }
    assert state.list_runs() == []
    assert launched == []


def test_slack_flow_does_not_relaunch_existing_active_thread(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("BMONICA",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)
    event = _slack_event(
        "this checkout crash looks mobile, please clean it up",
        raw_text="<@BMONICA> this checkout crash looks mobile, please clean it up",
    )

    first = flow.handle_gateway_event(event)
    second = flow.handle_gateway_event(event)

    runs = state.list_runs()
    assert first == {"action": "skip", "reason": "monica_loop_queued"}
    assert second == {"action": "skip", "reason": "monica_loop_already_active"}
    assert len(runs) == 1
    assert launched == [runs[0].id]


def test_slack_flow_does_not_launch_when_create_discovers_existing_run(tmp_path, monkeypatch):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("BMONICA",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    existing = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_TAGGER",
        request_text="checkout crash on Android",
    )
    monkeypatch.setattr(state, "find_run", lambda **_: None)
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)

    result = flow.handle_gateway_event(
        _slack_event(
            "checkout crash on Android",
            raw_text="<@BMONICA> checkout crash on Android",
            thread_ts="1710000000.000100",
        )
    )

    assert result == {"action": "skip", "reason": "monica_loop_already_active"}
    assert state.get_run(existing.id) == existing
    assert launched == []


def test_slack_flow_refuses_new_run_during_runtime_sync_lease(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("BMONICA",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    lease, reason = state.try_acquire_runtime_sync_lease(
        owner_id="hermes-update",
        owner_pid=1234,
        owner_host="host",
        project_root="/repo/hermes",
        pre_update_commit="dead1234",
        started_at="2026-06-15T10:00:00Z",
        expires_at="2026-06-15T10:15:00Z",
    )
    assert lease is not None
    assert reason == ""
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)

    result = flow.handle_gateway_event(
        _slack_event(
            "marketplace PDP copy is wrong",
            raw_text="<@BMONICA> marketplace PDP copy is wrong",
        )
    )

    assert result == {
        "action": "skip_reply",
        "reason": "monica_runtime_sync_in_progress",
        "text": (
            "Monica is temporarily paused while Hermes is updating. "
            "Please retry after the update finishes."
        ),
    }
    assert state.list_runs() == []
    assert launched == []


def test_slack_flow_tagged_approval_resumes_waiting_run(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("BMONICA",),
            approver_user_ids=("U_APPROVER",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_TAGGER",
        request_text="can you fix this Android checkout crash?",
    )
    state.update_run(
        run.id,
        status="awaiting_fix_approval",
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
    )
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)

    result = flow.handle_gateway_event(
        _slack_event(
            "approved, fix it",
            raw_text="<@BMONICA> approved, fix it",
            user_id="U_APPROVER",
            thread_ts="1710000000.000100",
        )
    )

    updated = state.get_run(run.id)
    assert result == {"action": "skip", "reason": "monica_loop_approved"}
    assert updated is not None
    assert updated.status == "approved"
    assert updated.approved_by_user_id == "U_APPROVER"
    assert launched == [run.id]


def test_slack_flow_tagged_approval_logs_breadcrumbs(tmp_path, caplog):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("BMONICA",),
            approver_user_ids=("U_APPROVER",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_TAGGER",
        request_text="can you fix this Android checkout crash?",
    )
    state.update_run(
        run.id,
        status="awaiting_fix_approval",
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
    )
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)

    with caplog.at_level(logging.INFO, logger="plugins.mobile_bug_agent.slack_flow"):
        result = flow.handle_gateway_event(
            _slack_event(
                "approved, fix it",
                raw_text="<@BMONICA> approved, fix it",
                user_id="U_APPROVER",
                thread_ts="1710000000.000100",
            )
        )

    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert result == {"action": "skip", "reason": "monica_loop_approved"}
    assert "event=approved" in logs
    assert run.id in logs
    assert "C_MOBILE" in logs
    assert "1710000000.000100" in logs
    assert "MOB-123" in logs
    assert "https://linear.app/acme/issue/MOB-123" in logs
    assert "approved_by=U_APPROVER" in logs
    assert launched == [run.id]


def test_slack_flow_does_not_launch_when_approval_discovers_already_approved(tmp_path, monkeypatch):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("BMONICA",),
            approver_user_ids=("U_APPROVER",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    stale = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_TAGGER",
        request_text="can you fix this Android checkout crash?",
        status="awaiting_fix_approval",
    )
    state.approve_fix(stale.id, approved_by_user_id="U_FIRST")
    monkeypatch.setattr(state, "find_run", lambda **_: stale)
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)

    result = flow.handle_gateway_event(
        _slack_event(
            "approved, fix it",
            raw_text="<@BMONICA> approved, fix it",
            user_id="U_APPROVER",
            thread_ts="1710000000.000100",
        )
    )

    updated = state.get_run(stale.id)
    assert result == {"action": "skip", "reason": "monica_loop_already_active"}
    assert updated is not None
    assert updated.status == "approved"
    assert updated.approved_by_user_id == "U_FIRST"
    assert launched == []


def test_slack_flow_accepts_take_the_fix_as_tagged_approval(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("BMONICA",),
            approver_user_ids=("U_APPROVER",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_TAGGER",
        request_text="can you fix this Android checkout crash?",
    )
    state.update_run(
        run.id,
        status="awaiting_fix_approval",
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
    )
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)

    result = flow.handle_gateway_event(
        _slack_event(
            "yes, take the fix",
            raw_text="<@BMONICA> yes, take the fix",
            user_id="U_APPROVER",
            thread_ts="1710000000.000100",
        )
    )

    updated = state.get_run(run.id)
    assert result == {"action": "skip", "reason": "monica_loop_approved"}
    assert updated is not None
    assert updated.status == "approved"
    assert updated.approved_by_user_id == "U_APPROVER"
    assert launched == [run.id]


def test_slack_flow_accepts_yes_fix_it_as_tagged_approval(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("BMONICA",),
            approver_user_ids=("U_APPROVER",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_TAGGER",
        request_text="can you fix this Android checkout crash?",
    )
    state.update_run(
        run.id,
        status="awaiting_fix_approval",
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
    )
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)

    result = flow.handle_gateway_event(
        _slack_event(
            "yes, fix it",
            raw_text="<@BMONICA> yes, fix it",
            user_id="U_APPROVER",
            thread_ts="1710000000.000100",
        )
    )

    updated = state.get_run(run.id)
    assert result == {"action": "skip", "reason": "monica_loop_approved"}
    assert updated is not None
    assert updated.status == "approved"
    assert updated.approved_by_user_id == "U_APPROVER"
    assert launched == [run.id]


def test_slack_flow_fix_request_is_not_approval(tmp_path):
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("BMONICA",),
            approver_user_ids=("U_APPROVER",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_TAGGER",
        request_text="can you fix this Android checkout crash?",
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123")
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=lambda run_id: None)

    result = flow.handle_gateway_event(
        _slack_event(
            "can you fix it?",
            raw_text="<@BMONICA> can you fix it?",
            user_id="U_APPROVER",
            thread_ts="1710000000.000100",
        )
    )

    updated = state.get_run(run.id)
    assert result == {"action": "skip", "reason": "monica_loop_already_active"}
    assert updated is not None
    assert updated.status == "awaiting_fix_approval"
    assert updated.approved_by_user_id == ""


def test_slack_flow_new_context_requeues_waiting_run_for_linear_update(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("BMONICA",),
            approver_user_ids=("U_APPROVER",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_TAGGER",
        request_text="can you fix this Android checkout crash?",
    )
    state.update_run(
        run.id,
        status="awaiting_fix_approval",
        linear_identifier="MOB-123",
        linear_issue_id="issue-id",
        linear_url="https://linear.app/acme/issue/MOB-123",
        proof_screen="/OldPdpScreen",
        approved_by_user_id="",
    )
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)

    result = flow.handle_gateway_event(
        _slack_event(
            "new reproduction detail: this also happens on iOS after Apple Pay",
            raw_text="<@BMONICA> new reproduction detail: this also happens on iOS after Apple Pay",
            user_id="U_REPRODUCER",
            thread_ts="1710000000.000100",
            message_ts="1710000002.000300",
        )
    )

    updated = state.get_run(run.id)
    assert result == {"action": "skip", "reason": "monica_loop_requeued"}
    assert updated is not None
    assert updated.status == "queued"
    assert updated.linear_identifier == "MOB-123"
    assert updated.linear_issue_id == "issue-id"
    assert updated.linear_url == "https://linear.app/acme/issue/MOB-123"
    assert updated.message_ts == "1710000002.000300"
    assert updated.user_id == "U_REPRODUCER"
    assert "new reproduction detail" in updated.request_text
    assert updated.approved_by_user_id == ""
    assert updated.proof_screen == ""
    assert launched == [run.id]


def test_slack_flow_question_shaped_approval_is_not_approval(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("BMONICA",),
            approver_user_ids=("U_APPROVER",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_TAGGER",
        request_text="can you fix this Android checkout crash?",
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)

    result = flow.handle_gateway_event(
        _slack_event(
            "should I approve this after QA is done?",
            raw_text="<@BMONICA> should I approve this after QA is done?",
            user_id="U_APPROVER",
            thread_ts="1710000000.000100",
        )
    )

    updated = state.get_run(run.id)
    assert result == {"action": "skip", "reason": "monica_loop_already_active"}
    assert updated is not None
    assert updated.status == "awaiting_fix_approval"
    assert updated.approved_by_user_id == ""
    assert launched == []


def test_slack_flow_bare_approved_question_is_not_approval(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("BMONICA",),
            approver_user_ids=("U_APPROVER",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_TAGGER",
        request_text="can you fix this Android checkout crash?",
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)

    result = flow.handle_gateway_event(
        _slack_event(
            "approved?",
            raw_text="<@BMONICA> approved?",
            user_id="U_APPROVER",
            thread_ts="1710000000.000100",
        )
    )

    updated = state.get_run(run.id)
    assert result == {"action": "skip", "reason": "monica_loop_already_active"}
    assert updated is not None
    assert updated.status == "awaiting_fix_approval"
    assert updated.approved_by_user_id == ""
    assert launched == []


def test_slack_flow_approve_this_question_is_not_approval(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("BMONICA",),
            approver_user_ids=("U_APPROVER",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_TAGGER",
        request_text="can you fix this Android checkout crash?",
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)

    result = flow.handle_gateway_event(
        _slack_event(
            "approve this?",
            raw_text="<@BMONICA> approve this?",
            user_id="U_APPROVER",
            thread_ts="1710000000.000100",
        )
    )

    updated = state.get_run(run.id)
    assert result == {"action": "skip", "reason": "monica_loop_already_active"}
    assert updated is not None
    assert updated.status == "awaiting_fix_approval"
    assert updated.approved_by_user_id == ""
    assert launched == []


def test_slack_flow_ship_it_question_is_not_approval(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("BMONICA",),
            approver_user_ids=("U_APPROVER",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_TAGGER",
        request_text="can you fix this Android checkout crash?",
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)

    result = flow.handle_gateway_event(
        _slack_event(
            "ship it?",
            raw_text="<@BMONICA> ship it?",
            user_id="U_APPROVER",
            thread_ts="1710000000.000100",
        )
    )

    updated = state.get_run(run.id)
    assert result == {"action": "skip", "reason": "monica_loop_already_active"}
    assert updated is not None
    assert updated.status == "awaiting_fix_approval"
    assert updated.approved_by_user_id == ""
    assert launched == []


def test_slack_flow_can_you_approve_question_is_not_approval(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("BMONICA",),
            approver_user_ids=("U_APPROVER",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_TAGGER",
        request_text="can you fix this Android checkout crash?",
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)

    result = flow.handle_gateway_event(
        _slack_event(
            "can you approve this once QA is done?",
            raw_text="<@BMONICA> can you approve this once QA is done?",
            user_id="U_APPROVER",
            thread_ts="1710000000.000100",
        )
    )

    updated = state.get_run(run.id)
    assert result == {"action": "skip", "reason": "monica_loop_already_active"}
    assert updated is not None
    assert updated.status == "awaiting_fix_approval"
    assert updated.approved_by_user_id == ""
    assert launched == []


def test_slack_flow_negated_approval_is_not_approval(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("BMONICA",),
            approver_user_ids=("U_APPROVER",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_TAGGER",
        request_text="can you fix this Android checkout crash?",
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)

    result = flow.handle_gateway_event(
        _slack_event(
            "not approved yet, still checking QA",
            raw_text="<@BMONICA> not approved yet, still checking QA",
            user_id="U_APPROVER",
            thread_ts="1710000000.000100",
        )
    )

    updated = state.get_run(run.id)
    assert result == {"action": "skip", "reason": "monica_loop_already_active"}
    assert updated is not None
    assert updated.status == "awaiting_fix_approval"
    assert updated.approved_by_user_id == ""
    assert launched == []


def test_slack_flow_negated_go_ahead_is_not_approval(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("BMONICA",),
            approver_user_ids=("U_APPROVER",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_TAGGER",
        request_text="can you fix this Android checkout crash?",
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)

    result = flow.handle_gateway_event(
        _slack_event(
            "don't go ahead yet, waiting for QA",
            raw_text="<@BMONICA> don't go ahead yet, waiting for QA",
            user_id="U_APPROVER",
            thread_ts="1710000000.000100",
        )
    )

    updated = state.get_run(run.id)
    assert result == {"action": "skip", "reason": "monica_loop_already_active"}
    assert updated is not None
    assert updated.status == "awaiting_fix_approval"
    assert updated.approved_by_user_id == ""
    assert launched == []


def test_slack_flow_negated_ship_it_is_not_approval(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("BMONICA",),
            approver_user_ids=("U_APPROVER",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_TAGGER",
        request_text="can you fix this Android checkout crash?",
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)

    result = flow.handle_gateway_event(
        _slack_event(
            "don't ship it yet, QA is still checking",
            raw_text="<@BMONICA> don't ship it yet, QA is still checking",
            user_id="U_APPROVER",
            thread_ts="1710000000.000100",
        )
    )

    updated = state.get_run(run.id)
    assert result == {"action": "skip", "reason": "monica_loop_already_active"}
    assert updated is not None
    assert updated.status == "awaiting_fix_approval"
    assert updated.approved_by_user_id == ""
    assert launched == []


def test_slack_flow_tagged_cancel_blocks_waiting_run(tmp_path):
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("BMONICA",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_TAGGER",
        request_text="can you fix this Android checkout crash?",
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=lambda run_id: None)

    result = flow.handle_gateway_event(
        _slack_event(
            "do not fix this",
            raw_text="<@BMONICA> do not fix this",
            user_id="U_APPROVER",
            thread_ts="1710000000.000100",
        )
    )

    updated = state.get_run(run.id)
    assert result == {"action": "skip", "reason": "monica_loop_cancelled"}
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "cancelled by U_APPROVER"


def test_slack_flow_stop_question_does_not_cancel_waiting_run(tmp_path):
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("BMONICA",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_TAGGER",
        request_text="can you fix this Android checkout crash?",
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=lambda run_id: None)

    result = flow.handle_gateway_event(
        _slack_event(
            "stop?",
            raw_text="<@BMONICA> stop?",
            user_id="U_APPROVER",
            thread_ts="1710000000.000100",
        )
    )

    updated = state.get_run(run.id)
    assert result == {"action": "skip", "reason": "monica_loop_already_active"}
    assert updated is not None
    assert updated.status == "awaiting_fix_approval"
    assert updated.failure_reason == ""


def test_slack_flow_do_not_fix_question_does_not_cancel_waiting_run(tmp_path):
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("BMONICA",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_TAGGER",
        request_text="can you fix this Android checkout crash?",
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=lambda run_id: None)

    result = flow.handle_gateway_event(
        _slack_event(
            "do not fix?",
            raw_text="<@BMONICA> do not fix?",
            user_id="U_APPROVER",
            thread_ts="1710000000.000100",
        )
    )

    updated = state.get_run(run.id)
    assert result == {"action": "skip", "reason": "monica_loop_already_active"}
    assert updated is not None
    assert updated.status == "awaiting_fix_approval"
    assert updated.failure_reason == ""


def test_slack_flow_tagged_cancel_logs_breadcrumbs(tmp_path, caplog):
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("BMONICA",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_TAGGER",
        request_text="can you fix this Android checkout crash?",
    )
    state.update_run(
        run.id,
        status="awaiting_fix_approval",
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
    )
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=lambda run_id: None)

    with caplog.at_level(logging.INFO, logger="plugins.mobile_bug_agent.slack_flow"):
        result = flow.handle_gateway_event(
            _slack_event(
                "do not fix this",
                raw_text="<@BMONICA> do not fix this",
                user_id="U_APPROVER",
                thread_ts="1710000000.000100",
            )
        )

    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert result == {"action": "skip", "reason": "monica_loop_cancelled"}
    assert "event=cancelled" in logs
    assert run.id in logs
    assert "C_MOBILE" in logs
    assert "1710000000.000100" in logs
    assert "MOB-123" in logs
    assert "https://linear.app/acme/issue/MOB-123" in logs
    assert "cancelled_by=U_APPROVER" in logs


def test_slack_flow_tagged_cancel_blocks_active_run_before_approval(tmp_path):
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("BMONICA",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_TAGGER",
        request_text="can you fix this Android checkout crash?",
    )
    state.update_run(run.id, status="triaging")
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=lambda run_id: None)

    result = flow.handle_gateway_event(
        _slack_event(
            "cancel",
            raw_text="<@BMONICA> cancel",
            user_id="U_APPROVER",
            thread_ts="1710000000.000100",
        )
    )

    updated = state.get_run(run.id)
    assert result == {"action": "skip", "reason": "monica_loop_cancelled"}
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "cancelled by U_APPROVER"


def test_slack_flow_tagged_cancel_does_not_requeue_blocked_run(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("BMONICA",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_TAGGER",
        request_text="can you fix this Android checkout crash?",
    )
    state.update_run(run.id, status="blocked", failure_reason="cancelled by U_APPROVER")
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)

    result = flow.handle_gateway_event(
        _slack_event(
            "cancel",
            raw_text="<@BMONICA> cancel",
            user_id="U_APPROVER",
            thread_ts="1710000000.000100",
        )
    )

    updated = state.get_run(run.id)
    assert result == {"action": "skip", "reason": "monica_loop_cancelled"}
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "cancelled by U_APPROVER"
    assert launched == []


def test_slack_flow_tagged_cancel_does_not_requeue_completed_run(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("BMONICA",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_TAGGER",
        request_text="can you fix this Android checkout crash?",
    )
    state.update_run(
        run.id,
        status="done",
        linear_identifier="MOB-123",
        pr_url="https://github.com/acme/mobile/pull/123",
    )
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)

    result = flow.handle_gateway_event(
        _slack_event(
            "stop",
            raw_text="<@BMONICA> stop",
            user_id="U_APPROVER",
            thread_ts="1710000000.000100",
        )
    )

    updated = state.get_run(run.id)
    assert result == {"action": "skip", "reason": "monica_loop_cancelled"}
    assert updated is not None
    assert updated.status == "done"
    assert updated.pr_url == "https://github.com/acme/mobile/pull/123"
    assert launched == []


def test_slack_flow_negated_cancel_does_not_block_waiting_run(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("BMONICA",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_TAGGER",
        request_text="can you fix this Android checkout crash?",
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)

    result = flow.handle_gateway_event(
        _slack_event(
            "don't stop yet, still checking QA",
            raw_text="<@BMONICA> don't stop yet, still checking QA",
            user_id="U_APPROVER",
            thread_ts="1710000000.000100",
        )
    )

    updated = state.get_run(run.id)
    assert result == {"action": "skip", "reason": "monica_loop_already_active"}
    assert updated is not None
    assert updated.status == "awaiting_fix_approval"
    assert updated.failure_reason == ""
    assert launched == []


def test_slack_flow_bug_context_with_stop_is_not_cancel(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("BMONICA",),
            approver_user_ids=("U_APPROVER",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_TAGGER",
        request_text="can you fix this Android checkout crash?",
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)

    result = flow.handle_gateway_event(
        _slack_event(
            "can you stop this Android checkout crash from happening?",
            raw_text="<@BMONICA> can you stop this Android checkout crash from happening?",
            user_id="U_TAGGER",
            thread_ts="1710000000.000100",
        )
    )

    updated = state.get_run(run.id)
    assert result == {"action": "skip", "reason": "monica_loop_already_active"}
    assert updated is not None
    assert updated.status == "awaiting_fix_approval"
    assert updated.failure_reason == ""
    assert launched == []


def test_slack_flow_untagged_approval_is_ignored(tmp_path):
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("BMONICA",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_TAGGER",
        request_text="can you fix this Android checkout crash?",
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=lambda run_id: None)

    result = flow.handle_gateway_event(
        _slack_event(
            "approved, fix it",
            raw_text="approved, fix it",
            user_id="U_APPROVER",
            thread_ts="1710000000.000100",
        )
    )

    updated = state.get_run(run.id)
    assert result is None
    assert updated is not None
    assert updated.status == "awaiting_fix_approval"


def test_slack_flow_tagged_approval_from_unauthorized_user_is_explicitly_denied(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("BMONICA",),
            approver_user_ids=("U_APPROVER",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_TAGGER",
        request_text="can you fix this Android checkout crash?",
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)

    result = flow.handle_gateway_event(
        _slack_event(
            "approved, fix it",
            raw_text="<@BMONICA> approved, fix it",
            user_id="U_RANDOM",
            thread_ts="1710000000.000100",
        )
    )

    updated = state.get_run(run.id)
    assert result == {
        "action": "skip_reply",
        "reason": "monica_loop_approval_denied",
        "text": (
            "I cannot start the fix from this approval. "
            "A configured Monica approver must tag me to approve code changes. "
            "Allowed approvers: <@U_APPROVER>."
        ),
    }
    assert updated is not None
    assert updated.status == "awaiting_fix_approval"
    assert updated.approved_by_user_id == ""
    assert launched == []


def test_slack_flow_tagged_approval_refuses_when_readiness_check_fails(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("U_MONICA",),
            approver_user_ids=("U_APPROVER",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_TAGGER",
        request_text="can you fix this Android checkout crash?",
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    flow = MonicaSlackFlow(
        config=config,
        state=state,
        loop_launcher=launched.append,
        approval_readiness_checker=lambda: (False, "repo.url is missing"),
    )

    result = flow.handle_gateway_event(
        _slack_event(
            "approved, fix it",
            raw_text="<@U_MONICA> approved, fix it",
            user_id="U_APPROVER",
            thread_ts="1710000000.000100",
        )
    )

    updated = state.get_run(run.id)
    assert result == {
        "action": "skip_reply",
        "reason": "monica_loop_approval_not_ready",
        "text": (
            "I cannot start the fix from this approval because Monica is not ready for approved-PR mode: "
            "repo.url is missing"
        ),
    }
    assert updated is not None
    assert updated.status == "awaiting_fix_approval"
    assert updated.approved_by_user_id == ""
    assert launched == []


def test_slack_flow_tagged_approval_refuses_when_linear_api_key_is_missing(
    tmp_path,
    monkeypatch,
):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("U_MONICA",),
            approver_user_ids=("U_APPROVER",),
        ),
        linear=LinearConfig(team_id="team-id"),
        repo=RepoConfig(url="git@github.com:acme/mobile.git"),
        verification=VerificationConfig(commands=("npm test",)),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_TAGGER",
        request_text="can you fix this Android checkout crash?",
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)

    result = flow.handle_gateway_event(
        _slack_event(
            "approved, fix it",
            raw_text="<@U_MONICA> approved, fix it",
            user_id="U_APPROVER",
            thread_ts="1710000000.000100",
        )
    )

    updated = state.get_run(run.id)
    assert result == {
        "action": "skip_reply",
        "reason": "monica_loop_approval_not_ready",
        "text": (
            "I cannot start the fix from this approval because Monica is not ready for approved-PR mode: "
            "LINEAR_API_KEY is missing"
        ),
    }
    assert updated is not None
    assert updated.status == "awaiting_fix_approval"
    assert updated.approved_by_user_id == ""
    assert launched == []


def test_slack_flow_tagged_approval_refuses_when_slack_bot_token_is_missing(
    tmp_path,
    monkeypatch,
):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("MONICA_SLACK_BOT_TOKEN", raising=False)
    monkeypatch.setenv("LINEAR_API_KEY", "lin-key")
    monkeypatch.setattr(
        "plugins.mobile_bug_agent.slack_flow.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("U_MONICA",),
            approver_user_ids=("U_APPROVER",),
        ),
        linear=LinearConfig(team_id="team-id"),
        repo=RepoConfig(url="git@github.com:acme/mobile.git"),
        verification=VerificationConfig(commands=("npm test",)),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_TAGGER",
        request_text="can you fix this Android checkout crash?",
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)

    result = flow.handle_gateway_event(
        _slack_event(
            "approved, fix it",
            raw_text="<@U_MONICA> approved, fix it",
            user_id="U_APPROVER",
            thread_ts="1710000000.000100",
        )
    )

    updated = state.get_run(run.id)
    assert result == {
        "action": "skip_reply",
        "reason": "monica_loop_approval_not_ready",
            "text": (
                "I cannot start the fix from this approval because Monica is not ready for approved-PR mode: "
                "MONICA_SLACK_BOT_TOKEN is missing"
            ),
        }
    assert updated is not None
    assert updated.status == "awaiting_fix_approval"
    assert updated.approved_by_user_id == ""
    assert launched == []


def test_slack_flow_tagged_approval_refuses_chandler_worker_session_prefix(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("LINEAR_API_KEY", "lin-key")
    monkeypatch.setattr(
        "plugins.mobile_bug_agent.slack_flow.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("U_MONICA",),
            approver_user_ids=("U_APPROVER",),
        ),
        linear=LinearConfig(team_id="team-id"),
        repo=RepoConfig(url="git@github.com:acme/mobile.git"),
        runtime=RuntimeConfig(worker_session_prefix="chandler"),
        verification=VerificationConfig(commands=("npm test",)),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_TAGGER",
        request_text="can you fix this Android checkout crash?",
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)

    result = flow.handle_gateway_event(
        _slack_event(
            "approved, fix it",
            raw_text="<@U_MONICA> approved, fix it",
            user_id="U_APPROVER",
            thread_ts="1710000000.000100",
        )
    )

    updated = state.get_run(run.id)
    assert result == {
        "action": "skip_reply",
        "reason": "monica_loop_approval_not_ready",
        "text": (
            "I cannot start the fix from this approval because Monica is not ready for approved-PR mode: "
            "runtime.worker_session_prefix must include `monica` to keep worker sessions segregated"
        ),
    }
    assert updated is not None
    assert updated.status == "awaiting_fix_approval"
    assert updated.approved_by_user_id == ""
    assert launched == []


def test_slack_flow_tagged_approval_is_denied_when_no_approvers_are_configured(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("U_MONICA",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_TAGGER",
        request_text="can you fix this Android checkout crash?",
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)

    result = flow.handle_gateway_event(
        _slack_event(
            "approved, fix it",
            raw_text="<@U_MONICA> approved, fix it",
            user_id="U_RANDOM",
            thread_ts="1710000000.000100",
        )
    )

    updated = state.get_run(run.id)
    assert result == {
        "action": "skip_reply",
        "reason": "monica_loop_approval_denied",
        "text": (
            "I cannot start the fix from this approval. "
            "No Monica approver is configured for code changes."
        ),
    }
    assert updated is not None
    assert updated.status == "awaiting_fix_approval"
    assert updated.approved_by_user_id == ""
    assert launched == []


def test_slack_flow_requeues_completed_thread_for_linear_update(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("BMONICA",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_TAGGER",
        request_text="original Android checkout crash",
    )
    state.update_run(
        run.id,
        status="done",
        linear_identifier="MOB-123",
        linear_issue_id="issue-id",
        linear_url="https://linear.app/acme/issue/MOB-123",
        branch_name="monica/MOB-123-checkout-crash",
        base_branch="origin/dev",
        base_commit="abc1234",
        proof_deep_link="elixir-card://marketplace/offer/old-offer",
        proof_expected_text="Old Offer",
        proof_screen="/OldPdpScreen",
        pr_url="https://github.com/acme/mobile/pull/123",
        failure_reason="old failure",
        approved_by_user_id="U_OLD_APPROVER",
    )
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)

    result = flow.handle_gateway_event(
        _slack_event(
            "new reproduction detail: happens only after promo code",
            raw_text="<@BMONICA> new reproduction detail: happens only after promo code",
            user_id="U_REPRODUCER",
            thread_ts="1710000000.000100",
            message_ts="1710000002.000300",
        )
    )

    updated = state.get_run(run.id)
    assert result == {"action": "skip", "reason": "monica_loop_requeued"}
    assert updated is not None
    assert updated.status == "queued"
    assert updated.linear_issue_id == "issue-id"
    assert updated.linear_identifier == "MOB-123"
    assert updated.linear_url == "https://linear.app/acme/issue/MOB-123"
    assert updated.branch_name == ""
    assert updated.base_branch == ""
    assert updated.base_commit == ""
    assert updated.proof_deep_link == ""
    assert updated.proof_expected_text == ""
    assert updated.proof_screen == ""
    assert updated.pr_url == ""
    assert updated.failure_reason == ""
    assert updated.approved_by_user_id == ""
    assert updated.user_id == "U_REPRODUCER"
    assert updated.message_ts == "1710000002.000300"
    assert "new reproduction detail" in updated.request_text
    assert launched == [run.id]


def test_slack_flow_requeue_logs_breadcrumbs(tmp_path, caplog):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("BMONICA",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_TAGGER",
        request_text="original Android checkout crash",
    )
    state.update_run(
        run.id,
        status="done",
        linear_identifier="MOB-123",
        linear_issue_id="issue-id",
        linear_url="https://linear.app/acme/issue/MOB-123",
        branch_name="monica/MOB-123-checkout-crash",
        pr_url="https://github.com/acme/mobile/pull/123",
    )
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)

    with caplog.at_level(logging.INFO, logger="plugins.mobile_bug_agent.slack_flow"):
        result = flow.handle_gateway_event(
            _slack_event(
                "new reproduction detail: happens only after promo code",
                raw_text="<@BMONICA> new reproduction detail: happens only after promo code",
                user_id="U_REPRODUCER",
                thread_ts="1710000000.000100",
                message_ts="1710000002.000300",
            )
        )

    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert result == {"action": "skip", "reason": "monica_loop_requeued"}
    assert "event=requeued" in logs
    assert run.id in logs
    assert "C_MOBILE" in logs
    assert "1710000000.000100" in logs
    assert "MOB-123" in logs
    assert "https://linear.app/acme/issue/MOB-123" in logs
    assert "requeued_by=U_REPRODUCER" in logs
    assert launched == [run.id]


def test_slack_flow_does_not_requeue_completed_thread_for_tagged_thanks(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("BMONICA",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_TAGGER",
        request_text="original Android checkout crash",
    )
    state.update_run(
        run.id,
        status="done",
        linear_identifier="MOB-123",
        linear_issue_id="issue-id",
        linear_url="https://linear.app/acme/issue/MOB-123",
        pr_url="https://github.com/acme/mobile/pull/123",
    )
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)

    result = flow.handle_gateway_event(
        _slack_event(
            "thanks, this is fixed now",
            raw_text="<@BMONICA> thanks, this is fixed now",
            user_id="U_TAGGER",
            thread_ts="1710000000.000100",
            message_ts="1710000003.000400",
        )
    )

    updated = state.get_run(run.id)
    assert result == {"action": "skip", "reason": "monica_loop_noop"}
    assert updated is not None
    assert updated.status == "done"
    assert updated.pr_url == "https://github.com/acme/mobile/pull/123"
    assert updated.message_ts == "1710000000.000100"
    assert updated.request_text == "original Android checkout crash"
    assert launched == []


def test_slack_flow_does_not_requeue_completed_thread_when_thanks_mentions_bug(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("BMONICA",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_TAGGER",
        request_text="original Android checkout crash",
    )
    state.update_run(
        run.id,
        status="done",
        linear_identifier="MOB-123",
        linear_issue_id="issue-id",
        linear_url="https://linear.app/acme/issue/MOB-123",
        pr_url="https://github.com/acme/mobile/pull/123",
    )
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)

    result = flow.handle_gateway_event(
        _slack_event(
            "thanks, the Android checkout crash is fixed now",
            raw_text="<@BMONICA> thanks, the Android checkout crash is fixed now",
            user_id="U_TAGGER",
            thread_ts="1710000000.000100",
            message_ts="1710000003.000400",
        )
    )

    updated = state.get_run(run.id)
    assert result == {"action": "skip", "reason": "monica_loop_noop"}
    assert updated is not None
    assert updated.status == "done"
    assert updated.pr_url == "https://github.com/acme/mobile/pull/123"
    assert updated.message_ts == "1710000000.000100"
    assert updated.request_text == "original Android checkout crash"
    assert launched == []


def test_slack_flow_late_approval_resumes_completed_ticket_only_run(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("U_MONICA",),
            approver_user_ids=("U_APPROVER",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_TAGGER",
        request_text="original Android checkout crash",
    )
    state.update_run(
        run.id,
        status="done",
        linear_identifier="MOB-123",
        linear_issue_id="issue-id",
        linear_url="https://linear.app/acme/issue/MOB-123",
    )
    flow = MonicaSlackFlow(
        config=config,
        state=state,
        loop_launcher=launched.append,
        approval_readiness_checker=lambda: (True, ""),
    )

    result = flow.handle_gateway_event(
        _slack_event(
            "approved, fix it",
            raw_text="<@U_MONICA> approved, fix it",
            user_id="U_APPROVER",
            thread_ts="1710000000.000100",
            message_ts="1710000004.000500",
        )
    )

    updated = state.get_run(run.id)
    assert result == {"action": "skip", "reason": "monica_loop_approved"}
    assert updated is not None
    assert updated.status == "approved"
    assert updated.approved_by_user_id == "U_APPROVER"
    assert updated.linear_identifier == "MOB-123"
    assert updated.linear_issue_id == "issue-id"
    assert updated.linear_url == "https://linear.app/acme/issue/MOB-123"
    assert updated.request_text == "original Android checkout crash"
    assert launched == [run.id]


def test_slack_flow_late_approval_logs_breadcrumbs(tmp_path, caplog):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("U_MONICA",),
            approver_user_ids=("U_APPROVER",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_TAGGER",
        request_text="original Android checkout crash",
    )
    state.update_run(
        run.id,
        status="done",
        linear_identifier="MOB-123",
        linear_issue_id="issue-id",
        linear_url="https://linear.app/acme/issue/MOB-123",
    )
    flow = MonicaSlackFlow(
        config=config,
        state=state,
        loop_launcher=launched.append,
        approval_readiness_checker=lambda: (True, ""),
    )

    with caplog.at_level(logging.INFO, logger="plugins.mobile_bug_agent.slack_flow"):
        result = flow.handle_gateway_event(
            _slack_event(
                "approved, fix it",
                raw_text="<@U_MONICA> approved, fix it",
                user_id="U_APPROVER",
                thread_ts="1710000000.000100",
                message_ts="1710000004.000500",
            )
        )

    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert result == {"action": "skip", "reason": "monica_loop_approved"}
    assert "event=approved" in logs
    assert run.id in logs
    assert "C_MOBILE" in logs
    assert "1710000000.000100" in logs
    assert "MOB-123" in logs
    assert "https://linear.app/acme/issue/MOB-123" in logs
    assert "approved_by=U_APPROVER" in logs
    assert launched == [run.id]


def test_slack_flow_tagged_approval_resumes_blocked_linear_run(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("U_MONICA",),
            approver_user_ids=("U_APPROVER",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_TAGGER",
        request_text="original Android checkout crash",
    )
    state.update_run(
        run.id,
        status="blocked",
        linear_identifier="MOB-123",
        linear_issue_id="issue-id",
        linear_url="https://linear.app/acme/issue/MOB-123",
        branch_name="monica/MOB-123-checkout-crash",
        base_branch="origin/dev",
        base_commit="abc1234",
        proof_deep_link="elixir-card://marketplace/offer/old-offer",
        proof_expected_text="Old Offer",
        proof_screen="/OldPdpScreen",
        failure_reason="draft_pr_url_missing",
        approved_by_user_id="U_APPROVER",
    )
    flow = MonicaSlackFlow(
        config=config,
        state=state,
        loop_launcher=launched.append,
        approval_readiness_checker=lambda: (True, ""),
    )

    result = flow.handle_gateway_event(
        _slack_event(
            "approved, try again",
            raw_text="<@U_MONICA> approved, try again",
            user_id="U_APPROVER",
            thread_ts="1710000000.000100",
            message_ts="1710000005.000600",
        )
    )

    updated = state.get_run(run.id)
    assert result == {"action": "skip", "reason": "monica_loop_approved"}
    assert updated is not None
    assert updated.status == "approved"
    assert updated.linear_identifier == "MOB-123"
    assert updated.linear_issue_id == "issue-id"
    assert updated.linear_url == "https://linear.app/acme/issue/MOB-123"
    assert updated.branch_name == ""
    assert updated.base_branch == ""
    assert updated.base_commit == ""
    assert updated.proof_deep_link == ""
    assert updated.proof_expected_text == ""
    assert updated.proof_screen == ""
    assert updated.failure_reason == ""
    assert updated.approved_by_user_id == "U_APPROVER"
    assert updated.pr_url == ""
    assert updated.request_text == "original Android checkout crash"
    assert launched == [run.id]


def test_slack_flow_does_not_requeue_completed_pr_for_late_approval(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("BMONICA",),
            approver_user_ids=("U_APPROVER",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_TAGGER",
        request_text="original Android checkout crash",
    )
    state.update_run(
        run.id,
        status="done",
        linear_identifier="MOB-123",
        linear_issue_id="issue-id",
        linear_url="https://linear.app/acme/issue/MOB-123",
        branch_name="monica/MOB-123-checkout-crash",
        pr_url="https://github.com/acme/mobile/pull/123",
        approved_by_user_id="U_APPROVER",
    )
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)

    result = flow.handle_gateway_event(
        _slack_event(
            "approved, fix it",
            raw_text="<@BMONICA> approved, fix it",
            user_id="U_APPROVER",
            thread_ts="1710000000.000100",
            message_ts="1710000004.000500",
        )
    )

    updated = state.get_run(run.id)
    assert result == {"action": "skip", "reason": "monica_loop_already_done"}
    assert updated is not None
    assert updated.status == "done"
    assert updated.branch_name == "monica/MOB-123-checkout-crash"
    assert updated.pr_url == "https://github.com/acme/mobile/pull/123"
    assert updated.approved_by_user_id == "U_APPROVER"
    assert updated.message_ts == "1710000000.000100"
    assert updated.request_text == "original Android checkout crash"
    assert launched == []


def test_slack_flow_does_not_requeue_blocked_run_that_already_has_pr(tmp_path):
    launched: list[str] = []
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("U_MONICA",),
            approver_user_ids=("U_APPROVER",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_TAGGER",
        request_text="original Android checkout crash",
    )
    state.update_run(
        run.id,
        status="blocked",
        linear_identifier="MOB-123",
        linear_issue_id="issue-id",
        linear_url="https://linear.app/acme/issue/MOB-123",
        branch_name="monica/MOB-123-checkout-crash",
        pr_url="https://github.com/acme/mobile/pull/123",
        failure_reason="cancelled by U_APPROVER",
        approved_by_user_id="U_APPROVER",
    )
    flow = MonicaSlackFlow(config=config, state=state, loop_launcher=launched.append)

    result = flow.handle_gateway_event(
        _slack_event(
            "approved, fix it",
            raw_text="<@U_MONICA> approved, fix it",
            user_id="U_APPROVER",
            thread_ts="1710000000.000100",
            message_ts="1710000006.000700",
        )
    )

    updated = state.get_run(run.id)
    assert result == {"action": "skip", "reason": "monica_loop_already_done"}
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.branch_name == "monica/MOB-123-checkout-crash"
    assert updated.pr_url == "https://github.com/acme/mobile/pull/123"
    assert updated.failure_reason == "cancelled by U_APPROVER"
    assert updated.approved_by_user_id == "U_APPROVER"
    assert updated.message_ts == "1710000000.000100"
    assert updated.request_text == "original Android checkout crash"
    assert launched == []


@dataclass
class FakeSkills(MonicaLoopSkills):
    calls: list[str] = field(default_factory=list)
    status_posts: list[str] = field(default_factory=list)

    def read_slack_thread(self, run: Any) -> dict[str, Any]:
        self.calls.append("read_slack_thread")
        return {
            "permalink": "https://example.slack.com/archives/C_MOBILE/p1710000000000200",
            "messages": [
                "Alice: Android checkout crashes after applying a promo code.",
                "Bob: I reproduced on 2.14.1.",
            ],
            "attachments": ["screenshot.png"],
        }

    def infer_user_intent(self, run: Any, thread: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("infer_user_intent")
        return {
            "is_mobile_bug": True,
            "confidence": 0.91,
            "wants_fix": True,
            "needs_clarification": False,
            "summary": "Android checkout crashes after applying a promo code.",
        }

    def create_or_update_linear(self, run: Any, thread: dict[str, Any], intent: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("create_or_update_linear")
        return {
            "identifier": "DRY-RUN",
            "url": "",
            "dry_run": True,
            "title": "[Mobile] Android checkout crashes after promo code",
            "description": "## Summary\nAndroid checkout crashes after applying a promo code.",
        }

    def ask_fix_approval(self, run: Any, issue: dict[str, Any]) -> None:
        self.calls.append("ask_fix_approval")

    def post_status(self, run: Any, text: str) -> None:
        self.status_posts.append(text)

    def run_internal_codex_worker(self, run: Any) -> dict[str, Any]:
        self.calls.append("run_internal_codex_worker")
        raise AssertionError("Monica must not write code before approval")

    def run_verification(self, run: Any, worker_result: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("run_verification")
        return {"passed": True}

    def open_draft_pr(
        self,
        run: Any,
        worker_result: dict[str, Any],
        verification: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append("open_draft_pr")
        return {"url": "https://github.com/example/mobile/pull/123"}


def test_dry_run_finishes_without_waiting_for_approval_or_code(tmp_path):
    config = MonicaConfig(enabled=True, rollout_mode="dry_run")
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
    )
    skills = FakeSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "done"
    assert updated.linear_identifier == "DRY-RUN"
    assert skills.calls == [
        "read_slack_thread",
        "infer_user_intent",
        "create_or_update_linear",
    ]
    assert "Dry run" in skills.status_posts[0]
    assert "Preview:" in skills.status_posts[0]
    assert "## Summary" in skills.status_posts[0]


def test_loop_acquires_and_releases_loop_lease(tmp_path):
    config = MonicaConfig(enabled=True, rollout_mode="dry_run")
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
    )
    skills = FakeSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    assert state.current_loop_lease(run.id) is None
    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "done"


def test_loop_skips_when_live_loop_lease_exists(tmp_path):
    config = MonicaConfig(enabled=True, rollout_mode="dry_run")
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
    )
    lease, reason = state.acquire_loop_lease(
        run.id,
        owner_id="other-gateway",
        acquired_at=1781520000.0,
        ttl_seconds=300,
    )
    assert lease is not None
    assert reason == ""
    skills = FakeSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    assert skills.calls == []
    assert state.get_run(run.id) == run
    assert state.current_loop_lease(run.id) == lease


def test_approved_pr_refuses_state_run_without_tag_or_dm_intake(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_APPROVER",)),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event={
            "type": "message",
            "channel": "C_MOBILE",
            "channel_type": "channel",
            "text": "can you fix this marketplace PDP copy bug?",
            "ts": "1710000000.000200",
        },
    )
    skills = FakeSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "approved_pr_slack_intake_missing"
    assert skills.calls == []
    assert len(skills.status_posts) == 1
    assert "tagged Slack message or direct message" in skills.status_posts[0]
    assert "allowed Slack channel" in skills.status_posts[0]
    assert "before filing Linear, changing code, or opening a PR" in skills.status_posts[0]


def test_dry_run_logs_ticket_breadcrumbs(tmp_path, caplog):
    config = MonicaConfig(enabled=True, rollout_mode="dry_run")
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
    )
    skills = FakeSkills()

    with caplog.at_level(logging.INFO, logger="plugins.mobile_bug_agent.loop"):
        MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "event=done" in logs
    assert "stage=dry_run" in logs
    assert run.id in logs
    assert "C_MOBILE" in logs
    assert "1710000000.000100" in logs
    assert "DRY-RUN" in logs


@dataclass
class CancellingReadSkills(FakeSkills):
    state: MonicaState | None = None

    def read_slack_thread(self, run: Any) -> dict[str, Any]:
        self.calls.append("read_slack_thread")
        assert self.state is not None
        self.state.update_run(
            run.id,
            status="blocked",
            failure_reason="cancelled by U_APPROVER",
        )
        return {"permalink": "https://example.slack.com/thread", "messages": [run.request_text]}


def test_loop_stops_when_run_is_cancelled_during_triage(tmp_path):
    config = MonicaConfig(enabled=True, rollout_mode="linear_only")
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
    )
    skills = CancellingReadSkills(state=state)

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "cancelled by U_APPROVER"
    assert skills.calls == ["read_slack_thread"]


def test_unknown_rollout_mode_blocks_before_side_effects(tmp_path):
    config = MonicaConfig(enabled=True, rollout_mode="typo")
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you clean this Android checkout crash up?",
    )
    skills = FakeSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "unknown_rollout_mode: typo"
    assert skills.calls == []
    assert "known rollout mode" in skills.status_posts[0]


def test_unknown_rollout_mode_logs_blocked_breadcrumbs(tmp_path, caplog):
    config = MonicaConfig(enabled=True, rollout_mode="typo")
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you clean this Android checkout crash up?",
    )
    skills = FakeSkills()

    with caplog.at_level(logging.INFO, logger="plugins.mobile_bug_agent.loop"):
        MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "event=blocked" in logs
    assert "stage=preflight" in logs
    assert "failure_reason=unknown_rollout_mode: typo" in logs
    assert run.id in logs
    assert "C_MOBILE" in logs
    assert "1710000000.000100" in logs


def test_linear_only_blocks_when_linear_creation_is_disabled(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="linear_only",
        loop=LoopConfig(create_linear=False),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you clean this Android checkout crash up?",
    )
    skills = FakeSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "linear_creation_disabled_in_rollout"
    assert skills.calls == []
    assert "Linear creation is disabled" in skills.status_posts[0]


@dataclass
class ClarificationNeededSkills(FakeSkills):
    def infer_user_intent(self, run: Any, thread: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("infer_user_intent")
        return {
            "is_mobile_bug": True,
            "wants_linear": False,
            "wants_fix": False,
            "needs_clarification": True,
            "confidence": 0.52,
            "summary": "Android checkout report is missing reproduction details",
            "missing_questions": ["Which app build and device reproduced this?"],
        }

    def create_or_update_linear(self, run: Any, thread: dict[str, Any], intent: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("create_or_update_linear")
        raise AssertionError("Monica must not file Linear before clarification")


def test_clarification_needed_logs_triage_breadcrumbs(tmp_path, caplog):
    config = MonicaConfig(enabled=True, rollout_mode="linear_only")
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you look into this checkout thing?",
    )
    skills = ClarificationNeededSkills()

    with caplog.at_level(logging.INFO, logger="plugins.mobile_bug_agent.loop"):
        MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "event=needs_clarification" in logs
    assert "stage=triaging" in logs
    assert run.id in logs
    assert "C_MOBILE" in logs
    assert "1710000000.000100" in logs


@dataclass
class NonMobileBugSkills(FakeSkills):
    def infer_user_intent(self, run: Any, thread: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("infer_user_intent")
        return {
            "is_mobile_bug": False,
            "wants_linear": False,
            "wants_fix": False,
            "needs_clarification": False,
            "confidence": 0.24,
            "summary": "Backend webhook discussion",
            "reason": "The thread is about backend webhook delivery rather than the mobile app.",
        }

    def create_or_update_linear(self, run: Any, thread: dict[str, Any], intent: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("create_or_update_linear")
        raise AssertionError("Monica must not file Linear for non-mobile bugs")


def test_not_mobile_bug_logs_blocked_breadcrumbs(tmp_path, caplog):
    config = MonicaConfig(enabled=True, rollout_mode="linear_only")
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you clean up this webhook delivery bug?",
    )
    skills = NonMobileBugSkills()

    with caplog.at_level(logging.INFO, logger="plugins.mobile_bug_agent.loop"):
        MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "event=blocked" in logs
    assert "stage=triaging" in logs
    assert "failure_reason=not_a_mobile_bug" in logs
    assert run.id in logs
    assert "C_MOBILE" in logs
    assert "1710000000.000100" in logs


@dataclass
class QuestionOnlyBugSkills(FakeSkills):
    def infer_user_intent(self, run: Any, thread: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("infer_user_intent")
        return {
            "is_mobile_bug": True,
            "wants_linear": False,
            "wants_fix": False,
            "needs_clarification": False,
            "confidence": 0.86,
            "summary": "Android checkout crash discussion",
            "reason": "The thread is about a mobile bug, but the tag asks for thoughts only.",
        }

    def create_or_update_linear(self, run: Any, thread: dict[str, Any], intent: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("create_or_update_linear")
        raise AssertionError("Monica must not file Linear when the classifier says no action was requested")


def test_question_only_mobile_bug_tag_asks_for_clarification_without_filing(tmp_path):
    config = MonicaConfig(enabled=True, rollout_mode="linear_only")
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="any thoughts on this Android checkout crash?",
    )
    skills = QuestionOnlyBugSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "needs_clarification"
    assert updated.linear_identifier == ""
    assert skills.calls == ["read_slack_thread", "infer_user_intent"]
    assert "file a Linear issue or prepare a fix" in skills.status_posts[0]


@dataclass
class LinearOnlySkills(FakeSkills):
    def create_or_update_linear(self, run: Any, thread: dict[str, Any], intent: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("create_or_update_linear")
        return {
            "id": "issue-id",
            "identifier": "MOB-123",
            "url": "https://linear.app/acme/issue/MOB-123",
            "dry_run": False,
            "title": "[Mobile] Android checkout crashes after promo code",
        }


@dataclass
class ApprovedPrLinearSkills(LinearOnlySkills):
    def infer_user_intent(self, run: Any, thread: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("infer_user_intent")
        return {
            "is_mobile_bug": True,
            "confidence": 0.93,
            "wants_fix": True,
            "needs_clarification": False,
            "summary": "Marketplace PDP copy bug.",
            "observed_behavior": "The marketplace PDP promo copy uses the wrong wording.",
            "expected_behavior": "The marketplace PDP promo copy should match the approved text.",
            "reason": "The tagged request is a marketplace copy/design issue in the mobile app.",
        }


def test_linear_only_creates_ticket_without_waiting_for_code_approval(tmp_path):
    config = MonicaConfig(enabled=True, rollout_mode="linear_only")
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you clean this Android checkout crash up?",
    )
    skills = LinearOnlySkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "done"
    assert updated.linear_identifier == "MOB-123"
    assert skills.calls == [
        "read_slack_thread",
        "infer_user_intent",
        "create_or_update_linear",
    ]
    assert "Created Linear issue" in skills.status_posts[0]
    assert "Code fixes are disabled" in skills.status_posts[0]


def test_linear_only_logs_ticket_breadcrumbs(tmp_path, caplog):
    config = MonicaConfig(enabled=True, rollout_mode="linear_only")
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you clean this Android checkout crash up?",
    )
    skills = LinearOnlySkills()

    with caplog.at_level(logging.INFO, logger="plugins.mobile_bug_agent.loop"):
        MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "event=done" in logs
    assert "stage=linear_only" in logs
    assert run.id in logs
    assert "C_MOBILE" in logs
    assert "1710000000.000100" in logs
    assert "MOB-123" in logs
    assert "https://linear.app/acme/issue/MOB-123" in logs


def test_approved_pr_creates_ticket_then_waits_for_approval_before_code(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",)),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    skills = ApprovedPrLinearSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "awaiting_fix_approval"
    assert updated.linear_identifier == "MOB-123"
    assert skills.calls == [
        "read_slack_thread",
        "infer_user_intent",
        "create_or_update_linear",
        "ask_fix_approval",
    ]


def test_approved_pr_logs_awaiting_approval_breadcrumbs(tmp_path, caplog):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",)),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    skills = ApprovedPrLinearSkills()

    with caplog.at_level(logging.INFO, logger="plugins.mobile_bug_agent.loop"):
        MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "event=awaiting_fix_approval" in logs
    assert "stage=linear_created" in logs
    assert run.id in logs
    assert "C_MOBILE" in logs
    assert "1710000000.000100" in logs
    assert "MOB-123" in logs
    assert "https://linear.app/acme/issue/MOB-123" in logs


def test_approved_pr_refuses_out_of_scope_request_before_linear(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",)),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you clean this Android offer card crash up?",
        raw_event=_approved_pr_raw_event(),
    )
    skills = FakeSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "approved_pr_fix_scope_unsupported"
    assert updated.linear_identifier == ""
    assert skills.calls == ["read_slack_thread", "infer_user_intent"]
    assert "marketplace copy/design" in skills.status_posts[0]


def test_approved_pr_refuses_marketplace_crash_with_copy_terms_before_linear(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",)),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Android marketplace PDP crashes after loading promo copy, please fix it",
        raw_event=_approved_pr_raw_event(),
    )
    skills = FakeSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "approved_pr_fix_scope_unsupported"
    assert updated.linear_identifier == ""
    assert skills.calls == ["read_slack_thread", "infer_user_intent"]
    assert "marketplace copy/design" in skills.status_posts[0]


@dataclass
class PerformanceScopeSkills(FakeSkills):
    def infer_user_intent(self, run: Any, thread: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("infer_user_intent")
        return {
            "is_mobile_bug": True,
            "confidence": 0.92,
            "wants_fix": True,
            "needs_clarification": False,
            "summary": "Marketplace PDP layout is slow to load.",
            "observed_behavior": "The marketplace PDP layout has high latency before rendering.",
            "reason": "The tagged request is about performance, not marketplace copy/design.",
        }

    def create_or_update_linear(self, run: Any, thread: dict[str, Any], intent: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("create_or_update_linear")
        raise AssertionError("Monica must not file Linear for marketplace performance work")


def test_approved_pr_refuses_marketplace_performance_before_linear(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",)),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Android marketplace PDP layout is slow to load, please fix it",
        raw_event=_approved_pr_raw_event(),
    )
    skills = PerformanceScopeSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "approved_pr_fix_scope_unsupported"
    assert updated.linear_identifier == ""
    assert skills.calls == ["read_slack_thread", "infer_user_intent"]
    assert "marketplace copy/design" in skills.status_posts[0]


@dataclass
class IntentRevealsCrashSkills(FakeSkills):
    def infer_user_intent(self, run: Any, thread: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("infer_user_intent")
        return {
            "is_mobile_bug": True,
            "confidence": 0.94,
            "wants_fix": True,
            "needs_clarification": False,
            "summary": "Marketplace PDP copy update triggers a crash.",
            "observed_behavior": "The marketplace PDP crashes after the promo copy renders.",
            "reason": "The request mentions marketplace copy, but the thread describes a crash.",
        }

    def create_or_update_linear(self, run: Any, thread: dict[str, Any], intent: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("create_or_update_linear")
        raise AssertionError("Monica must not file Linear when triage intent reveals crash scope")


def test_approved_pr_refuses_scope_when_intent_reveals_crash_before_linear(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",)),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="please fix this marketplace PDP copy bug",
        raw_event=_approved_pr_raw_event(),
    )
    skills = IntentRevealsCrashSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "approved_pr_fix_scope_unsupported"
    assert updated.linear_identifier == ""
    assert skills.calls == ["read_slack_thread", "infer_user_intent"]
    assert "marketplace copy/design" in skills.status_posts[0]


def test_approved_pr_still_requires_tagged_approval_when_config_disables_gate(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",)),
        loop=LoopConfig(require_fix_approval=False),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    skills = ApprovedPrLinearSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "awaiting_fix_approval"
    assert updated.approved_by_user_id == ""
    assert skills.calls == [
        "read_slack_thread",
        "infer_user_intent",
        "create_or_update_linear",
        "ask_fix_approval",
    ]


def test_approved_pr_refuses_out_of_scope_code_fix_before_worker(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this Android offer card crash?",
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "approved_pr_fix_scope_unsupported"
    assert skills.calls == []
    assert "marketplace copy/design" in skills.status_posts[0]


def test_approved_pr_refuses_code_fix_without_recorded_approver_before_worker(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(approver_user_ids=("U_APPROVER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        status="approved",
    )
    state.update_run(run.id, linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    skills = ApprovedFixSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "approved_pr_approval_missing"
    assert skills.calls == []
    assert "explicit configured approver approval" in skills.status_posts[0]


def test_approved_pr_refuses_non_linear_issue_url_before_worker(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(
        run.id,
        status="awaiting_fix_approval",
        linear_identifier="MOB-123",
        linear_url="https://linear.example/MOB-123",
    )
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "linear_issue_url_invalid_before_fix"
    assert skills.calls == []
    assert "Linear issue URL" in skills.status_posts[0]


def test_approved_pr_refuses_code_fix_from_unconfigured_approver_before_worker(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(approver_user_ids=("U_APPROVER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event={
            "type": "message",
            "channel": "C_MOBILE",
            "text": "<@U_MONICA> can you fix this marketplace PDP copy bug?",
            "ts": "1710000000.000200",
        },
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_NOT_ALLOWED")
    skills = ApprovedFixSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "approved_pr_approver_not_configured"
    assert skills.calls == []
    assert "explicit configured approver approval" in skills.status_posts[0]


def test_approved_pr_refuses_code_fix_without_tagged_or_dm_intake_before_worker(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(
            bot_user_ids=("U_MONICA",),
            approver_user_ids=("U_TAGGER",),
        ),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
    )
    state.update_run(
        run.id,
        status="awaiting_fix_approval",
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
    )
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "approved_pr_slack_intake_missing"
    assert skills.calls == []
    assert "tagged Slack message or direct message" in skills.status_posts[0]


def test_approved_pr_refuses_code_fix_from_disallowed_channel_before_worker(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("U_MONICA",),
            approver_user_ids=("U_TAGGER",),
        ),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_OTHER",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event={
            "type": "app_mention",
            "channel": "C_OTHER",
            "channel_type": "channel",
            "text": "<@U_MONICA> can you fix this marketplace PDP copy bug?",
            "ts": "1710000000.000200",
        },
    )
    state.update_run(
        run.id,
        status="awaiting_fix_approval",
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
    )
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "approved_pr_slack_channel_not_allowed"
    assert updated.pr_url == ""
    assert skills.calls == []
    assert "tagged Slack message or direct message" in skills.status_posts[0]
    assert "allowed Slack channel" in skills.status_posts[0]


def test_approved_pr_refuses_generic_slack_message_when_bot_ids_are_unconfigured(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event={
            "type": "message",
            "channel": "C_MOBILE",
            "channel_type": "channel",
            "text": "can you fix this marketplace PDP copy bug?",
            "ts": "1710000000.000200",
        },
    )
    state.update_run(
        run.id,
        status="awaiting_fix_approval",
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
    )
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "approved_pr_slack_intake_missing"
    assert updated.pr_url == ""
    assert skills.calls == []
    assert "tagged Slack message or direct message" in skills.status_posts[0]


def test_approved_pr_refuses_app_mention_when_bot_ids_are_unconfigured_before_worker(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event={
            "type": "app_mention",
            "channel": "C_MOBILE",
            "text": "<@U_MONICA> can you fix this marketplace PDP copy bug?",
            "ts": "1710000000.000200",
        },
    )
    state.update_run(
        run.id,
        status="awaiting_fix_approval",
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
    )
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "approved_pr_slack_intake_missing"
    assert updated.pr_url == ""
    assert skills.calls == []
    assert "tagged Slack message or direct message" in skills.status_posts[0]


def test_approved_pr_refuses_app_mention_for_different_configured_bot_before_worker(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(
            bot_user_ids=("U_MONICA",),
            approver_user_ids=("U_TAGGER",),
        ),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event={
            "type": "app_mention",
            "channel": "C_MOBILE",
            "text": "<@U_CHANDLER> can you fix this marketplace PDP copy bug?",
            "ts": "1710000000.000200",
        },
    )
    state.update_run(
        run.id,
        status="awaiting_fix_approval",
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
    )
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "approved_pr_slack_intake_missing"
    assert updated.pr_url == ""
    assert skills.calls == []
    assert "tagged Slack message or direct message" in skills.status_posts[0]


def test_approved_pr_refuses_unprovable_slack_intake_when_bot_ids_are_unconfigured(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
    )
    state.update_run(
        run.id,
        status="awaiting_fix_approval",
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
    )
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "approved_pr_slack_intake_missing"
    assert updated.pr_url == ""
    assert skills.calls == []
    assert "tagged Slack message or direct message" in skills.status_posts[0]


@dataclass
class FailingLinearSkills(FakeSkills):
    def create_or_update_linear(self, run: Any, thread: dict[str, Any], intent: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("create_or_update_linear")
        raise RuntimeError("Linear write failed")


@dataclass
class SensitiveFailingLinearSkills(FakeSkills):
    def create_or_update_linear(self, run: Any, thread: dict[str, Any], intent: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("create_or_update_linear")
        raise RuntimeError("Linear write failed at /Users/ritik/.hermes/secrets with xoxb-secret-token")


def test_loop_marks_unexpected_linear_failure_with_stage(tmp_path):
    config = MonicaConfig(enabled=True, rollout_mode="linear_only")
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you clean this Android checkout crash up?",
    )
    skills = FailingLinearSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "failed"
    assert updated.failure_reason == "creating_linear_failed: Linear write failed"
    assert "Stage: creating_linear" in skills.status_posts[0]


def test_loop_failure_status_does_not_post_sensitive_exception_details(tmp_path):
    config = MonicaConfig(enabled=True, rollout_mode="linear_only")
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you clean this Android checkout crash up?",
    )
    skills = SensitiveFailingLinearSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "failed"
    assert updated.failure_reason == (
        "creating_linear_failed: Linear write failed at /Users/ritik/.hermes/secrets "
        "with xoxb-secret-token"
    )
    assert "Stage: creating_linear" in skills.status_posts[0]
    assert "/Users/ritik/.hermes/secrets" not in skills.status_posts[0]
    assert "xoxb-secret-token" not in skills.status_posts[0]
    assert "Check Monica logs or `hermes mobile-bug-agent show" in skills.status_posts[0]


@dataclass
class ApprovedFixSkills(FakeSkills):
    verification_passed: bool = True
    proof_passed: bool = True
    worker_proof_deep_link: str = "elixir-card://marketplace/offer/fitness-first"
    worker_proof_expected_text: str = "Fitness First"
    worker_proof_screen: str = ""
    include_proof_manifest: bool = True
    proof_manifest_artifact: str = "/tmp/monica-proof/monica-proof-manifest.json"
    proof_manifest_branch_name: str | None = None
    proof_manifest_base_commit: str | None = None
    proof_manifest_base_ref: str | None = None
    proof_manifest_worktree: str | None = None
    proof_manifest_run_id: str | None = None
    proof_manifest_linear_identifier: str | None = None
    proof_manifest_linear_url: str | None = None
    proof_manifest_target: dict[str, str] | None = None
    proof_manifest_omit_keys: frozenset[str] = frozenset()
    proof_setup_commands: tuple[str, ...] = ("npm run monica:seed-auth",)
    proof_commands: tuple[str, ...] = ("npm run monica:proof",)
    proof_required_env_keys: tuple[str, ...] = ("MONICA_TEST_LOGIN_TOKEN",)
    proof_platforms: tuple[str, ...] = ("ios", "android")
    proof_manifest_platforms: tuple[str, ...] | None = None
    proof_manifest_setup_commands: tuple[str, ...] | None = None
    proof_manifest_commands: tuple[str, ...] | None = None
    proof_manifest_required_env_keys: tuple[str, ...] | None = None
    proof_manifest_proof_artifacts: tuple[str, ...] | None = None
    proof_route: str = "/MarketplacePdp"
    proof_artifacts: tuple[str, ...] = (
        "/tmp/monica-proof/ios-screenshot.png",
        "/tmp/monica-proof/android-screenshot.png",
        "/tmp/monica-proof/ios-target.log",
        "/tmp/monica-proof/android-ui.xml",
        "/tmp/monica-proof/ios-metro.stdout.log",
        "/tmp/monica-proof/android-metro.stdout.log",
    )
    proof_shareable_artifacts: tuple[dict[str, str], ...] = (
        {
            "platform": "ios",
            "path": "/tmp/monica-proof/ios-screenshot.png",
            "url": "https://slack.example/files/ios-screenshot.png",
        },
        {
            "platform": "android",
            "path": "/tmp/monica-proof/android-screenshot.png",
            "url": "https://slack.example/files/android-screenshot.png",
        },
    )
    create_proof_artifacts: bool = True
    proof_target: dict[str, str] = field(
        default_factory=lambda: {
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Fitness First",
        }
    )
    opened_pr_worker_result: dict[str, Any] = field(default_factory=dict)
    proof_worker_result: dict[str, Any] = field(default_factory=dict)
    share_calls: list[str] = field(default_factory=list)

    def run_internal_codex_worker(self, run: Any) -> dict[str, Any]:
        self.calls.append("run_internal_codex_worker")
        result: dict[str, Any] = {
            "branch_name": "monica/MOB-123-checkout-crash",
            "base_ref": "origin/main",
            "base_commit": "abc1234",
            "changed": True,
        }
        if self.worker_proof_deep_link:
            result["proof_deep_link"] = self.worker_proof_deep_link
        if self.worker_proof_expected_text:
            result["proof_expected_text"] = self.worker_proof_expected_text
        if self.worker_proof_screen:
            result["proof_screen"] = self.worker_proof_screen
        return result

    def run_verification(self, run: Any, worker_result: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("run_verification")
        return {
            "passed": self.verification_passed,
            "summary": "npm test",
            "output": "$ npm test\nok",
            "commands": ["npm test"],
        }

    def run_proof(
        self,
        run: Any,
        worker_result: dict[str, Any],
        verification: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append("run_proof")
        self.proof_worker_result = dict(worker_result)
        artifacts = list(self.proof_artifacts)
        if self.include_proof_manifest and artifacts:
            artifacts.insert(0, self.proof_manifest_artifact)
        if self.create_proof_artifacts:
            for artifact in artifacts:
                path = Path(artifact)
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.name == "monica-proof-manifest.json":
                    manifest_payload = {
                        "run_id": self.proof_manifest_run_id or run.id,
                        "linear_identifier": self.proof_manifest_linear_identifier
                        or str(getattr(run, "linear_identifier", "") or ""),
                        "linear_url": self.proof_manifest_linear_url
                        or str(getattr(run, "linear_url", "") or ""),
                        "branch_name": self.proof_manifest_branch_name
                        or str(worker_result.get("branch_name") or ""),
                        "base_commit": self.proof_manifest_base_commit
                        or str(worker_result.get("base_commit") or ""),
                        "base_ref": self.proof_manifest_base_ref
                        or str(worker_result.get("base_ref") or ""),
                        "worktree": self.proof_manifest_worktree
                        or str(worker_result.get("worktree_path") or worker_result.get("worktree") or ""),
                        "proof_target": self.proof_manifest_target or self.proof_target,
                        "setup_commands": list(
                            self.proof_manifest_setup_commands or self.proof_setup_commands
                        ),
                        "commands": list(self.proof_manifest_commands or self.proof_commands),
                        "required_env_keys": list(
                            self.proof_manifest_required_env_keys
                            if self.proof_manifest_required_env_keys is not None
                            else self.proof_required_env_keys
                        ),
                        "platforms": list(self.proof_manifest_platforms or self.proof_platforms),
                        "proof_artifacts": list(
                            self.proof_manifest_proof_artifacts
                            if self.proof_manifest_proof_artifacts is not None
                            else self.proof_artifacts
                        ),
                    }
                    for key in self.proof_manifest_omit_keys:
                        manifest_payload.pop(key, None)
                    path.write_text(json.dumps(manifest_payload), encoding="utf-8")
                else:
                    if path.suffix.lower() in {".html", ".json", ".log", ".txt", ".xml"}:
                        expected_text = str(self.proof_target.get("expected_text") or "")
                        if path.name in {"ios-metro.stdout.log", "android-metro.stdout.log"}:
                            path.write_text(
                                "LOG  [APP-PERF-METRIC] ui.load | "
                                f"screen.load {self.proof_route} | 180ms | ok",
                                encoding="utf-8",
                            )
                        else:
                            path.write_text(f"visible target text: {expected_text}", encoding="utf-8")
                    else:
                        path.write_bytes(b"proof")
        return {
            "passed": self.proof_passed,
            "summary": "Simulator proof captured." if self.proof_passed else "simctl is unavailable",
            "artifacts": artifacts,
            "proof_target": dict(self.proof_target),
            "setup_commands": list(self.proof_setup_commands),
            "commands": list(self.proof_commands),
            "required_env_keys": list(self.proof_required_env_keys),
            "platforms": list(self.proof_platforms),
            "output": "",
        }

    def share_proof_artifacts(self, run: Any, proof: dict[str, Any]) -> dict[str, Any]:
        self.share_calls.append("share_proof_artifacts")
        return {"shareable_artifacts": [dict(item) for item in self.proof_shareable_artifacts]}

    def open_draft_pr(
        self,
        run: Any,
        worker_result: dict[str, Any],
        verification: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append("open_draft_pr")
        self.opened_pr_worker_result = dict(worker_result)
        return {"url": "https://github.com/example/mobile/pull/123"}


class ApprovedFixSkillsMissingBase(ApprovedFixSkills):
    def run_internal_codex_worker(self, run: Any) -> dict[str, Any]:
        self.calls.append("run_internal_codex_worker")
        return {
            "branch_name": "monica/MOB-123-checkout-crash",
            "changed": True,
            "proof_deep_link": "elixir-card://marketplace/offer/fitness-first",
            "proof_expected_text": "Fitness First",
        }


class ApprovedFixSkillsBadBaseCommit(ApprovedFixSkills):
    def run_internal_codex_worker(self, run: Any) -> dict[str, Any]:
        result = super().run_internal_codex_worker(run)
        result["base_commit"] = "abc123base"
        return result


class ApprovedFixSkillsWrongBaseRef(ApprovedFixSkills):
    def run_internal_codex_worker(self, run: Any) -> dict[str, Any]:
        result = super().run_internal_codex_worker(run)
        result["base_ref"] = "origin/dev"
        return result


class ApprovedFixSkillsWithWorktree(ApprovedFixSkills):
    def run_internal_codex_worker(self, run: Any) -> dict[str, Any]:
        result = super().run_internal_codex_worker(run)
        result["worktree_path"] = "/tmp/monica-worktrees/monica-MOB-123-checkout-crash"
        return result


class ApprovedFixSkillsFailingFinalStatus(ApprovedFixSkills):
    def post_status(self, run: Any, text: str) -> None:
        if "Draft PR is ready:" in text:
            raise RuntimeError("Slack post failed")
        super().post_status(run, text)


class ApprovedFixSkillsFalseFinalStatus(ApprovedFixSkills):
    def post_status(self, run: Any, text: str) -> bool | None:
        if "Draft PR is ready:" in text:
            return False
        super().post_status(run, text)
        return None


class ApprovedFixSkillsGenericVerificationSummary(ApprovedFixSkills):
    def run_verification(self, run: Any, worker_result: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("run_verification")
        return {
            "passed": True,
            "summary": "Verification passed.",
            "commands": ["npm test"],
        }


class ApprovedFixSkillsNoVerificationEvidence(ApprovedFixSkills):
    def run_verification(self, run: Any, worker_result: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("run_verification")
        return {
            "passed": True,
            "summary": "Verification passed.",
        }


def _approved_pr_proof_config(**overrides: Any) -> ProofConfig:
    values: dict[str, Any] = {
        "commands": ("npm run monica:proof",),
        "setup_commands": ("npm run monica:seed-auth",),
        "required_env_keys": ("MONICA_TEST_LOGIN_TOKEN",),
    }
    values.update(overrides)
    return ProofConfig(**values)


@dataclass
class ApprovedPrEndToEndSkills(ApprovedFixSkills):
    def infer_user_intent(self, run: Any, thread: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("infer_user_intent")
        return {
            "is_mobile_bug": True,
            "confidence": 0.96,
            "wants_fix": True,
            "needs_clarification": False,
            "summary": "Marketplace PDP copy is wrong.",
            "observed_behavior": "Fitness First offer detail page uses stale copy.",
            "expected_behavior": "The offer detail page should show the approved Fitness First copy.",
        }

    def create_or_update_linear(self, run: Any, thread: dict[str, Any], intent: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("create_or_update_linear")
        return {
            "id": "issue-id",
            "identifier": "MOB-123",
            "url": "https://linear.app/acme/issue/MOB-123",
            "dry_run": False,
            "title": "[Mobile] Marketplace PDP copy is wrong",
        }


def test_approved_pr_slack_tag_to_approval_to_proof_to_draft_pr_regression(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("U_MONICA",),
            approver_user_ids=("U_APPROVER",),
        ),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    skills = ApprovedPrEndToEndSkills()

    def launch(run_id: str) -> None:
        MonicaLoop(config=config, state=state, skills=skills).run(run_id)

    flow = MonicaSlackFlow(
        config=config,
        state=state,
        loop_launcher=launch,
        approval_readiness_checker=lambda: (True, ""),
    )

    queued = flow.handle_gateway_event(
        _slack_event(
            "can you fix this marketplace PDP copy bug?",
            raw_text="<@U_MONICA> can you fix this marketplace PDP copy bug?",
            raw_type="app_mention",
            user_id="U_REPORTER",
        )
    )

    runs = state.list_runs()
    assert queued == {"action": "skip", "reason": "monica_loop_queued"}
    assert len(runs) == 1
    waiting = state.get_run(runs[0].id)
    assert waiting is not None
    assert waiting.status == "awaiting_fix_approval"
    assert waiting.linear_identifier == "MOB-123"
    assert waiting.pr_url == ""
    assert waiting.approved_by_user_id == ""
    assert skills.calls == [
        "read_slack_thread",
        "infer_user_intent",
        "create_or_update_linear",
        "ask_fix_approval",
    ]

    approved = flow.handle_gateway_event(
        _slack_event(
            "approved, fix it",
            raw_text="<@U_MONICA> approved, fix it",
            user_id="U_APPROVER",
            thread_ts="1710000000.000100",
            message_ts="1710000001.000300",
        )
    )

    done = state.get_run(runs[0].id)
    assert approved == {"action": "skip", "reason": "monica_loop_approved"}
    assert done is not None
    assert done.status == "done"
    assert done.approved_by_user_id == "U_APPROVER"
    assert done.branch_name == "monica/MOB-123-checkout-crash"
    assert done.base_branch == "origin/main"
    assert done.base_commit == "abc1234"
    assert done.pr_url == "https://github.com/example/mobile/pull/123"
    assert skills.calls == [
        "read_slack_thread",
        "infer_user_intent",
        "create_or_update_linear",
        "ask_fix_approval",
        "run_internal_codex_worker",
        "run_verification",
        "run_proof",
        "open_draft_pr",
    ]
    assert skills.share_calls == ["share_proof_artifacts"]
    assert skills.opened_pr_worker_result["proof"]["shareable_artifacts"] == [
        {
            "platform": "ios",
            "path": "/tmp/monica-proof/ios-screenshot.png",
            "url": "https://slack.example/files/ios-screenshot.png",
        },
        {
            "platform": "android",
            "path": "/tmp/monica-proof/android-screenshot.png",
            "url": "https://slack.example/files/android-screenshot.png",
        },
    ]
    final_post = skills.status_posts[-1]
    assert "Draft PR is ready: https://github.com/example/mobile/pull/123" in final_post
    assert "Linear: https://linear.app/acme/issue/MOB-123" in final_post
    assert "Base: origin/main @ abc1234" in final_post
    assert "Verification: npm test" in final_post
    assert "Proof target: elixir-card://marketplace/offer/fitness-first" in final_post
    assert "expected text: Fitness First" in final_post
    assert "https://slack.example/files/ios-screenshot.png" in final_post
    assert "https://slack.example/files/android-screenshot.png" in final_post


def test_approved_pr_dm_to_approval_to_proof_to_draft_pr_regression(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(
            allowed_channels=("C_MOBILE",),
            bot_user_ids=("U_MONICA",),
            approver_user_ids=("U_APPROVER",),
        ),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    skills = ApprovedPrEndToEndSkills()

    def launch(run_id: str) -> None:
        MonicaLoop(config=config, state=state, skills=skills).run(run_id)

    flow = MonicaSlackFlow(
        config=config,
        state=state,
        loop_launcher=launch,
        approval_readiness_checker=lambda: (True, ""),
    )

    queued = flow.handle_gateway_event(
        _slack_event(
            "can you fix this marketplace PDP copy bug?",
            raw_text="can you fix this marketplace PDP copy bug?",
            raw_type="message",
            raw_channel_type="im",
            channel_id="D_MONICA",
            chat_type="dm",
            user_id="U_REPORTER",
        )
    )

    runs = state.list_runs()
    assert queued == {"action": "skip", "reason": "monica_loop_queued"}
    assert len(runs) == 1
    waiting = state.get_run(runs[0].id)
    assert waiting is not None
    assert waiting.status == "awaiting_fix_approval"
    assert waiting.channel_id == "D_MONICA"
    assert waiting.raw_event is not None
    assert waiting.raw_event["channel_type"] == "im"
    assert waiting.linear_identifier == "MOB-123"
    assert waiting.pr_url == ""
    assert skills.calls == [
        "read_slack_thread",
        "infer_user_intent",
        "create_or_update_linear",
        "ask_fix_approval",
    ]

    approved = flow.handle_gateway_event(
        _slack_event(
            "approved, fix it",
            raw_text="approved, fix it",
            raw_type="message",
            raw_channel_type="im",
            channel_id="D_MONICA",
            chat_type="dm",
            user_id="U_APPROVER",
            thread_ts="1710000000.000100",
            message_ts="1710000001.000300",
        )
    )

    done = state.get_run(runs[0].id)
    assert approved == {"action": "skip", "reason": "monica_loop_approved"}
    assert done is not None
    assert done.status == "done"
    assert done.approved_by_user_id == "U_APPROVER"
    assert done.branch_name == "monica/MOB-123-checkout-crash"
    assert done.base_branch == "origin/main"
    assert done.base_commit == "abc1234"
    assert done.pr_url == "https://github.com/example/mobile/pull/123"
    assert skills.calls == [
        "read_slack_thread",
        "infer_user_intent",
        "create_or_update_linear",
        "ask_fix_approval",
        "run_internal_codex_worker",
        "run_verification",
        "run_proof",
        "open_draft_pr",
    ]
    assert skills.share_calls == ["share_proof_artifacts"]
    final_post = skills.status_posts[-1]
    assert "Draft PR is ready: https://github.com/example/mobile/pull/123" in final_post
    assert "Linear: https://linear.app/acme/issue/MOB-123" in final_post
    assert "Base: origin/main @ abc1234" in final_post
    assert "Verification: npm test" in final_post
    assert "Proof target: elixir-card://marketplace/offer/fitness-first" in final_post
    assert "expected text: Fitness First" in final_post
    assert "https://slack.example/files/ios-screenshot.png" in final_post
    assert "https://slack.example/files/android-screenshot.png" in final_post


@pytest.mark.parametrize("status", ("approved", "proof_blocked", "proofing"))
def test_approved_pr_loop_refuses_to_resume_run_that_already_has_pr_url(
    tmp_path,
    status,
):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(
        run.id,
        status=status,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        branch_name="monica/MOB-123-checkout-crash",
        base_branch="origin/dev",
        base_commit="abc1234",
        proof_deep_link="elixir-card://marketplace/offer/fitness-first",
        proof_expected_text="Fitness First",
        pr_url="https://github.com/example/mobile/pull/123",
        approved_by_user_id="U_TAGGER",
    )
    skills = ApprovedFixSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "draft_pr_already_exists"
    assert updated.pr_url == "https://github.com/example/mobile/pull/123"
    assert skills.calls == []
    assert skills.share_calls == []
    assert "already has a draft PR" in skills.status_posts[0]


def test_approved_run_writes_code_then_opens_draft_pr(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(required_env_keys=("MONICA_TEST_LOGIN_TOKEN",)),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(
        run.id,
        status="awaiting_fix_approval",
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
    )
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills(proof_required_env_keys=("MONICA_TEST_LOGIN_TOKEN",))

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "done"
    assert updated.branch_name == "monica/MOB-123-checkout-crash"
    assert updated.base_branch == "origin/main"
    assert updated.base_commit == "abc1234"
    assert updated.pr_url == "https://github.com/example/mobile/pull/123"
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof", "open_draft_pr"]
    assert skills.share_calls == ["share_proof_artifacts"]
    assert skills.opened_pr_worker_result["proof"]["shareable_artifacts"] == [
        {
            "platform": "ios",
            "path": "/tmp/monica-proof/ios-screenshot.png",
            "url": "https://slack.example/files/ios-screenshot.png",
        },
        {
            "platform": "android",
            "path": "/tmp/monica-proof/android-screenshot.png",
            "url": "https://slack.example/files/android-screenshot.png",
        },
    ]
    final_post = skills.status_posts[-1]
    assert "Draft PR is ready: https://github.com/example/mobile/pull/123" in final_post
    assert "Linear: https://linear.app/acme/issue/MOB-123" in final_post
    assert "Base: origin/main @ abc1234" in final_post
    assert "Verification: npm test" in final_post
    assert "https://slack.example/files/ios-screenshot.png" in final_post
    assert "https://slack.example/files/android-screenshot.png" in final_post
    assert "iOS: https://slack.example/files/ios-screenshot.png" in final_post
    assert "Android: https://slack.example/files/android-screenshot.png" in final_post
    assert "Proof target: elixir-card://marketplace/offer/fitness-first" in final_post
    assert "expected text: Fitness First" in final_post
    assert "required env keys: MONICA_TEST_LOGIN_TOKEN" in final_post


def test_approved_pr_normalizes_configured_required_env_keys_before_pr(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(
            required_env_keys=(
                " MONICA_TEST_LOGIN_TOKEN ",
                "MONICA_TEST_LOGIN_TOKEN",
                " ",
            )
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(
        run.id,
        status="awaiting_fix_approval",
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
    )
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills(proof_required_env_keys=("MONICA_TEST_LOGIN_TOKEN",))

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "done"
    assert updated.pr_url == "https://github.com/example/mobile/pull/123"
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof", "open_draft_pr"]
    assert skills.share_calls == ["share_proof_artifacts"]
    assert skills.opened_pr_worker_result["proof"]["required_env_keys"] == [
        "MONICA_TEST_LOGIN_TOKEN"
    ]


def test_approved_run_does_not_mark_done_when_final_slack_update_fails(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(
        run.id,
        status="awaiting_fix_approval",
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
    )
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkillsFailingFinalStatus()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.pr_url == "https://github.com/example/mobile/pull/123"
    assert updated.failure_reason == "final_slack_update_failed"
    assert not any("Draft PR is ready" in post for post in skills.status_posts)


def test_approved_run_does_not_mark_done_when_final_slack_update_returns_false(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(
        run.id,
        status="awaiting_fix_approval",
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
    )
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkillsFalseFinalStatus()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.pr_url == "https://github.com/example/mobile/pull/123"
    assert updated.failure_reason == "final_slack_update_failed"
    assert not any("Draft PR is ready" in post for post in skills.status_posts)


def test_approved_run_final_slack_status_includes_verification_commands_when_summary_is_generic(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(
        run.id,
        status="awaiting_fix_approval",
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
    )
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkillsGenericVerificationSummary()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    final_post = skills.status_posts[-1]
    assert "Verification: npm test" in final_post


def test_approved_pr_blocks_before_proof_when_verification_lacks_command_evidence(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkillsNoVerificationEvidence()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "verification_evidence_missing"
    assert updated.pr_url == ""
    assert skills.calls == ["run_internal_codex_worker", "run_verification"]
    assert skills.share_calls == []
    assert "verification command evidence" in skills.status_posts[0]


def test_approved_pr_blocks_before_verification_when_worker_base_metadata_is_missing(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong, please fix it",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkillsMissingBase()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "approved_pr_base_metadata_missing"
    assert updated.pr_url == ""
    assert skills.calls == ["run_internal_codex_worker"]
    assert skills.share_calls == []
    assert "fresh mobile base commit" in skills.status_posts[0]


def test_approved_pr_blocks_before_verification_when_worker_base_commit_is_not_sha(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong, please fix it",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(
        run.id,
        status="awaiting_fix_approval",
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
    )
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkillsBadBaseCommit()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "approved_pr_base_commit_invalid"
    assert updated.base_branch == ""
    assert updated.base_commit == ""
    assert updated.pr_url == ""
    assert skills.calls == ["run_internal_codex_worker"]
    assert skills.share_calls == []
    assert "fresh mobile base commit" in skills.status_posts[0]


def test_approved_pr_blocks_before_verification_when_worker_base_ref_mismatches_config(
    tmp_path,
):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong, please fix it",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(
        run.id,
        status="awaiting_fix_approval",
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
    )
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkillsWrongBaseRef()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "approved_pr_base_ref_mismatch"
    assert updated.base_branch == ""
    assert updated.base_commit == ""
    assert updated.pr_url == ""
    assert skills.calls == ["run_internal_codex_worker"]
    assert skills.share_calls == []
    assert "fresh mobile base commit" in skills.status_posts[0]


def test_approved_pr_blocks_before_proof_when_proof_commands_are_empty(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong, please fix it",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: proof.commands is empty"
    assert skills.calls == ["run_internal_codex_worker", "run_verification"]
    assert skills.share_calls == []


def test_approved_pr_blocks_before_proof_when_setup_commands_are_empty(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=ProofConfig(commands=("npm run monica:proof",)),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong, please fix it",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: proof.setup_commands is empty"
    assert skills.calls == ["run_internal_codex_worker", "run_verification"]
    assert skills.share_calls == []


def test_approved_pr_blocks_before_proof_when_required_env_keys_are_empty(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(required_env_keys=()),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong, please fix it",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(
        run.id,
        status="awaiting_fix_approval",
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
    )
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills(proof_required_env_keys=())

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: proof.required_env_keys is empty"
    assert skills.calls == ["run_internal_codex_worker", "run_verification"]
    assert skills.share_calls == []


def test_approved_pr_blocks_before_proof_when_setup_commands_are_noop(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(setup_commands=("true", "exit 0")),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong, please fix it",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(
        run.id,
        status="awaiting_fix_approval",
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
    )
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: proof.setup_commands contains only no-op commands"
    assert skills.calls == ["run_internal_codex_worker", "run_verification"]
    assert skills.share_calls == []


@pytest.mark.parametrize(
    ("proof_config", "expected_reason"),
    (
        (
            _approved_pr_proof_config(setup_commands=("<auth/session seed command>",)),
            "proof_unavailable: proof.setup_commands contains a placeholder",
        ),
        (
            _approved_pr_proof_config(commands=("<simulator proof command>",)),
            "proof_unavailable: proof.commands contains a placeholder",
        ),
        (
            _approved_pr_proof_config(
                setup_commands=("MONICA_TEST_LOGIN_TOKEN=secret npm run monica:seed-auth",)
            ),
            (
                "proof_unavailable: proof.setup_commands must not inline secret "
                "env assignment(s): MONICA_TEST_LOGIN_TOKEN"
            ),
        ),
        (
            _approved_pr_proof_config(
                commands=("MONICA_TEST_LOGIN_OTP=123456 npm run monica:proof",)
            ),
            (
                "proof_unavailable: proof.commands must not inline secret "
                "env assignment(s): MONICA_TEST_LOGIN_OTP"
            ),
        ),
    ),
)
def test_approved_pr_blocks_before_proof_when_proof_config_is_not_real_command(
    tmp_path, proof_config, expected_reason
):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=proof_config,
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong, please fix it",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(
        run.id,
        status="awaiting_fix_approval",
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
    )
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == expected_reason
    assert skills.calls == ["run_internal_codex_worker", "run_verification"]
    assert skills.share_calls == []


@pytest.mark.parametrize(
    ("proof_config", "expected_reason"),
    (
        (
            _approved_pr_proof_config(
                setup_commands=("npm run monica:seed-auth -- --token profile-secret",)
            ),
            (
                "proof_unavailable: proof.setup_commands must not include literal "
                "values from proof.required_env_keys: MONICA_TEST_LOGIN_TOKEN"
            ),
        ),
        (
            _approved_pr_proof_config(
                commands=("npm run monica:proof -- --token=profile-secret",)
            ),
            (
                "proof_unavailable: proof.commands must not include literal "
                "values from proof.required_env_keys: MONICA_TEST_LOGIN_TOKEN"
            ),
        ),
    ),
)
def test_approved_pr_blocks_before_proof_when_commands_embed_required_env_values(
    tmp_path, monkeypatch, proof_config, expected_reason
):
    monkeypatch.setenv("MONICA_TEST_LOGIN_TOKEN", "profile-secret")
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=proof_config,
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong, please fix it",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(
        run.id,
        status="awaiting_fix_approval",
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
    )
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == expected_reason
    assert "profile-secret" not in updated.failure_reason
    assert skills.calls == ["run_internal_codex_worker", "run_verification"]
    assert skills.share_calls == []


def test_approved_pr_blocks_before_proof_when_proof_commands_are_noop(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(commands=("true", "exit 0")),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong, please fix it",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(
        run.id,
        status="awaiting_fix_approval",
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
    )
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: proof.commands contains only no-op commands"
    assert skills.calls == ["run_internal_codex_worker", "run_verification"]
    assert skills.share_calls == []


def test_approved_pr_blocks_before_proof_when_platform_order_omits_android(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(platform_order=("ios",)),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong, please fix it",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(
        run.id,
        status="awaiting_fix_approval",
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
    )
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == (
        "proof_unavailable: proof.platform_order must include both ios and android: missing android"
    )
    assert skills.calls == ["run_internal_codex_worker", "run_verification"]
    assert skills.share_calls == []


def test_local_fix_only_run_writes_code_and_stops_before_pr(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="local_fix_only",
        verification=VerificationConfig(commands=("npm test",)),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this Android checkout crash?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills(proof_artifacts=("/tmp/monica-proof/screenshot.png",))

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "done"
    assert updated.branch_name == "monica/MOB-123-checkout-crash"
    assert updated.pr_url == ""
    assert skills.calls == ["run_internal_codex_worker", "run_verification"]
    assert "Local fix is ready" in skills.status_posts[0]
    assert "not pushed" in skills.status_posts[0]


def test_local_fix_only_requires_proof_before_done_when_configured(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="local_fix_only",
        verification=VerificationConfig(commands=("npm test",)),
        proof=ProofConfig(enabled=True, required_for_done=True),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this Android checkout crash?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills(proof_passed=False, proof_artifacts=())

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.branch_name == "monica/MOB-123-checkout-crash"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: simctl is unavailable"
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert "Verification passed" in skills.status_posts[0]
    assert "proof is unavailable" in skills.status_posts[0]
    assert "not mark this run done" in skills.status_posts[0]


class RaisingProofSkills(ApprovedFixSkills):
    def run_proof(
        self,
        run: Any,
        worker_result: dict[str, Any],
        verification: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append("run_proof")
        raise RuntimeError("simulator proof crashed before capture")


class ProofSetupFailureSkills(ApprovedFixSkills):
    def run_proof(
        self,
        run: Any,
        worker_result: dict[str, Any],
        verification: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append("run_proof")
        self.proof_worker_result = dict(worker_result)
        return {
            "passed": False,
            "summary": "proof setup failed: npm run monica:seed-auth exited 1",
            "artifacts": [],
            "proof_target": dict(self.proof_target),
            "setup_commands": list(self.proof_setup_commands),
            "commands": list(self.proof_commands),
            "required_env_keys": list(self.proof_required_env_keys),
            "platforms": list(self.proof_platforms),
        }


def test_proof_exception_marks_run_proof_blocked_instead_of_failed(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(
        run.id,
        status="awaiting_fix_approval",
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
    )
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = RaisingProofSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: simulator proof crashed before capture"
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert "Verification passed" in skills.status_posts[0]
    assert "proof is unavailable" in skills.status_posts[0]
    assert "simulator proof crashed before capture" in skills.status_posts[0]


def test_proof_setup_failure_blocks_share_and_pr_at_loop_level(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(
        run.id,
        status="awaiting_fix_approval",
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
    )
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ProofSetupFailureSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.branch_name == "monica/MOB-123-checkout-crash"
    assert updated.pr_url == ""
    assert updated.failure_reason == (
        "proof_unavailable: proof setup failed: npm run monica:seed-auth exited 1"
    )
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert skills.share_calls == []
    assert skills.opened_pr_worker_result == {}
    assert "Verification passed" in skills.status_posts[0]
    assert "proof is unavailable" in skills.status_posts[0]
    assert "npm run monica:seed-auth exited 1" in skills.status_posts[0]


def test_proof_blocked_run_resumes_existing_branch_without_worker(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    worktree = runtime / "workspace" / "worktrees" / "monica-MOB-123-checkout-crash"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: /tmp/fake\n", encoding="utf-8")
    monkeypatch.setattr(
        monica_loop,
        "_worktree_current_branch",
        lambda path: "monica/MOB-123-checkout-crash",
        raising=False,
    )
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        repo=RepoConfig(branch_prefix="monica", default_branch="dev"),
        runtime=RuntimeConfig(home_subdir=str(runtime)),
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(enabled=True, required_for_done=True),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event={
            "type": "message",
            "channel": "C_MOBILE",
            "text": "<@U_MONICA> can you fix this marketplace PDP copy bug?",
            "ts": "1710000000.000200",
            "permalink": "https://slack.example/archives/C_MOBILE/p1710000000000100",
        },
    )
    state.update_run(
        run.id,
        status="proof_blocked",
        linear_identifier="MOB-123",
        linear_issue_id="dbff3d48-3fd4-49d3-a3c6-61d8a85075b0",
        linear_url="https://linear.app/acme/issue/MOB-123",
        branch_name="monica/MOB-123-checkout-crash",
        base_branch="origin/dev",
        base_commit="abc1234",
        proof_deep_link="elixir-card://marketplace/offer/fitness-first",
        proof_expected_text="Fitness First",
        failure_reason="proof_unavailable: simulator not configured",
        approved_by_user_id="U_TAGGER",
    )
    skills = ApprovedFixSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "done"
    assert updated.branch_name == "monica/MOB-123-checkout-crash"
    assert updated.pr_url == "https://github.com/example/mobile/pull/123"
    assert skills.calls == ["run_verification", "run_proof", "open_draft_pr"]
    assert skills.proof_worker_result["proof_deep_link"] == "elixir-card://marketplace/offer/fitness-first"
    assert skills.proof_worker_result["proof_expected_text"] == "Fitness First"
    assert skills.opened_pr_worker_result["base_ref"] == "origin/dev"
    assert skills.opened_pr_worker_result["base_commit"] == "abc1234"


def test_proof_blocked_retry_preserves_stored_proof_screen(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    worktree = runtime / "workspace" / "worktrees" / "monica-MOB-123-checkout-crash"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: /tmp/fake\n", encoding="utf-8")
    monkeypatch.setattr(
        monica_loop,
        "_worktree_current_branch",
        lambda path: "monica/MOB-123-checkout-crash",
        raising=False,
    )
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        repo=RepoConfig(branch_prefix="monica", default_branch="dev"),
        runtime=RuntimeConfig(home_subdir=str(runtime)),
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(enabled=True, required_for_done=True),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event={
            "type": "message",
            "channel": "C_MOBILE",
            "text": "<@U_MONICA> can you fix this marketplace PDP copy bug?",
            "ts": "1710000000.000200",
            "permalink": "https://slack.example/archives/C_MOBILE/p1710000000000100",
        },
    )
    state.update_run(
        run.id,
        status="proof_blocked",
        linear_identifier="MOB-123",
        linear_issue_id="dbff3d48-3fd4-49d3-a3c6-61d8a85075b0",
        linear_url="https://linear.app/acme/issue/MOB-123",
        branch_name="monica/MOB-123-checkout-crash",
        base_branch="origin/dev",
        base_commit="abc1234",
        proof_deep_link="elixir-card://marketplace/offer/fitness-first",
        proof_expected_text="Fitness First",
        proof_screen="/MarketplacePdp",
        failure_reason="proof_unavailable: simulator not configured",
        approved_by_user_id="U_TAGGER",
    )
    skills = ApprovedFixSkills(
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Fitness First",
            "screen": "/MarketplacePdp",
        },
    )

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "done"
    assert skills.proof_worker_result["proof_screen"] == "/MarketplacePdp"
    assert skills.opened_pr_worker_result["proof_screen"] == "/MarketplacePdp"


def test_proof_blocked_retry_refuses_out_of_scope_request_before_proof(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    worktree = runtime / "workspace" / "worktrees" / "monica-MOB-123-checkout-crash"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: /tmp/fake\n", encoding="utf-8")
    monkeypatch.setattr(
        monica_loop,
        "_worktree_current_branch",
        lambda path: "monica/MOB-123-checkout-crash",
        raising=False,
    )
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        repo=RepoConfig(branch_prefix="monica", default_branch="dev"),
        runtime=RuntimeConfig(home_subdir=str(runtime)),
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(enabled=True, required_for_done=True),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this Android checkout crash?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(
        run.id,
        status="proof_blocked",
        linear_identifier="MOB-123",
        linear_issue_id="dbff3d48-3fd4-49d3-a3c6-61d8a85075b0",
        linear_url="https://linear.app/acme/issue/MOB-123",
        branch_name="monica/MOB-123-checkout-crash",
        base_branch="origin/dev",
        base_commit="abc1234",
        proof_deep_link="elixir-card://marketplace/offer/fitness-first",
        proof_expected_text="Fitness First",
        failure_reason="proof_unavailable: simulator not configured",
        approved_by_user_id="U_TAGGER",
    )
    skills = ApprovedFixSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "approved_pr_fix_scope_unsupported"
    assert updated.pr_url == ""
    assert skills.calls == []
    assert skills.share_calls == []
    assert "marketplace copy/design" in skills.status_posts[0]


def test_proof_blocked_retry_requires_verification_commands_before_verification(
    tmp_path,
    monkeypatch,
):
    runtime = tmp_path / "runtime"
    worktree = runtime / "workspace" / "worktrees" / "monica-MOB-123-checkout-crash"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: /tmp/fake\n", encoding="utf-8")
    monkeypatch.setattr(
        monica_loop,
        "_worktree_current_branch",
        lambda path: "monica/MOB-123-checkout-crash",
        raising=False,
    )
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        repo=RepoConfig(branch_prefix="monica", default_branch="dev"),
        runtime=RuntimeConfig(home_subdir=str(runtime)),
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=()),
        proof=_approved_pr_proof_config(enabled=True, required_for_done=True),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(
        run.id,
        status="proof_blocked",
        linear_identifier="MOB-123",
        linear_issue_id="dbff3d48-3fd4-49d3-a3c6-61d8a85075b0",
        linear_url="https://linear.app/acme/issue/MOB-123",
        branch_name="monica/MOB-123-checkout-crash",
        base_branch="origin/dev",
        base_commit="abc1234",
        proof_deep_link="elixir-card://marketplace/offer/fitness-first",
        proof_expected_text="Fitness First",
        failure_reason="proof_unavailable: simulator not configured",
        approved_by_user_id="U_TAGGER",
    )
    skills = ApprovedFixSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "verification_commands_missing"
    assert updated.pr_url == ""
    assert skills.calls == []
    assert skills.share_calls == []
    assert "verification.commands" in skills.status_posts[0]


def test_proof_blocked_retry_refuses_unexpected_worktree_branch(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    worktree = runtime / "workspace" / "worktrees" / "monica-MOB-123-checkout-crash"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: /tmp/fake\n", encoding="utf-8")
    monkeypatch.setattr(
        monica_loop,
        "_worktree_current_branch",
        lambda path: "monica/MOB-999-other",
        raising=False,
    )
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        repo=RepoConfig(branch_prefix="monica"),
        runtime=RuntimeConfig(home_subdir=str(runtime)),
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(enabled=True, required_for_done=True),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong, please fix it",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(
        run.id,
        status="proof_blocked",
        linear_identifier="MOB-123",
        branch_name="monica/MOB-123-checkout-crash",
        base_branch="origin/dev",
        base_commit="abc1234",
        proof_deep_link="elixir-card://marketplace/offer/fitness-first",
        proof_expected_text="Fitness First",
        failure_reason="proof_unavailable: simulator not configured",
        approved_by_user_id="U_TAGGER",
    )
    skills = ApprovedFixSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "proof_retry_worktree_branch_mismatch"
    assert updated.pr_url == ""
    assert skills.calls == []
    assert skills.share_calls == []
    assert "stored Monica branch" in skills.status_posts[0]


def test_proof_blocked_retry_allows_uncommitted_worktree_changes(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    worktree = runtime / "workspace" / "worktrees" / "monica-MOB-123-checkout-crash"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: /tmp/fake\n", encoding="utf-8")
    monkeypatch.setattr(
        monica_loop,
        "_worktree_current_branch",
        lambda path: "monica/MOB-123-checkout-crash",
        raising=False,
    )
    monkeypatch.setattr(
        monica_loop,
        "_worktree_has_uncommitted_changes",
        lambda path: True,
        raising=False,
    )
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        repo=RepoConfig(branch_prefix="monica", default_branch="dev"),
        runtime=RuntimeConfig(home_subdir=str(runtime)),
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(enabled=True, required_for_done=True),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong, please fix it",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(
        run.id,
        status="proof_blocked",
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        branch_name="monica/MOB-123-checkout-crash",
        base_branch="origin/dev",
        base_commit="abc1234",
        proof_deep_link="elixir-card://marketplace/offer/fitness-first",
        proof_expected_text="Fitness First",
        failure_reason="proof_unavailable: simulator not configured",
        approved_by_user_id="U_TAGGER",
    )
    skills = ApprovedFixSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "done"
    assert updated.pr_url == "https://github.com/example/mobile/pull/123"
    assert skills.calls == ["run_verification", "run_proof", "open_draft_pr"]


def test_proof_blocked_retry_requires_branch_to_match_stored_linear_issue(
    tmp_path,
    monkeypatch,
):
    runtime = tmp_path / "runtime"
    worktree = runtime / "workspace" / "worktrees" / "monica-MOB-999-checkout-crash"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: /tmp/fake\n", encoding="utf-8")
    monkeypatch.setattr(
        monica_loop,
        "_worktree_current_branch",
        lambda path: "monica/MOB-999-checkout-crash",
        raising=False,
    )
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        repo=RepoConfig(branch_prefix="monica"),
        runtime=RuntimeConfig(home_subdir=str(runtime)),
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(enabled=True, required_for_done=True),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong, please fix it",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(
        run.id,
        status="proof_blocked",
        linear_url="https://linear.app/acme/issue/MOB-123",
        branch_name="monica/MOB-999-checkout-crash",
        base_branch="origin/main",
        base_commit="abc1234",
        proof_deep_link="elixir-card://marketplace/offer/fitness-first",
        proof_expected_text="Fitness First",
        failure_reason="proof_unavailable: simulator not configured",
        approved_by_user_id="U_TAGGER",
    )
    skills = ApprovedFixSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "proof_retry_branch_mismatch"
    assert updated.pr_url == ""
    assert skills.calls == []
    assert skills.share_calls == []
    assert "stored branch" in skills.status_posts[0]


def test_proof_blocked_retry_requires_stored_base_metadata_before_verification(
    tmp_path,
    monkeypatch,
):
    runtime = tmp_path / "runtime"
    worktree = runtime / "workspace" / "worktrees" / "monica-MOB-123-checkout-crash"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: /tmp/fake\n", encoding="utf-8")
    monkeypatch.setattr(
        monica_loop,
        "_worktree_current_branch",
        lambda path: "monica/MOB-123-checkout-crash",
        raising=False,
    )
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        repo=RepoConfig(branch_prefix="monica"),
        runtime=RuntimeConfig(home_subdir=str(runtime)),
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(enabled=True, required_for_done=True),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong, please fix it",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(
        run.id,
        status="proof_blocked",
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        branch_name="monica/MOB-123-checkout-crash",
        proof_deep_link="elixir-card://marketplace/offer/fitness-first",
        proof_expected_text="Fitness First",
        failure_reason="proof_unavailable: simulator not configured",
        approved_by_user_id="U_TAGGER",
    )
    skills = ApprovedFixSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "approved_pr_base_metadata_missing"
    assert updated.pr_url == ""
    assert skills.calls == []
    assert skills.share_calls == []
    assert "fresh mobile base commit" in skills.status_posts[0]


def test_proof_blocked_retry_requires_configured_approver_before_proof(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    worktree = runtime / "workspace" / "worktrees" / "monica-MOB-123-checkout-crash"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: /tmp/fake\n", encoding="utf-8")
    monkeypatch.setattr(
        monica_loop,
        "_worktree_current_branch",
        lambda path: "monica/MOB-123-checkout-crash",
        raising=False,
    )
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        repo=RepoConfig(branch_prefix="monica"),
        runtime=RuntimeConfig(home_subdir=str(runtime)),
        slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(enabled=True, required_for_done=True),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong, please fix it",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(
        run.id,
        status="proof_blocked",
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        branch_name="monica/MOB-123-checkout-crash",
        proof_deep_link="elixir-card://marketplace/offer/fitness-first",
        proof_expected_text="Fitness First",
        failure_reason="proof_unavailable: simulator not configured",
        approved_by_user_id="U_NOT_ALLOWED",
    )
    skills = ApprovedFixSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "approved_pr_approver_not_configured"
    assert skills.calls == []
    assert "explicit configured approver approval" in skills.status_posts[0]


def test_proof_blocked_retry_requires_linear_issue_url_before_proof(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    worktree = runtime / "workspace" / "worktrees" / "monica-MOB-123-checkout-crash"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: /tmp/fake\n", encoding="utf-8")
    monkeypatch.setattr(
        monica_loop,
        "_worktree_current_branch",
        lambda path: "monica/MOB-123-checkout-crash",
        raising=False,
    )
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        repo=RepoConfig(branch_prefix="monica"),
        runtime=RuntimeConfig(home_subdir=str(runtime)),
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(enabled=True, required_for_done=True),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong, please fix it",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(
        run.id,
        status="proof_blocked",
        linear_identifier="MOB-123",
        branch_name="monica/MOB-123-checkout-crash",
        base_branch="origin/main",
        base_commit="abc1234",
        proof_deep_link="elixir-card://marketplace/offer/fitness-first",
        proof_expected_text="Fitness First",
        failure_reason="proof_unavailable: simulator not configured",
        approved_by_user_id="U_TAGGER",
    )
    skills = ApprovedFixSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "linear_issue_url_missing_before_fix"
    assert updated.pr_url == ""
    assert skills.calls == []
    assert skills.share_calls == []
    assert "Linear issue URL" in skills.status_posts[0]


def test_proof_blocked_retry_refuses_non_linear_issue_url_before_proof(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    worktree = runtime / "workspace" / "worktrees" / "monica-MOB-123-checkout-crash"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: /tmp/fake\n", encoding="utf-8")
    monkeypatch.setattr(
        monica_loop,
        "_worktree_current_branch",
        lambda path: "monica/MOB-123-checkout-crash",
        raising=False,
    )
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        repo=RepoConfig(branch_prefix="monica"),
        runtime=RuntimeConfig(home_subdir=str(runtime)),
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(enabled=True, required_for_done=True),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong, please fix it",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(
        run.id,
        status="proof_blocked",
        linear_identifier="MOB-123",
        linear_url="https://linear.example/MOB-123",
        branch_name="monica/MOB-123-checkout-crash",
        base_branch="origin/main",
        base_commit="abc1234",
        proof_deep_link="elixir-card://marketplace/offer/fitness-first",
        proof_expected_text="Fitness First",
        failure_reason="proof_unavailable: simulator not configured",
        approved_by_user_id="U_TAGGER",
    )
    skills = ApprovedFixSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "linear_issue_url_invalid_before_fix"
    assert updated.pr_url == ""
    assert skills.calls == []
    assert skills.share_calls == []
    assert "Linear issue URL" in skills.status_posts[0]


def test_proof_blocked_retry_requires_stored_expected_text_before_proof(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    worktree = runtime / "workspace" / "worktrees" / "monica-MOB-123-checkout-crash"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: /tmp/fake\n", encoding="utf-8")
    monkeypatch.setattr(
        monica_loop,
        "_worktree_current_branch",
        lambda path: "monica/MOB-123-checkout-crash",
        raising=False,
    )
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        repo=RepoConfig(branch_prefix="monica"),
        runtime=RuntimeConfig(home_subdir=str(runtime)),
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(enabled=True, required_for_done=True),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong, please fix it",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(
        run.id,
        status="proof_blocked",
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        branch_name="monica/MOB-123-checkout-crash",
        base_branch="origin/main",
        base_commit="abc1234",
        proof_deep_link="elixir-card://marketplace/offer/fitness-first",
        failure_reason="proof_unavailable: simulator not configured",
        approved_by_user_id="U_TAGGER",
    )
    skills = ApprovedFixSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.failure_reason == "proof_unavailable: missing proof target expected text"
    assert skills.calls == ["run_verification"]


def test_local_fix_only_marks_done_after_required_proof_artifact(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="local_fix_only",
        verification=VerificationConfig(commands=("npm test",)),
        proof=ProofConfig(enabled=True, required_for_done=True),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this Android checkout crash?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "done"
    assert updated.branch_name == "monica/MOB-123-checkout-crash"
    assert updated.pr_url == ""
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert "Proof captured" in skills.status_posts[0]
    assert "/tmp/monica-proof/ios-screenshot.png" in skills.status_posts[0]


def test_approved_pr_requires_proof_before_opening_pr_when_configured(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(enabled=True, required_for_done=True),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills(proof_passed=True, proof_artifacts=())

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: Simulator proof captured."
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]


def test_approved_pr_requires_both_ios_and_android_proof_artifacts(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills(proof_artifacts=("/tmp/monica-proof/android-screenshot.png",))

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: missing required platform artifacts: ios"
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]


def test_approved_pr_requires_distinct_ios_and_android_visual_artifacts(tmp_path):
    mixed_artifact = "/tmp/monica-proof/ios-android-screenshot.png"
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills(
        proof_artifacts=(mixed_artifact,),
        proof_shareable_artifacts=(
            {
                "platform": "ios",
                "path": mixed_artifact,
                "url": "https://slack.example/files/ios-android-screenshot.png",
            },
            {
                "platform": "android",
                "path": mixed_artifact,
                "url": "https://slack.example/files/ios-android-screenshot.png",
            },
        ),
    )

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: missing required platform artifacts: android"
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert skills.share_calls == []


def test_approved_pr_requires_visual_proof_artifact_files_to_exist(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills(
        proof_artifacts=(
            str(tmp_path / "proof" / "ios-screenshot.png"),
            str(tmp_path / "proof" / "android-screenshot.png"),
        ),
        create_proof_artifacts=False,
    )

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: missing proof artifact files: ios, android"
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert skills.share_calls == []


def test_approved_pr_requires_proof_manifest_artifact_before_sharing_or_pr(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills(include_proof_manifest=False)

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: missing proof manifest artifact"
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert skills.share_calls == []


def test_approved_pr_requires_proof_manifest_to_match_worker_base_commit(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills(proof_manifest_base_commit="stale-base")

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: proof manifest base commit does not match worker base commit"
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert skills.share_calls == []


def test_approved_pr_requires_proof_manifest_to_match_worker_base_ref(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills(proof_manifest_base_ref="origin/stale")

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: proof manifest base ref does not match worker base ref"
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert skills.share_calls == []


def test_approved_pr_requires_proof_manifest_to_match_worker_branch(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills(proof_manifest_branch_name="monica/MOB-999-stale-proof")

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: proof manifest branch does not match worker branch"
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert skills.share_calls == []


def test_approved_pr_requires_proof_manifest_to_match_linear_issue(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(
        run.id,
        status="awaiting_fix_approval",
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
    )
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills(
        proof_manifest_linear_identifier="MOB-999",
        proof_manifest_linear_url="https://linear.app/acme/issue/MOB-999",
    )

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: proof manifest Linear issue does not match Monica run"
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert skills.share_calls == []


def test_approved_pr_requires_proof_manifest_to_match_run_id(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(
        run.id,
        status="awaiting_fix_approval",
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
    )
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills(proof_manifest_run_id="stale-run")

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: proof manifest run id does not match Monica run"
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert skills.share_calls == []


def test_approved_pr_requires_proof_manifest_to_include_run_id(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(
        run.id,
        status="awaiting_fix_approval",
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
    )
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills(proof_manifest_omit_keys=frozenset({"run_id"}))

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: proof manifest run id does not match Monica run"
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert skills.share_calls == []


def test_approved_pr_requires_proof_manifest_to_match_worker_worktree(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkillsWithWorktree(proof_manifest_worktree="/tmp/monica-worktrees/stale")

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: proof manifest worktree does not match worker worktree"
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert skills.share_calls == []


def test_approved_pr_requires_proof_manifest_to_match_proof_target(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills(
        proof_manifest_target={
            "deep_link": "elixir-card://marketplace/offer/stale-offer",
            "expected_text": "Stale Offer",
        },
    )

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: proof manifest target does not match proof target"
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert skills.share_calls == []


def test_approved_pr_requires_proof_manifest_to_match_setup_commands(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills(proof_manifest_setup_commands=("npm run stale-auth",))

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: proof manifest setup commands do not match proof setup commands"
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert skills.share_calls == []


def test_approved_pr_requires_proof_manifest_to_match_capture_commands(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills(proof_manifest_commands=("npm run stale-proof",))

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: proof manifest commands do not match proof commands"
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert skills.share_calls == []


def test_approved_pr_requires_proof_manifest_to_match_required_env_keys(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(required_env_keys=("MONICA_TEST_LOGIN_TOKEN",)),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills(
        proof_required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
        proof_manifest_required_env_keys=("MONICA_STALE_LOGIN_TOKEN",),
    )

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: proof manifest required env keys do not match proof required env keys"
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert skills.share_calls == []


def test_approved_pr_rejects_invalid_proof_manifest_required_env_key(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(required_env_keys=("MONICA_TEST_LOGIN_TOKEN",)),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(
        run.id,
        status="awaiting_fix_approval",
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
    )
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills(
        proof_required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
        proof_manifest_required_env_keys=("MONICA_TEST_LOGIN_TOKEN=secret",),
    )

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert "proof manifest required env keys are invalid" in updated.failure_reason
    assert "MONICA_TEST_LOGIN_TOKEN" in updated.failure_reason
    assert "secret" not in updated.failure_reason
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert skills.share_calls == []


def test_approved_pr_requires_proof_manifest_to_match_visual_artifacts(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills(
        proof_manifest_proof_artifacts=(
            "/tmp/monica-proof/ios-screenshot.png",
            "/tmp/monica-proof/stale-android-screenshot.png",
        )
    )

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: proof manifest artifacts do not match proof artifacts"
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert skills.share_calls == []


def test_approved_pr_requires_proof_manifest_to_match_platforms(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills(proof_manifest_platforms=("ios",))

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: proof manifest platforms do not match proof platforms"
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert skills.share_calls == []


def test_approved_pr_requires_proof_setup_commands_to_match_config(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(setup_commands=("npm run monica:seed-current-auth",)),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: proof setup commands do not match configured proof setup commands"
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert skills.share_calls == []


def test_approved_pr_requires_proof_required_env_keys_to_match_config(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(required_env_keys=("MONICA_TEST_LOGIN_TOKEN",)),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills(proof_required_env_keys=())

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: proof required env keys do not match configured proof required env keys"
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert skills.share_calls == []


def test_approved_pr_rejects_invalid_configured_proof_required_env_keys(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(
            required_env_keys=("MONICA_TEST_LOGIN_TOKEN=secret",),
        ),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills(
        proof_required_env_keys=("MONICA_TEST_LOGIN_TOKEN=secret",),
        proof_manifest_required_env_keys=("MONICA_TEST_LOGIN_TOKEN=secret",),
    )

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == (
        "proof_unavailable: proof.required_env_keys contains invalid key names: "
        "MONICA_TEST_LOGIN_TOKEN"
    )
    assert "secret" not in updated.failure_reason
    assert skills.calls == ["run_internal_codex_worker", "run_verification"]
    assert skills.share_calls == []


def test_approved_pr_requires_proof_capture_commands_to_match_config(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(commands=("npm run monica:proof-current-target",)),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: proof commands do not match configured proof commands"
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert skills.share_calls == []


def test_approved_pr_requires_target_text_evidence_artifacts_before_sharing_or_pr(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    visual_artifacts = (
        "/tmp/monica-proof/ios-screenshot.png",
        "/tmp/monica-proof/android-screenshot.png",
        "/tmp/monica-proof/ios-metro.stdout.log",
        "/tmp/monica-proof/android-metro.stdout.log",
    )
    skills = ApprovedFixSkills(
        proof_artifacts=visual_artifacts,
        proof_manifest_proof_artifacts=visual_artifacts,
    )

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: missing proof target evidence artifacts: ios, android"
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert skills.share_calls == []


def test_approved_pr_requires_target_route_evidence_artifacts_before_sharing_or_pr(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    proof_root = tmp_path / "proof"
    proof_artifacts = (
        str(proof_root / "ios-screenshot.png"),
        str(proof_root / "android-screenshot.png"),
        str(proof_root / "ios-target.log"),
        str(proof_root / "android-ui.xml"),
    )
    skills = ApprovedFixSkills(
        proof_manifest_artifact=str(proof_root / "monica-proof-manifest.json"),
        proof_artifacts=proof_artifacts,
        proof_manifest_proof_artifacts=proof_artifacts,
        proof_shareable_artifacts=(
            {
                "platform": "ios",
                "path": proof_artifacts[0],
                "url": "https://slack.example/files/ios-screenshot.png",
            },
            {
                "platform": "android",
                "path": proof_artifacts[1],
                "url": "https://slack.example/files/android-screenshot.png",
            },
        ),
    )

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: missing proof target route evidence artifacts: ios, android"
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert skills.share_calls == []


def test_approved_pr_rejects_route_that_does_not_match_target_before_sharing_or_pr(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills(proof_route="/Home")

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: proof target route does not match proof target for: ios, android"
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert skills.share_calls == []


def test_approved_pr_does_not_accept_manifest_as_target_text_evidence(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    proof_root = tmp_path / "ios-android-proof"
    visual_artifacts = (
        str(proof_root / "ios-screenshot.png"),
        str(proof_root / "android-screenshot.png"),
        str(proof_root / "ios-metro.stdout.log"),
        str(proof_root / "android-metro.stdout.log"),
    )
    skills = ApprovedFixSkills(
        proof_manifest_artifact=str(proof_root / "monica-proof-manifest.json"),
        proof_artifacts=visual_artifacts,
        proof_manifest_proof_artifacts=visual_artifacts,
    )

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: missing proof target evidence artifacts: ios, android"
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert skills.share_calls == []


def test_approved_pr_requires_distinct_target_text_evidence_per_platform(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    proof_root = tmp_path / "proof"
    proof_artifacts = (
        str(proof_root / "ios-screenshot.png"),
        str(proof_root / "android-screenshot.png"),
        str(proof_root / "ios-android-ui.xml"),
        str(proof_root / "ios-metro.stdout.log"),
        str(proof_root / "android-metro.stdout.log"),
    )
    skills = ApprovedFixSkills(
        proof_manifest_artifact=str(proof_root / "monica-proof-manifest.json"),
        proof_artifacts=proof_artifacts,
        proof_manifest_proof_artifacts=proof_artifacts,
    )

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: missing proof target evidence artifacts: android"
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert skills.share_calls == []


def test_approved_pr_requires_shareable_ios_and_android_proof_links(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills(
        proof_shareable_artifacts=(
            {
                "platform": "ios",
                "path": "/tmp/monica-proof/ios-screenshot.png",
                "url": "https://slack.example/files/ios-screenshot.png",
            },
        )
    )

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: missing shareable proof links: android"
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert skills.share_calls == ["share_proof_artifacts"]


def test_approved_pr_blocks_stale_proof_artifacts_before_sharing(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    stale_artifacts = (
        str(tmp_path / "stale-proof" / "ios-screenshot.png"),
        str(tmp_path / "stale-proof" / "android-screenshot.png"),
        str(tmp_path / "stale-proof" / "ios-target.log"),
        str(tmp_path / "stale-proof" / "android-ui.xml"),
        str(tmp_path / "stale-proof" / "ios-metro.stdout.log"),
        str(tmp_path / "stale-proof" / "android-metro.stdout.log"),
    )
    skills = ApprovedFixSkills(
        proof_manifest_artifact=str(tmp_path / "proof" / "monica-proof-manifest.json"),
        proof_artifacts=stale_artifacts,
        proof_manifest_proof_artifacts=stale_artifacts,
    )

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == (
        "proof_unavailable: proof artifacts must stay under proof manifest directory: "
        "ios-screenshot.png, android-screenshot.png, ios-target.log, android-ui.xml, "
        "ios-metro.stdout.log, android-metro.stdout.log"
    )
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert skills.share_calls == []


def test_approved_pr_reports_proof_share_errors_when_links_are_missing(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")

    class ShareFailingSkills(ApprovedFixSkills):
        def share_proof_artifacts(self, run: Any, proof: dict[str, Any]) -> dict[str, Any]:
            self.share_calls.append("share_proof_artifacts")
            return {
                "shareable_artifacts": [],
                "share_errors": ["ios screenshot upload failed: not_in_channel"],
            }

    skills = ShareFailingSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == (
        "proof_unavailable: missing shareable proof links: ios, android "
        "(share errors: ios screenshot upload failed: not_in_channel)"
    )
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert skills.share_calls == ["share_proof_artifacts"]


def test_approved_pr_ignores_prefilled_shareable_links_when_upload_fails(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(
        run.id,
        status="awaiting_fix_approval",
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
    )
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")

    class StaleShareableSkills(ApprovedFixSkills):
        def run_proof(
            self,
            run: Any,
            worker_result: dict[str, Any],
            verification: dict[str, Any],
        ) -> dict[str, Any]:
            proof = super().run_proof(run, worker_result, verification)
            proof["shareable_artifacts"] = [
                {
                    "platform": "ios",
                    "path": "/tmp/monica-proof/ios-screenshot.png",
                    "url": "https://slack.example/files/stale-ios-screenshot.png",
                },
                {
                    "platform": "android",
                    "path": "/tmp/monica-proof/android-screenshot.png",
                    "url": "https://slack.example/files/stale-android-screenshot.png",
                },
            ]
            return proof

        def share_proof_artifacts(self, run: Any, proof: dict[str, Any]) -> dict[str, Any]:
            self.share_calls.append("share_proof_artifacts")
            raise RuntimeError("Slack upload failed")

    skills = StaleShareableSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == (
        "proof_unavailable: missing shareable proof links: ios, android "
        "(share errors: Slack upload failed)"
    )
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert skills.share_calls == ["share_proof_artifacts"]


def test_approved_pr_requires_shareable_proof_links_to_match_local_artifacts(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    proof_root = tmp_path / "proof"
    ios_route_artifact = str(proof_root / "ios-metro.stdout.log")
    android_route_artifact = str(proof_root / "android-metro.stdout.log")
    skills = ApprovedFixSkills(
        proof_manifest_artifact=str(proof_root / "monica-proof-manifest.json"),
        proof_artifacts=(
            str(proof_root / "ios-screenshot.png"),
            str(proof_root / "android-screenshot.png"),
            str(proof_root / "ios-target.log"),
            str(proof_root / "android-ui.xml"),
            ios_route_artifact,
            android_route_artifact,
        ),
        proof_shareable_artifacts=(
            {
                "platform": "ios",
                "path": str(tmp_path / "other-proof" / "ios-screenshot.png"),
                "url": "https://slack.example/files/ios-screenshot.png",
            },
            {
                "platform": "android",
                "path": str(tmp_path / "other-proof" / "android-screenshot.png"),
                "url": "https://slack.example/files/android-screenshot.png",
            },
        ),
    )

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == (
        "proof_unavailable: shareable proof links do not match local artifacts: ios, android"
    )
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert skills.share_calls == ["share_proof_artifacts"]


def test_approved_pr_share_step_cannot_rewrite_local_proof_artifacts(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")

    class ArtifactOverwritingShareSkills(ApprovedFixSkills):
        def share_proof_artifacts(self, run: Any, proof: dict[str, Any]) -> dict[str, Any]:
            self.share_calls.append("share_proof_artifacts")
            return {
                "artifacts": [
                    str(tmp_path / "other-proof" / "ios-screenshot.png"),
                    str(tmp_path / "other-proof" / "android-screenshot.png"),
                ],
                "shareable_artifacts": [
                    {
                        "platform": "ios",
                        "path": str(tmp_path / "other-proof" / "ios-screenshot.png"),
                        "url": "https://slack.example/files/ios-screenshot.png",
                    },
                    {
                        "platform": "android",
                        "path": str(tmp_path / "other-proof" / "android-screenshot.png"),
                        "url": "https://slack.example/files/android-screenshot.png",
                    },
                ],
            }

    skills = ArtifactOverwritingShareSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == (
        "proof_unavailable: shareable proof links do not match local artifacts: ios, android"
    )
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert skills.share_calls == ["share_proof_artifacts"]


def test_approved_pr_share_step_cannot_mutate_local_proof_artifacts_in_place(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    proof_root = tmp_path / "proof"
    ios_artifact = str(proof_root / "ios-screenshot.png")
    android_artifact = str(proof_root / "android-screenshot.png")
    ios_target_artifact = str(proof_root / "ios-target.log")
    android_target_artifact = str(proof_root / "android-ui.xml")
    ios_route_artifact = str(proof_root / "ios-metro.stdout.log")
    android_route_artifact = str(proof_root / "android-metro.stdout.log")
    mutated_ios = str(tmp_path / "other-proof" / "ios-screenshot.png")
    mutated_android = str(tmp_path / "other-proof" / "android-screenshot.png")

    class MutatingShareSkills(ApprovedFixSkills):
        def share_proof_artifacts(self, run: Any, proof: dict[str, Any]) -> dict[str, Any]:
            self.share_calls.append("share_proof_artifacts")
            proof["artifacts"][:] = [mutated_ios, mutated_android]
            return {
                "shareable_artifacts": [
                    {
                        "platform": "ios",
                        "path": mutated_ios,
                        "url": "https://slack.example/files/ios-screenshot.png",
                    },
                    {
                        "platform": "android",
                        "path": mutated_android,
                        "url": "https://slack.example/files/android-screenshot.png",
                    },
                ],
            }

    skills = MutatingShareSkills(
        proof_manifest_artifact=str(proof_root / "monica-proof-manifest.json"),
        proof_artifacts=(
            ios_artifact,
            android_artifact,
            ios_target_artifact,
            android_target_artifact,
            ios_route_artifact,
            android_route_artifact,
        ),
    )

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == (
        "proof_unavailable: shareable proof links do not match local artifacts: ios, android"
    )
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert skills.share_calls == ["share_proof_artifacts"]


def test_approved_pr_requires_distinct_shareable_proof_links_per_platform(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    proof_root = tmp_path / "proof"
    ios_artifact = str(proof_root / "ios-screenshot.png")
    android_artifact = str(proof_root / "android-screenshot.png")
    ios_target_artifact = str(proof_root / "ios-target.log")
    android_target_artifact = str(proof_root / "android-ui.xml")
    ios_route_artifact = str(proof_root / "ios-metro.stdout.log")
    android_route_artifact = str(proof_root / "android-metro.stdout.log")
    shared_url = "https://slack.example/files/shared-proof.png"
    skills = ApprovedFixSkills(
        proof_manifest_artifact=str(proof_root / "monica-proof-manifest.json"),
        proof_artifacts=(
            ios_artifact,
            android_artifact,
            ios_target_artifact,
            android_target_artifact,
            ios_route_artifact,
            android_route_artifact,
        ),
        proof_shareable_artifacts=(
            {
                "platform": "ios",
                "path": ios_artifact,
                "url": shared_url,
            },
            {
                "platform": "android",
                "path": android_artifact,
                "url": shared_url,
            },
        ),
    )

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: duplicate shareable proof links: android"
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert skills.share_calls == ["share_proof_artifacts"]


def test_approved_pr_rejects_local_paths_as_shareable_proof_links(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    proof_root = tmp_path / "proof"
    ios_artifact = str(proof_root / "ios-screenshot.png")
    android_artifact = str(proof_root / "android-screenshot.png")
    ios_target_artifact = str(proof_root / "ios-target.log")
    android_target_artifact = str(proof_root / "android-ui.xml")
    ios_route_artifact = str(proof_root / "ios-metro.stdout.log")
    android_route_artifact = str(proof_root / "android-metro.stdout.log")
    skills = ApprovedFixSkills(
        proof_manifest_artifact=str(proof_root / "monica-proof-manifest.json"),
        proof_artifacts=(
            ios_artifact,
            android_artifact,
            ios_target_artifact,
            android_target_artifact,
            ios_route_artifact,
            android_route_artifact,
        ),
        proof_shareable_artifacts=(
            {
                "platform": "ios",
                "path": ios_artifact,
                "url": f"file://{ios_artifact}",
            },
            {
                "platform": "android",
                "path": android_artifact,
                "url": android_artifact,
            },
        ),
    )

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: missing shareable proof links: ios, android"
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert skills.share_calls == ["share_proof_artifacts"]


def test_approved_pr_rejects_loopback_urls_as_shareable_proof_links(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    proof_root = tmp_path / "proof"
    ios_artifact = str(proof_root / "ios-screenshot.png")
    android_artifact = str(proof_root / "android-screenshot.png")
    ios_target_artifact = str(proof_root / "ios-target.log")
    android_target_artifact = str(proof_root / "android-ui.xml")
    ios_route_artifact = str(proof_root / "ios-metro.stdout.log")
    android_route_artifact = str(proof_root / "android-metro.stdout.log")
    skills = ApprovedFixSkills(
        proof_manifest_artifact=str(proof_root / "monica-proof-manifest.json"),
        proof_artifacts=(
            ios_artifact,
            android_artifact,
            ios_target_artifact,
            android_target_artifact,
            ios_route_artifact,
            android_route_artifact,
        ),
        proof_shareable_artifacts=(
            {
                "platform": "ios",
                "path": ios_artifact,
                "url": "http://localhost:3000/ios-screenshot.png",
            },
            {
                "platform": "android",
                "path": android_artifact,
                "url": "https://127.0.0.1/android-screenshot.png",
            },
        ),
    )

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: missing shareable proof links: ios, android"
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert skills.share_calls == ["share_proof_artifacts"]


def test_approved_pr_rejects_hostless_urls_as_shareable_proof_links(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    proof_root = tmp_path / "proof"
    ios_artifact = str(proof_root / "ios-screenshot.png")
    android_artifact = str(proof_root / "android-screenshot.png")
    ios_target_artifact = str(proof_root / "ios-target.log")
    android_target_artifact = str(proof_root / "android-ui.xml")
    ios_route_artifact = str(proof_root / "ios-metro.stdout.log")
    android_route_artifact = str(proof_root / "android-metro.stdout.log")
    skills = ApprovedFixSkills(
        proof_manifest_artifact=str(proof_root / "monica-proof-manifest.json"),
        proof_artifacts=(
            ios_artifact,
            android_artifact,
            ios_target_artifact,
            android_target_artifact,
            ios_route_artifact,
            android_route_artifact,
        ),
        proof_shareable_artifacts=(
            {
                "platform": "ios",
                "path": ios_artifact,
                "url": "https:///ios-screenshot.png",
            },
            {
                "platform": "android",
                "path": android_artifact,
                "url": "http:///android-screenshot.png",
            },
        ),
    )

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: missing shareable proof links: ios, android"
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert skills.share_calls == ["share_proof_artifacts"]


@pytest.mark.parametrize(
    ("ios_url", "android_url", "missing_platforms"),
    (
        (
            "https://10.0.0.5/ios-screenshot.png",
            "http://192.168.1.12/android-screenshot.png",
            "ios, android",
        ),
        (
            "https://proof.local/ios-screenshot.png",
            "http://monica-proof/android-screenshot.png",
            "ios, android",
        ),
        (
            "https://slack.example/files/ios-screenshot.png",
            "https://proof.internal/android-screenshot.png",
            "android",
        ),
    ),
)
def test_approved_pr_rejects_private_network_urls_as_shareable_proof_links(
    tmp_path,
    ios_url,
    android_url,
    missing_platforms,
):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    proof_root = tmp_path / "proof"
    ios_artifact = str(proof_root / "ios-screenshot.png")
    android_artifact = str(proof_root / "android-screenshot.png")
    ios_target_artifact = str(proof_root / "ios-target.log")
    android_target_artifact = str(proof_root / "android-ui.xml")
    ios_route_artifact = str(proof_root / "ios-metro.stdout.log")
    android_route_artifact = str(proof_root / "android-metro.stdout.log")
    skills = ApprovedFixSkills(
        proof_manifest_artifact=str(proof_root / "monica-proof-manifest.json"),
        proof_artifacts=(
            ios_artifact,
            android_artifact,
            ios_target_artifact,
            android_target_artifact,
            ios_route_artifact,
            android_route_artifact,
        ),
        proof_shareable_artifacts=(
            {
                "platform": "ios",
                "path": ios_artifact,
                "url": ios_url,
            },
            {
                "platform": "android",
                "path": android_artifact,
                "url": android_url,
            },
        ),
    )

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == f"proof_unavailable: missing shareable proof links: {missing_platforms}"
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert skills.share_calls == ["share_proof_artifacts"]


def test_approved_pr_requires_target_deep_link_before_opening_pr(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong, please fix it",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills(worker_proof_deep_link="", proof_target={})

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: missing proof target deep link"
    assert skills.calls == ["run_internal_codex_worker", "run_verification"]


def test_approved_pr_requires_worker_target_deep_link_even_when_configured(tmp_path):
    fallback_deep_link = "elixir-card://marketplace/fallback"
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(deep_link=fallback_deep_link),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong, please fix it",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills(
        worker_proof_deep_link="",
        proof_target={
            "deep_link": fallback_deep_link,
            "expected_text": "Fitness First",
        },
    )

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: missing proof target deep link"
    assert skills.calls == ["run_internal_codex_worker", "run_verification"]
    assert skills.share_calls == []


def test_approved_pr_rejects_generic_home_target_before_proof(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong, please fix it",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills(
        worker_proof_deep_link="elixir-card://home",
        proof_target={
            "deep_link": "elixir-card://home",
            "expected_text": "Fitness First",
        },
    )

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: generic proof target is not enough"
    assert skills.calls == ["run_internal_codex_worker", "run_verification"]
    assert skills.share_calls == []


def test_approved_pr_rejects_generic_marketplace_target_before_proof(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong, please fix it",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills(
        worker_proof_deep_link="elixir-card://marketplace",
        proof_target={
            "deep_link": "elixir-card://marketplace",
            "expected_text": "Fitness First",
        },
    )

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: generic proof target is not enough"
    assert skills.calls == ["run_internal_codex_worker", "run_verification"]
    assert skills.share_calls == []


def test_approved_pr_rejects_generic_offer_route_target_before_proof(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong, please fix it",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills(
        worker_proof_deep_link="elixir-card://marketplace/offer",
        proof_target={
            "deep_link": "elixir-card://marketplace/offer",
            "expected_text": "Fitness First",
        },
    )

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: generic proof target is not enough"
    assert skills.calls == ["run_internal_codex_worker", "run_verification"]
    assert skills.share_calls == []


def test_approved_pr_rejects_expo_dev_client_target_before_proof(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong, please fix it",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    expo_target = "elixir-card://expo-development-client/?url=http%3A%2F%2F127.0.0.1%3A8081"
    skills = ApprovedFixSkills(
        worker_proof_deep_link=expo_target,
        proof_target={
            "deep_link": expo_target,
            "expected_text": "Fitness First",
        },
    )

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: Expo Dev Client proof target is not enough"
    assert skills.calls == ["run_internal_codex_worker", "run_verification"]
    assert skills.share_calls == []


def test_approved_pr_rejects_expo_runtime_target_before_proof(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong, please fix it",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    expo_target = "exp://127.0.0.1:8081/--/marketplace/offer/fitness-first"
    skills = ApprovedFixSkills(
        worker_proof_deep_link=expo_target,
        proof_target={
            "deep_link": expo_target,
            "expected_text": "Fitness First",
        },
    )

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: Expo runtime proof target is not enough"
    assert skills.calls == ["run_internal_codex_worker", "run_verification"]
    assert skills.share_calls == []


def test_approved_pr_rejects_local_http_target_before_proof(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong, please fix it",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    local_target = "http://127.0.0.1:8081/marketplace/offer/fitness-first"
    skills = ApprovedFixSkills(
        worker_proof_deep_link=local_target,
        proof_target={
            "deep_link": local_target,
            "expected_text": "Fitness First",
        },
    )

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: local proof target is not enough"
    assert skills.calls == ["run_internal_codex_worker", "run_verification"]
    assert skills.share_calls == []


def test_approved_pr_requires_target_expected_text_before_opening_pr(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong, please fix it",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills(
        worker_proof_expected_text="",
        proof_target={"deep_link": "elixir-card://marketplace/offer/fitness-first"}
    )

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: missing proof target expected text"
    assert skills.calls == ["run_internal_codex_worker", "run_verification"]


@pytest.mark.parametrize(
    "worker_proof_expected_text",
    ("unavailable", "text visible on the fixed screen", "<text visible on the fixed screen>"),
)
def test_approved_pr_rejects_placeholder_worker_expected_text_before_proof(
    tmp_path,
    worker_proof_expected_text,
):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong, please fix it",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills(
        worker_proof_expected_text=worker_proof_expected_text,
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Fitness First",
        },
    )

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: missing proof target expected text"
    assert skills.calls == ["run_internal_codex_worker", "run_verification"]


def test_approved_pr_rejects_generic_worker_expected_text_before_proof(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong, please fix it",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills(
        worker_proof_expected_text="Marketplace",
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Fitness First",
        },
    )

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: proof target expected text is too generic"
    assert skills.calls == ["run_internal_codex_worker", "run_verification"]


def test_approved_pr_rejects_route_container_expected_text_before_proof(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong, please fix it",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills(
        worker_proof_expected_text="Offer",
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Fitness First",
        },
    )

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: proof target expected text is too generic"
    assert skills.calls == ["run_internal_codex_worker", "run_verification"]


def test_approved_pr_blocks_when_proof_target_does_not_match_worker_target(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong, please fix it",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills(
        proof_target={
            "deep_link": "elixir-card://marketplace",
            "expected_text": "Marketplace",
        }
    )

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: proof target does not match requested deep link"
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert skills.share_calls == []


def test_approved_pr_blocks_when_proof_screen_does_not_match_worker_screen(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong, please fix it",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills(
        worker_proof_screen="/MarketplacePdp",
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Fitness First",
        },
    )

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: proof target does not match requested screen"
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert skills.share_calls == []


def test_approved_pr_auth_fallback_proof_failure_blocks_pr(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong, please fix it",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills(
        proof_passed=False,
        proof_artifacts=("/tmp/monica-proof/ios-screenshot.png", "/tmp/monica-proof/android-screenshot.png"),
    )

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: simctl is unavailable"
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]


def test_approved_pr_auth_fallback_artifact_blocks_pr(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong, please fix it",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    proof_root = tmp_path / "proof"
    auth_log = str(proof_root / "ios-metro.stdout.log")
    ios_artifact = str(proof_root / "ios-screenshot.png")
    android_artifact = str(proof_root / "android-screenshot.png")
    ios_target_artifact = str(proof_root / "ios-target.log")
    android_target_artifact = str(proof_root / "android-ui.xml")

    class AuthFallbackSkills(ApprovedFixSkills):
        def run_proof(
            self,
            run: Any,
            worker_result: dict[str, Any],
            verification: dict[str, Any],
        ) -> dict[str, Any]:
            proof = super().run_proof(run, worker_result, verification)
            Path(auth_log).write_text(
                "LOG [auth] not logged in -> onboarding\n",
                encoding="utf-8",
            )
            proof["artifacts"].append(auth_log)
            return proof

    skills = AuthFallbackSkills(
        proof_manifest_artifact=str(proof_root / "monica-proof-manifest.json"),
        proof_artifacts=(
            ios_artifact,
            android_artifact,
            ios_target_artifact,
            android_target_artifact,
        ),
        proof_manifest_proof_artifacts=(
            ios_artifact,
            android_artifact,
            ios_target_artifact,
            android_target_artifact,
            auth_log,
        ),
        proof_shareable_artifacts=(
            {
                "platform": "ios",
                "path": ios_artifact,
                "url": "https://slack.example/files/ios-screenshot.png",
            },
            {
                "platform": "android",
                "path": android_artifact,
                "url": "https://slack.example/files/android-screenshot.png",
            },
        ),
    )

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: auth/onboarding proof fallback observed for: ios"
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert skills.share_calls == []


def test_approved_pr_splash_screen_artifact_blocks_before_share_or_pr(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong, please fix it",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    proof_root = tmp_path / "proof"
    ios_artifact = str(proof_root / "ios-screenshot.png")
    android_artifact = str(proof_root / "android-screenshot.png")
    ios_target_artifact = str(proof_root / "ios-target.log")
    android_target_artifact = str(proof_root / "android-ui.xml")
    ios_route_log = str(proof_root / "ios-metro.stdout.log")
    android_route_log = str(proof_root / "android-metro.stdout.log")

    class SplashScreenSkills(ApprovedFixSkills):
        def run_proof(
            self,
            run: Any,
            worker_result: dict[str, Any],
            verification: dict[str, Any],
        ) -> dict[str, Any]:
            proof = super().run_proof(run, worker_result, verification)
            Path(ios_route_log).write_text(
                "LOG  [APP-PERF-METRIC] ui.load | screen.load /SplashScreen | 6ms | ok\n",
                encoding="utf-8",
            )
            Path(android_route_log).write_text(
                "LOG  [APP-PERF-METRIC] ui.load | screen.load /SplashScreen | 19ms | ok\n",
                encoding="utf-8",
            )
            proof["artifacts"].extend([ios_route_log, android_route_log])
            return proof

    skills = SplashScreenSkills(
        proof_manifest_artifact=str(proof_root / "monica-proof-manifest.json"),
        proof_artifacts=(
            ios_artifact,
            android_artifact,
            ios_target_artifact,
            android_target_artifact,
        ),
        proof_manifest_proof_artifacts=(
            ios_artifact,
            android_artifact,
            ios_target_artifact,
            android_target_artifact,
            ios_route_log,
            android_route_log,
        ),
        proof_shareable_artifacts=(
            {
                "platform": "ios",
                "path": ios_artifact,
                "url": "https://slack.example/files/ios-screenshot.png",
            },
            {
                "platform": "android",
                "path": android_artifact,
                "url": "https://slack.example/files/android-screenshot.png",
            },
        ),
    )

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.pr_url == ""
    assert updated.failure_reason == "proof_unavailable: non-target app screen observed for: ios, android"
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof"]
    assert skills.share_calls == []


def test_local_fix_only_creates_ticket_then_waits_for_approval_before_code(tmp_path):
    config = MonicaConfig(enabled=True, rollout_mode="local_fix_only")
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this Android checkout crash?",
        raw_event=_approved_pr_raw_event(),
    )
    skills = LinearOnlySkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "awaiting_fix_approval"
    assert updated.linear_identifier == "MOB-123"
    assert skills.calls == ["read_slack_thread", "infer_user_intent", "create_or_update_linear", "ask_fix_approval"]


def test_approved_run_logs_operator_breadcrumbs(tmp_path, caplog):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(
        run.id,
        status="awaiting_fix_approval",
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
    )
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills()

    with caplog.at_level(logging.INFO, logger="plugins.mobile_bug_agent.loop"):
        MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert run.id in logs
    assert "C_MOBILE" in logs
    assert "1710000000.000100" in logs
    assert "MOB-123" in logs
    assert "monica/MOB-123-checkout-crash" in logs
    assert "https://github.com/example/mobile/pull/123" in logs


def test_approved_run_does_not_write_code_outside_approved_pr_rollout(tmp_path):
    config = MonicaConfig(enabled=True, rollout_mode="linear_only")
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this Android checkout crash?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "approved_pr_rollout_not_enabled"
    assert skills.calls == []


def test_approved_run_outside_approved_pr_rollout_logs_blocked_breadcrumbs(tmp_path, caplog):
    config = MonicaConfig(enabled=True, rollout_mode="linear_only")
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this Android checkout crash?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills()

    with caplog.at_level(logging.INFO, logger="plugins.mobile_bug_agent.loop"):
        MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "event=blocked" in logs
    assert "stage=preflight" in logs
    assert "failure_reason=approved_pr_rollout_not_enabled" in logs
    assert run.id in logs
    assert "C_MOBILE" in logs
    assert "1710000000.000100" in logs
    assert "MOB-123" in logs


def test_approved_run_does_not_write_code_without_linear_issue(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        verification=VerificationConfig(commands=("npm test",)),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this Android checkout crash?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "linear_issue_missing_before_fix"
    assert skills.calls == []
    assert "Linear issue" in skills.status_posts[0]


def test_approved_run_does_not_write_code_without_linear_issue_url(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "linear_issue_url_missing_before_fix"
    assert skills.calls == []
    assert "Linear issue URL" in skills.status_posts[0]


def test_approved_run_does_not_write_code_without_verification_commands(tmp_path):
    config = MonicaConfig(enabled=True, rollout_mode="approved_pr")
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this Android checkout crash?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "verification_commands_missing"
    assert skills.calls == []
    assert "verification.commands" in skills.status_posts[0]


@dataclass
class MissingBranchFixSkills(ApprovedFixSkills):
    def run_internal_codex_worker(self, run: Any) -> dict[str, Any]:
        self.calls.append("run_internal_codex_worker")
        return {"changed": True, "summary": "Patched checkout crash"}


def test_approved_run_blocks_when_worker_returns_no_branch(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = MissingBranchFixSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "worker_branch_missing"
    assert updated.branch_name == ""
    assert updated.pr_url == ""
    assert skills.calls == ["run_internal_codex_worker"]
    assert "could not identify a branch" in skills.status_posts[0]


@dataclass
class MismatchedBranchFixSkills(ApprovedFixSkills):
    def run_internal_codex_worker(self, run: Any) -> dict[str, Any]:
        self.calls.append("run_internal_codex_worker")
        return {
            "branch_name": "chandler/MOB-123-checkout-crash",
            "changed": True,
            "summary": "Patched checkout crash",
        }


@dataclass
class WrongIssueBranchFixSkills(ApprovedFixSkills):
    def run_internal_codex_worker(self, run: Any) -> dict[str, Any]:
        self.calls.append("run_internal_codex_worker")
        return {
            "branch_name": "monica/MOB-999-checkout-crash",
            "base_ref": "origin/main",
            "base_commit": "abc1234",
            "changed": True,
            "summary": "Patched checkout crash",
        }


def test_approved_run_blocks_when_worker_returns_mismatched_branch(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = MismatchedBranchFixSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "worker_branch_mismatch"
    assert updated.branch_name == ""
    assert updated.pr_url == ""
    assert skills.calls == ["run_internal_codex_worker"]
    assert "unexpected branch" in skills.status_posts[0]


def test_approved_run_requires_worker_branch_to_match_linear_url_when_identifier_missing(
    tmp_path,
):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(
        run.id,
        status="awaiting_fix_approval",
        linear_url="https://linear.app/acme/issue/MOB-123",
    )
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = WrongIssueBranchFixSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "worker_branch_mismatch"
    assert updated.branch_name == ""
    assert updated.pr_url == ""
    assert skills.calls == ["run_internal_codex_worker"]
    assert "unexpected branch" in skills.status_posts[0]


@dataclass
class NoChangeFixSkills(ApprovedFixSkills):
    def run_internal_codex_worker(self, run: Any) -> dict[str, Any]:
        self.calls.append("run_internal_codex_worker")
        return {
            "branch_name": "monica/MOB-123-checkout-crash",
            "changed": False,
            "summary": "The worker did not make code changes.",
        }


def test_approved_run_blocks_when_worker_reports_no_changes(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = NoChangeFixSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "worker_no_changes"
    assert updated.branch_name == "monica/MOB-123-checkout-crash"
    assert updated.pr_url == ""
    assert skills.calls == ["run_internal_codex_worker"]
    assert "did not report any code changes" in skills.status_posts[0]


def test_failed_verification_blocks_draft_pr(tmp_path, caplog):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(
        run.id,
        status="awaiting_fix_approval",
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
    )
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ApprovedFixSkills(verification_passed=False)

    with caplog.at_level(logging.INFO, logger="plugins.mobile_bug_agent.loop"):
        MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.pr_url == ""
    assert "verification_failed" in updated.failure_reason
    assert skills.calls == ["run_internal_codex_worker", "run_verification"]
    assert "Verification failed, so I did not open a PR." in skills.status_posts[0]
    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "event=blocked" in logs
    assert "stage=verifying" in logs
    assert run.id in logs
    assert "C_MOBILE" in logs
    assert "1710000000.000100" in logs
    assert "MOB-123" in logs
    assert "https://linear.app/acme/issue/MOB-123" in logs
    assert "monica/MOB-123-checkout-crash" in logs
    assert "verification_failed: npm test" in logs


@dataclass
class FailingPrSkills(ApprovedFixSkills):
    def open_draft_pr(
        self,
        run: Any,
        worker_result: dict[str, Any],
        verification: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append("open_draft_pr")
        raise RuntimeError("gh pr create failed")


@dataclass
class ProofFailingPrSkills(ApprovedFixSkills):
    def open_draft_pr(
        self,
        run: Any,
        worker_result: dict[str, Any],
        verification: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append("open_draft_pr")
        raise RuntimeError("shareable proof links are required before draft PR publishing: missing android.")


@dataclass
class MissingPrUrlSkills(ApprovedFixSkills):
    def open_draft_pr(
        self,
        run: Any,
        worker_result: dict[str, Any],
        verification: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append("open_draft_pr")
        return {"url": ""}


@dataclass
class InvalidPrUrlSkills(ApprovedFixSkills):
    def open_draft_pr(
        self,
        run: Any,
        worker_result: dict[str, Any],
        verification: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append("open_draft_pr")
        return {"url": "https://github.com/example/mobile/issues/123"}


@dataclass
class NonGithubPrUrlSkills(ApprovedFixSkills):
    def open_draft_pr(
        self,
        run: Any,
        worker_result: dict[str, Any],
        verification: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append("open_draft_pr")
        return {"url": "https://example.com/example/mobile/pull/123"}


@dataclass
class CancellingPrSkills(ApprovedFixSkills):
    state: MonicaState | None = None

    def open_draft_pr(
        self,
        run: Any,
        worker_result: dict[str, Any],
        verification: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append("open_draft_pr")
        assert self.state is not None
        self.state.update_run(
            run.id,
            status="blocked",
            failure_reason="cancelled by U_APPROVER",
        )
        return {"url": "https://github.com/example/mobile/pull/123"}


def test_loop_does_not_overwrite_cancellation_during_pr_opening(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = CancellingPrSkills(state=state)

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "cancelled by U_APPROVER"
    assert updated.pr_url == "https://github.com/example/mobile/pull/123"
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof", "open_draft_pr"]
    assert not any("Draft PR is ready" in post for post in skills.status_posts)


def test_loop_blocks_when_pr_publisher_returns_no_url(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = MissingPrUrlSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "draft_pr_url_missing"
    assert updated.pr_url == ""
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof", "open_draft_pr"]
    assert "did not return a draft PR URL" in skills.status_posts[0]


def test_loop_blocks_when_pr_publisher_returns_non_pull_request_url(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = InvalidPrUrlSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "draft_pr_url_invalid"
    assert updated.pr_url == ""
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof", "open_draft_pr"]
    assert "did not return a valid draft PR URL" in skills.status_posts[0]


def test_loop_blocks_when_pr_publisher_returns_non_github_pr_url(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = NonGithubPrUrlSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "blocked"
    assert updated.failure_reason == "draft_pr_url_invalid"
    assert updated.pr_url == ""
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof", "open_draft_pr"]
    assert "did not return a valid draft PR URL" in skills.status_posts[0]
    assert not any("Draft PR is ready" in post for post in skills.status_posts)


def test_loop_keeps_publisher_proof_refusal_as_proof_blocked(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = ProofFailingPrSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "proof_blocked"
    assert updated.failure_reason == (
        "proof_unavailable: shareable proof links are required before draft PR publishing: missing android."
    )
    assert updated.pr_url == ""
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof", "open_draft_pr"]
    assert "final proof validation failed" in skills.status_posts[0]
    assert "so I did not open a PR" in skills.status_posts[0]


def test_loop_marks_unexpected_pr_failure_with_stage(tmp_path):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_TAGGER",)),
        verification=VerificationConfig(commands=("npm test",)),
        proof=_approved_pr_proof_config(),
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="can you fix this marketplace PDP copy bug?",
        raw_event=_approved_pr_raw_event(),
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123", linear_url="https://linear.app/acme/issue/MOB-123")
    state.approve_fix(run.id, approved_by_user_id="U_TAGGER")
    skills = FailingPrSkills()

    MonicaLoop(config=config, state=state, skills=skills).run(run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert updated.status == "failed"
    assert updated.failure_reason == "opening_pr_failed: gh pr create failed"
    assert skills.calls == ["run_internal_codex_worker", "run_verification", "run_proof", "open_draft_pr"]
    assert "Stage: opening_pr" in skills.status_posts[0]
