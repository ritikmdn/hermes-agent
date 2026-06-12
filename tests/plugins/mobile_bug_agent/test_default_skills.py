from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from typing import Any

from plugins.mobile_bug_agent.config import LinearConfig, MonicaConfig, ProofConfig, SlackConfig, VerificationConfig
from plugins.mobile_bug_agent.repo_manager import Worktree
from plugins.mobile_bug_agent.skills import DefaultMonicaSkills
from plugins.mobile_bug_agent.state import MonicaState


@dataclass
class FakeLinearClient:
    calls: list[Any]
    attachment_calls: list[Any] = field(default_factory=list)
    fail_attachment: bool = False

    def create_or_update_issue(self, payload: Any, *, existing_issue_id: str = "") -> Any:
        self.calls.append((payload, existing_issue_id))

        @dataclass(frozen=True)
        class Issue:
            id: str = "issue-id"
            identifier: str = "MOB-123"
            url: str = "https://linear.app/acme/issue/MOB-123"

        return Issue()

    def create_attachment(self, payload: Any) -> Any:
        self.attachment_calls.append(payload)
        if self.fail_attachment:
            raise RuntimeError("Linear attachment failed")

        @dataclass(frozen=True)
        class Attachment:
            id: str = "attachment-id"
            title: str = payload.title
            url: str = payload.url

        return Attachment()


class FailingSlackClient:
    def post_thread_reply(self, *, channel_id: str, thread_ts: str, text: str) -> None:
        raise RuntimeError("not_in_channel")


@dataclass
class CapturingSlackClient:
    posts: list[dict[str, str]] = field(default_factory=list)

    def post_thread_reply(self, *, channel_id: str, thread_ts: str, text: str) -> None:
        self.posts.append({"channel_id": channel_id, "thread_ts": thread_ts, "text": text})


@dataclass
class CapturingProofRunner:
    calls: list[dict[str, Any]]

    def run(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)

        @dataclass(frozen=True)
        class Result:
            def to_dict(self) -> dict[str, Any]:
                return {
                    "passed": True,
                    "summary": "Proof captured.",
                    "artifacts": ["/tmp/ios.png"],
                    "platforms": ["ios"],
                }

        return Result()


def test_default_skills_create_real_linear_issue_when_not_dry_run(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Android checkout crashes after promo code",
    )
    calls: list[Any] = []
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="linear_only",
            linear=LinearConfig(team_id="team-id", project_id="project-id"),
        ),
        state=state,
        linear_client=FakeLinearClient(calls),
    )

    issue = skills.create_or_update_linear(
        run,
        {"permalink": "https://example.slack.com/thread", "messages": [run.request_text]},
        {
            "summary": "Android checkout crashes after promo code",
            "confidence": 0.91,
        },
    )

    payload, existing_issue_id = calls[0]
    assert existing_issue_id == ""
    assert payload.team_id == "team-id"
    assert payload.project_id == "project-id"
    assert payload.title == "[Mobile] Android checkout crashes after promo code"
    assert "https://example.slack.com/thread" in payload.description
    assert issue == {
        "id": "issue-id",
        "identifier": "MOB-123",
        "url": "https://linear.app/acme/issue/MOB-123",
        "dry_run": False,
        "title": "[Mobile] Android checkout crashes after promo code",
        "description": payload.description,
        "attachments": [],
        "attachment_errors": [],
    }
    assert issue["description"] == payload.description


def test_default_skills_bounds_linear_issue_title_from_agentic_summary(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Android checkout crashes after promo code",
    )
    calls: list[Any] = []
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="linear_only",
            linear=LinearConfig(team_id="team-id", project_id="project-id"),
        ),
        state=state,
        linear_client=FakeLinearClient(calls),
    )
    long_summary = " ".join(["Android checkout crashes after promo code"] * 12)

    issue = skills.create_or_update_linear(
        run,
        {"permalink": "https://example.slack.com/thread", "messages": [run.request_text]},
        {
            "summary": long_summary,
            "confidence": 0.91,
        },
    )

    payload, _existing_issue_id = calls[0]
    assert len(payload.title) <= 120
    assert payload.title.startswith("[Mobile] Android checkout crashes after promo code")
    assert payload.title.endswith("...")
    assert issue["title"] == payload.title


def test_default_skills_fallback_thread_uses_raw_slack_files(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Android checkout crashes after promo code",
        raw_event={
            "permalink": "https://example.slack.com/thread",
            "files": [
                {
                    "id": "F1",
                    "name": "crash.png",
                    "mimetype": "image/png",
                    "url_private": "https://files/crash.png",
                    "permalink": "https://example.slack.com/file/F1",
                }
            ],
        },
    )
    skills = DefaultMonicaSkills(config=MonicaConfig(), state=state)

    thread = skills.read_slack_thread(run)
    description = skills._linear_description(
        run,
        thread,
        {
            "summary": "Android checkout crashes after promo code",
            "confidence": 0.91,
        },
    )

    assert thread["permalink"] == "https://example.slack.com/thread"
    assert thread["files"][0]["name"] == "crash.png"
    assert thread["files"][0]["permalink"] == "https://example.slack.com/file/F1"
    assert "crash.png (image/png)" in description
    assert "https://example.slack.com/thread" in description


def test_default_skills_fallback_thread_preserves_triggering_message_metadata(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Android checkout crashes after promo code",
        raw_event={
            "permalink": "https://example.slack.com/thread",
            "ts": "1710000000.000200",
            "user": "U_TAGGER",
            "text": "<@BMONICA> Android checkout crashes after promo code",
        },
    )
    skills = DefaultMonicaSkills(config=MonicaConfig(), state=state)

    thread = skills.read_slack_thread(run)
    description = skills._linear_description(
        run,
        thread,
        {
            "summary": "Android checkout crashes after promo code",
            "confidence": 0.91,
        },
    )

    assert thread["channel_id"] == "C_MOBILE"
    assert thread["thread_ts"] == "1710000000.000100"
    assert thread["message_details"] == [
        {
            "user_id": "U_TAGGER",
            "text": "Android checkout crashes after promo code",
            "ts": "1710000000.000200",
            "permalink": "https://example.slack.com/thread",
        }
    ]
    assert (
        "- U_TAGGER at 1710000000.000200: Android checkout crashes after promo code "
        "(https://example.slack.com/thread)"
    ) in description


def test_default_skills_fallback_thread_uses_raw_slack_attachment_images(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Android checkout crashes after promo code",
        raw_event={
            "permalink": "https://example.slack.com/thread",
            "attachments": [
                {
                    "title": "Checkout crash screenshot",
                    "image_url": "https://files.example.com/checkout-crash.png",
                    "fallback": "checkout screenshot",
                }
            ],
        },
    )
    skills = DefaultMonicaSkills(config=MonicaConfig(), state=state)

    thread = skills.read_slack_thread(run)
    description = skills._linear_description(
        run,
        thread,
        {
            "summary": "Android checkout crashes after promo code",
            "confidence": 0.91,
        },
    )

    assert thread["files"] == [
        {
            "id": "attachment-1710000000.000200-1",
            "name": "Checkout crash screenshot",
            "mimetype": "image",
            "url_private": "https://files.example.com/checkout-crash.png",
            "permalink": "https://files.example.com/checkout-crash.png",
        }
    ]
    assert thread["attachments"] == ["Checkout crash screenshot"]
    assert "Checkout crash screenshot (image)" in description


def test_default_skills_simulated_runs_do_not_call_slack_client(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="LOCAL",
        thread_ts="sim-thread",
        message_ts="sim-message",
        user_id="local-operator",
        request_text="Android checkout crashes after promo code",
        raw_event={
            "monica_simulated": True,
            "permalink": "local://monica/simulated/sim-thread",
            "files": [
                {
                    "id": "F1",
                    "name": "simulated-crash.png",
                    "mimetype": "image/png",
                    "permalink": "local://monica/simulated/file/F1",
                }
            ],
        },
    )

    class RaisingSlackClient:
        def read_thread(self, **_: Any) -> Any:
            raise AssertionError("simulated runs must not fetch Slack")

    skills = DefaultMonicaSkills(
        config=MonicaConfig(),
        state=state,
        slack_client=RaisingSlackClient(),
    )

    thread = skills.read_slack_thread(run)

    assert thread["permalink"] == "local://monica/simulated/sim-thread"
    assert thread["messages"] == ["Android checkout crashes after promo code"]
    assert thread["files"][0]["name"] == "simulated-crash.png"


def test_default_skills_dry_run_returns_full_linear_payload(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Android checkout crashes after promo code",
        raw_event={
            "permalink": "https://example.slack.com/thread",
            "files": [
                {
                    "id": "F1",
                    "name": "crash.png",
                    "mimetype": "image/png",
                    "url_private": "https://files/crash.png",
                    "permalink": "https://example.slack.com/file/F1",
                }
            ],
        },
    )
    skills = DefaultMonicaSkills(config=MonicaConfig(rollout_mode="dry_run"), state=state)
    thread = skills.read_slack_thread(run)

    issue = skills.create_or_update_linear(
        run,
        thread,
        {
            "summary": "Android checkout crashes after promo code",
            "confidence": 0.91,
            "wants_fix": True,
            "reason": "Crash report with Android context.",
        },
    )

    assert issue["dry_run"] is True
    assert issue["title"] == "[Mobile] Android checkout crashes after promo code"
    assert "https://example.slack.com/thread" in issue["description"]
    assert "crash.png (image/png)" in issue["description"]
    assert "Dry run only; no code changes will run." in issue["description"]
    assert issue["attachments"] == [
        {
            "title": "crash.png",
            "url": "https://example.slack.com/file/F1",
            "subtitle": "image/png",
        }
    ]


def test_default_skills_linear_description_includes_structured_bug_context(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Android checkout crashes after promo code",
    )
    skills = DefaultMonicaSkills(config=MonicaConfig(rollout_mode="dry_run"), state=state)

    description = skills._linear_description(
        run,
        {
            "permalink": "https://example.slack.com/thread",
            "messages": [
                "Android checkout crashes after promo code",
                "Pixel 7, latest beta, happens after tapping Pay",
            ],
        },
        {
            "summary": "Android checkout crashes after applying promo code",
            "observed_behavior": "Checkout crashes after applying a promo code.",
            "expected_behavior": "Checkout should keep the cart open and apply the discount.",
            "reproduction_steps": ["Open checkout", "Apply a promo code", "Tap Pay"],
            "platforms": ["Android"],
            "device_context": "Pixel 7",
            "build_context": "2.14.0 beta",
            "confidence": 0.91,
            "reason": "Crash report with platform and reproduction context.",
        },
    )

    assert "## Observed Behavior\nCheckout crashes after applying a promo code." in description
    assert "## Expected Behavior\nCheckout should keep the cart open and apply the discount." in description
    assert "## Reproduction Context\n- Open checkout\n- Apply a promo code\n- Tap Pay" in description
    assert "## Platform And Build\n- Platforms: Android\n- Device context: Pixel 7\n- Build context: 2.14.0 beta" in description


def test_default_skills_linear_description_prefers_detailed_thread_messages(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Android checkout crashes after promo code",
    )
    skills = DefaultMonicaSkills(config=MonicaConfig(rollout_mode="dry_run"), state=state)

    description = skills._linear_description(
        run,
        {
            "permalink": "https://example.slack.com/thread",
            "messages": ["legacy fallback message"],
            "message_details": [
                {
                    "user_id": "U1",
                    "text": "Android checkout crashes after promo code",
                    "ts": "1710000000.000100",
                    "permalink": "https://example.slack.com/msg/1",
                },
                {
                    "user_id": "U2",
                    "text": "Pixel 7, latest beta",
                    "ts": "1710000001.000200",
                    "permalink": "https://example.slack.com/msg/2",
                },
            ],
        },
        {
            "summary": "Android checkout crashes after applying promo code",
            "confidence": 0.91,
        },
    )

    assert "- U1 at 1710000000.000100: Android checkout crashes after promo code (https://example.slack.com/msg/1)" in description
    assert "- U2 at 1710000001.000200: Pixel 7, latest beta (https://example.slack.com/msg/2)" in description
    assert "legacy fallback message" not in description


def test_default_skills_linear_description_includes_evidence_links(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Android checkout crashes after promo code",
    )
    skills = DefaultMonicaSkills(config=MonicaConfig(rollout_mode="dry_run"), state=state)

    description = skills._linear_description(
        run,
        {
            "permalink": "https://example.slack.com/thread",
            "messages": [run.request_text],
            "files": [
                {
                    "id": "F1",
                    "name": "crash.png",
                    "mimetype": "image/png",
                    "permalink": "https://example.slack.com/file/F1",
                }
            ],
        },
        {"summary": "Android checkout crashes after promo code", "confidence": 0.91},
    )

    assert "- crash.png (image/png): https://example.slack.com/file/F1" in description


def test_default_skills_linear_description_marks_fix_disabled_in_linear_only_mode(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Android checkout crashes after promo code, please fix it",
    )
    skills = DefaultMonicaSkills(config=MonicaConfig(rollout_mode="linear_only"), state=state)

    description = skills._linear_description(
        run,
        {"permalink": "https://example.slack.com/thread", "messages": [run.request_text]},
        {
            "summary": "Android checkout crashes after applying promo code",
            "wants_fix": True,
        },
    )

    assert "Code fixes are disabled in the current Monica rollout mode." in description
    assert "Awaiting explicit tagged approval before code changes." not in description


def test_default_skills_attaches_slack_file_links_to_linear_issue(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Android checkout crashes after promo code",
    )
    calls: list[Any] = []
    client = FakeLinearClient(calls)
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="linear_only",
            linear=LinearConfig(team_id="team-id", project_id="project-id"),
        ),
        state=state,
        linear_client=client,
    )

    issue = skills.create_or_update_linear(
        run,
        {
            "permalink": "https://example.slack.com/thread",
            "messages": [run.request_text],
            "files": [
                {
                    "id": "F1",
                    "name": "crash.png",
                    "mimetype": "image/png",
                    "url_private": "https://files/crash.png",
                    "permalink": "https://example.slack.com/file/F1",
                }
            ],
        },
        {
            "summary": "Android checkout crashes after promo code",
            "confidence": 0.91,
        },
    )

    assert client.attachment_calls[0].issue_id == "issue-id"
    assert client.attachment_calls[0].title == "crash.png"
    assert client.attachment_calls[0].url == "https://example.slack.com/file/F1"
    assert issue["attachments"] == [
        {
            "id": "attachment-id",
            "title": "crash.png",
            "url": "https://example.slack.com/file/F1",
        }
    ]
    assert issue["attachment_errors"] == []


def test_default_skills_skips_unsupported_evidence_urls_for_linear_issue(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Android checkout crashes after promo code",
    )
    calls: list[Any] = []
    client = FakeLinearClient(calls)
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="linear_only",
            linear=LinearConfig(team_id="team-id", project_id="project-id"),
        ),
        state=state,
        linear_client=client,
    )

    issue = skills.create_or_update_linear(
        run,
        {
            "permalink": "https://example.slack.com/thread",
            "messages": [run.request_text],
            "files": [
                {
                    "id": "F1",
                    "name": "crash.png",
                    "mimetype": "image/png",
                    "url_private": "file:///Users/ritik/.hermes/secrets",
                    "permalink": "file:///Users/ritik/.hermes/secrets",
                }
            ],
        },
        {
            "summary": "Android checkout crashes after promo code",
            "confidence": 0.91,
        },
    )

    payload, _existing_issue_id = calls[0]
    assert client.attachment_calls == []
    assert issue["attachments"] == []
    assert "file:///Users/ritik/.hermes/secrets" not in payload.description
    assert "- crash.png (image/png)" in payload.description


def test_default_skills_returns_created_linear_description(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Android checkout crashes after promo code",
    )
    calls: list[Any] = []
    client = FakeLinearClient(calls)
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="linear_only",
            linear=LinearConfig(team_id="team-id"),
        ),
        state=state,
        linear_client=client,
    )

    issue = skills.create_or_update_linear(
        run,
        {"permalink": "https://example.slack.com/thread", "messages": [run.request_text]},
        {"summary": "Android checkout crashes after promo code", "confidence": 0.91},
    )

    payload, _existing_issue_id = calls[0]
    assert issue["description"] == payload.description
    assert "## Slack Context" in issue["description"]
    assert "https://example.slack.com/thread" in issue["description"]


def test_default_skills_keeps_linear_issue_when_attachment_fails(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Android checkout crashes after promo code",
    )
    calls: list[Any] = []
    client = FakeLinearClient(calls, fail_attachment=True)
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="linear_only",
            linear=LinearConfig(team_id="team-id", project_id="project-id"),
        ),
        state=state,
        linear_client=client,
    )

    issue = skills.create_or_update_linear(
        run,
        {
            "permalink": "https://example.slack.com/thread",
            "messages": [run.request_text],
            "files": [
                {
                    "id": "F1",
                    "name": "crash.png",
                    "mimetype": "image/png",
                    "url_private": "https://files/crash.png",
                }
            ],
        },
        {
            "summary": "Android checkout crashes after promo code",
            "confidence": 0.91,
        },
    )

    assert issue["id"] == "issue-id"
    assert issue["attachments"] == []
    assert issue["attachment_errors"] == ["crash.png: Linear attachment failed"]


def test_default_skills_logs_slack_status_post_failures(tmp_path, caplog):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Android checkout crashes after promo code",
    )
    skills = DefaultMonicaSkills(
        config=MonicaConfig(),
        state=state,
        slack_client=FailingSlackClient(),
    )

    with caplog.at_level(logging.WARNING, logger="plugins.mobile_bug_agent.skills"):
        skills.post_status(run, "Created Linear issue")

    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "Slack status post failed" in logs
    assert run.id in logs
    assert "C_MOBILE" in logs
    assert "1710000000.000100" in logs
    assert "not_in_channel" in logs


def test_default_skills_approval_prompt_names_configured_approvers(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Android checkout crashes after promo code",
    )
    slack_client = CapturingSlackClient()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            slack=SlackConfig(approver_user_ids=("U_APPROVER",)),
        ),
        state=state,
        slack_client=slack_client,
    )

    skills.ask_fix_approval(
        run,
        {
            "identifier": "MOB-123",
            "url": "https://linear.app/acme/issue/MOB-123",
        },
    )

    assert slack_client.posts == [
        {
            "channel_id": "C_MOBILE",
            "thread_ts": "1710000000.000100",
            "text": (
                "I filed the mobile bug context and I am waiting for approval before code changes.\n"
                "Issue: https://linear.app/acme/issue/MOB-123\n"
                "Tag me in this thread with `approved, fix it` when you want me to start.\n"
                "Allowed approvers: <@U_APPROVER>."
            ),
        }
    ]


def test_default_skills_updates_existing_linear_issue_when_run_has_issue_id(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Android checkout crashes after promo code",
    )
    run = state.update_run(run.id, linear_issue_id="issue-id")
    calls: list[Any] = []
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="linear_only",
            linear=LinearConfig(team_id="team-id", project_id="project-id"),
        ),
        state=state,
        linear_client=FakeLinearClient(calls),
    )

    skills.create_or_update_linear(
        run,
        {"permalink": "https://example.slack.com/thread", "messages": [run.request_text]},
        {
            "summary": "Android checkout crashes after promo code",
            "confidence": 0.91,
        },
    )

    assert calls[0][1] == "issue-id"


@dataclass
class FakeRepoManager:
    calls: list[Any]

    def prepare_worktree(self, *, linear_identifier: str, summary: str) -> Worktree:
        self.calls.append((linear_identifier, summary))
        return Worktree(
            branch_name="monica/MOB-123-checkout-crash",
            path=PathLike("/tmp/monica-worktree"),
        )


@dataclass(frozen=True)
class PathLike:
    value: str

    def __str__(self) -> str:
        return self.value


@dataclass
class FakeCodexWorker:
    calls: list[Any]

    def run(self, *, run: Any, worktree: Worktree, brief: str) -> dict[str, Any]:
        self.calls.append((run.id, worktree.branch_name, str(worktree.path), brief))
        return {"changed": True, "summary": "Patched checkout crash"}


@dataclass
class FixedRepoManager:
    worktree_path: Any
    calls: list[Any] = field(default_factory=list)

    def prepare_worktree(self, *, linear_identifier: str, summary: str) -> Worktree:
        self.calls.append((linear_identifier, summary))
        return Worktree(
            branch_name="monica/MOB-123-checkout-crash",
            path=self.worktree_path,
        )


def test_default_skills_prepare_worktree_then_call_codex_worker(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Android checkout crashes after promo code",
    )
    run = state.update_run(run.id, linear_identifier="MOB-123")
    repo_calls: list[Any] = []
    worker_calls: list[Any] = []
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            verification=VerificationConfig(commands=("npm test",)),
        ),
        state=state,
        repo_manager=FakeRepoManager(repo_calls),
        codex_worker=FakeCodexWorker(worker_calls),
    )

    result = skills.run_internal_codex_worker(run)

    assert repo_calls == [("MOB-123", "Android checkout crashes after promo code")]
    assert worker_calls[0][1] == "monica/MOB-123-checkout-crash"
    assert "Android checkout crashes after promo code" in worker_calls[0][3]
    assert "## Slack Thread Context" in worker_calls[0][3]
    assert "## Verification Commands" in worker_calls[0][3]
    assert "npm test" in worker_calls[0][3]
    assert result == {
        "changed": True,
        "summary": "Patched checkout crash",
        "branch_name": "monica/MOB-123-checkout-crash",
        "worktree_path": "/tmp/monica-worktree",
        "slack_permalink": "",
        "evidence": [],
    }


def test_default_skills_marks_worker_summary_noop_when_worktree_has_no_git_changes(tmp_path):
    worktree_path = tmp_path / "mobile-worktree"
    worktree_path.mkdir()
    subprocess.run(["git", "init"], cwd=worktree_path, check=True, capture_output=True, text=True)
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Android checkout crashes after promo code",
    )
    run = state.update_run(run.id, linear_identifier="MOB-123")
    skills = DefaultMonicaSkills(
        config=MonicaConfig(rollout_mode="approved_pr"),
        state=state,
        repo_manager=FixedRepoManager(worktree_path),
        codex_worker=FakeCodexWorker([]),
    )

    result = skills.run_internal_codex_worker(run)

    assert result["summary"] == "Patched checkout crash"
    assert result["changed"] is False


def test_default_skills_marks_worker_changed_when_worktree_has_git_changes(tmp_path):
    worktree_path = tmp_path / "mobile-worktree"
    worktree_path.mkdir()
    subprocess.run(["git", "init"], cwd=worktree_path, check=True, capture_output=True, text=True)
    (worktree_path / "Checkout.tsx").write_text("export const fixed = true;\n", encoding="utf-8")
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Android checkout crashes after promo code",
    )
    run = state.update_run(run.id, linear_identifier="MOB-123")

    class ConservativeWorker:
        def run(self, *, run: Any, worktree: Worktree, brief: str) -> dict[str, Any]:
            return {"changed": False, "summary": "I made the checkout fix."}

    skills = DefaultMonicaSkills(
        config=MonicaConfig(rollout_mode="approved_pr"),
        state=state,
        repo_manager=FixedRepoManager(worktree_path),
        codex_worker=ConservativeWorker(),
    )

    result = skills.run_internal_codex_worker(run)

    assert result["summary"] == "I made the checkout fix."
    assert result["changed"] is True


def test_default_skills_marks_worker_changed_when_worker_committed_diff(tmp_path):
    worktree_path = tmp_path / "mobile-worktree"
    worktree_path.mkdir()
    subprocess.run(["git", "init"], cwd=worktree_path, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "config", "user.name", "Monica Test"],
        cwd=worktree_path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "monica-test@example.com"],
        cwd=worktree_path,
        check=True,
        capture_output=True,
        text=True,
    )
    (worktree_path / "Checkout.tsx").write_text("export const fixed = false;\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=worktree_path, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "commit", "-m", "base"],
        cwd=worktree_path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
        cwd=worktree_path,
        check=True,
        capture_output=True,
        text=True,
    )
    (worktree_path / "Checkout.tsx").write_text("export const fixed = true;\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=worktree_path, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "commit", "-m", "fix checkout"],
        cwd=worktree_path,
        check=True,
        capture_output=True,
        text=True,
    )
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Android checkout crashes after promo code",
    )
    run = state.update_run(run.id, linear_identifier="MOB-123")

    class QuietWorker:
        def run(self, *, run: Any, worktree: Worktree, brief: str) -> dict[str, Any]:
            return {"changed": False, "summary": "Committed the checkout fix."}

    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            verification=VerificationConfig(commands=("npm test",)),
        ),
        state=state,
        repo_manager=FixedRepoManager(worktree_path),
        codex_worker=QuietWorker(),
    )

    result = skills.run_internal_codex_worker(run)

    assert subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=worktree_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout == ""
    assert result["summary"] == "Committed the checkout fix."
    assert result["changed"] is True


def test_default_skills_worker_result_carries_slack_permalink_and_evidence(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Android checkout crashes after promo code",
        raw_event={
            "permalink": "https://example.slack.com/thread",
            "files": [
                {
                    "id": "F1",
                    "name": "crash.png",
                    "mimetype": "image/png",
                    "permalink": "https://example.slack.com/file/F1",
                }
            ],
        },
    )
    run = state.update_run(run.id, linear_identifier="MOB-123")
    skills = DefaultMonicaSkills(
        config=MonicaConfig(rollout_mode="approved_pr"),
        state=state,
        repo_manager=FakeRepoManager([]),
        codex_worker=FakeCodexWorker([]),
    )

    result = skills.run_internal_codex_worker(run)

    assert result["slack_permalink"] == "https://example.slack.com/thread"
    assert result["evidence"] == [
        {
            "name": "crash.png",
            "mimetype": "image/png",
            "url": "https://example.slack.com/file/F1",
        }
    ]


def test_default_skills_run_proof_passes_worker_proof_deep_link(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Hard-coded PDP tags",
    )
    proof_runner = CapturingProofRunner([])
    skills = DefaultMonicaSkills(
        config=MonicaConfig(proof=ProofConfig(commands=("capture-proof",))),
        state=state,
        proof_runner=proof_runner,
    )

    result = skills.run_proof(
        run,
        {
            "worktree_path": str(tmp_path),
            "proof_deep_link": "elixir-card://marketplace/offer/fitness-first",
        },
        {"passed": True},
    )

    assert result["passed"] is True
    assert proof_runner.calls[0]["proof_target"] == {
        "deep_link": "elixir-card://marketplace/offer/fitness-first"
    }


def test_default_skills_pr_body_includes_slack_permalink_and_evidence(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Android checkout crashes after promo code",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
    )

    body = DefaultMonicaSkills._pr_body(
        run=run,
        worker_result={
            "summary": "Patched checkout crash.",
            "slack_permalink": "https://example.slack.com/thread",
            "evidence": [
                {
                    "name": "crash.png",
                    "mimetype": "image/png",
                    "url": "https://example.slack.com/file/F1",
                }
            ],
            "proof": {
                "summary": "Proof captured.",
                "platforms": ["ios", "android"],
                "proof_target": {
                    "deep_link": "elixir-card://marketplace/offer/fitness-first",
                },
                "artifacts": [
                    "/tmp/monica-proof/monica-proof-manifest.json",
                    "/tmp/monica-proof/ios-pdp-fixed.png",
                    "/tmp/monica-proof/android-pdp-fixed.mp4",
                ],
            },
        },
        verification={"summary": "Verification passed.", "output": "npm test\nok"},
    )

    assert "- Slack: https://example.slack.com/thread" in body
    assert "## Evidence" in body
    assert "- crash.png (image/png): https://example.slack.com/file/F1" in body
    assert "## Proof" in body
    assert "Proof captured." in body
    assert "Platforms: ios, android" in body
    assert "Target: elixir-card://marketplace/offer/fitness-first" in body
    assert "- /tmp/monica-proof/monica-proof-manifest.json" in body
    assert "- /tmp/monica-proof/ios-pdp-fixed.png" in body
    assert "- /tmp/monica-proof/android-pdp-fixed.mp4" in body


def test_default_skills_linear_description_keeps_context_gaps(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Hard-coded PDP tags",
    )
    skills = DefaultMonicaSkills(config=MonicaConfig(), state=state)

    description = skills._linear_description(
        run,
        {"permalink": "https://example.slack.com/thread", "messages": [run.request_text]},
        {
            "summary": "Hard-coded PDP tags",
            "confidence": 0.96,
            "missing_questions": [
                "Which app build/environment should be used to verify the issue?",
                "Are there specific PDP examples where this was observed?",
            ],
        },
    )

    assert "## Open Questions / Context Gaps" in description
    assert "- Which app build/environment should be used to verify the issue?" in description
    assert "- Are there specific PDP examples where this was observed?" in description


def test_default_skills_pr_body_omits_unsupported_evidence_urls(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Android checkout crashes after promo code",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
    )

    body = DefaultMonicaSkills._pr_body(
        run=run,
        worker_result={
            "summary": "Patched checkout crash",
            "slack_permalink": "https://example.slack.com/thread",
            "evidence": [
                {
                    "name": "crash.png",
                    "mimetype": "image/png",
                    "url": "file:///Users/ritik/.hermes/secrets",
                }
            ],
        },
        verification={"summary": "Verification passed.", "output": "$ npm test\nok"},
    )

    assert "file:///Users/ritik/.hermes/secrets" not in body
    assert "- crash.png (image/png)" in body
    assert "npm test\nok" in body
