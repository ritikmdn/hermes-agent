from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from plugins.mobile_bug_agent.config import (
    LinearConfig,
    MonicaConfig,
    ProofConfig,
    RepoConfig,
    SlackConfig,
    VerificationConfig,
)
from plugins.mobile_bug_agent.pr_publisher import DraftPrPublisherError
from plugins.mobile_bug_agent.repo_manager import Worktree
from plugins.mobile_bug_agent.skills import DefaultMonicaSkills
from plugins.mobile_bug_agent.state import MonicaState


@dataclass
class FakeLinearClient:
    calls: list[Any]
    attachment_calls: list[Any] = field(default_factory=list)
    comment_calls: list[Any] = field(default_factory=list)
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

    def create_comment(self, payload: Any) -> Any:
        self.comment_calls.append(payload)

        @dataclass(frozen=True)
        class Comment:
            id: str = "comment-id"
            url: str = "https://linear.app/acme/issue/MOB-123#comment-id"

        return Comment()


class FailingSlackClient:
    def post_thread_reply(self, *, channel_id: str, thread_ts: str, text: str) -> None:
        raise RuntimeError("not_in_channel")


@dataclass
class CapturingSlackClient:
    posts: list[dict[str, str]] = field(default_factory=list)
    uploads: list[dict[str, str]] = field(default_factory=list)

    def post_thread_reply(self, *, channel_id: str, thread_ts: str, text: str) -> None:
        self.posts.append({"channel_id": channel_id, "thread_ts": thread_ts, "text": text})

    def upload_thread_file(
        self,
        *,
        channel_id: str,
        thread_ts: str,
        file_path: str,
        title: str,
        initial_comment: str = "",
    ) -> dict[str, str]:
        self.uploads.append(
            {
                "channel_id": channel_id,
                "thread_ts": thread_ts,
                "file_path": file_path,
                "title": title,
                "initial_comment": initial_comment,
            }
        )
        filename = file_path.rsplit("/", 1)[-1]
        return {
            "id": f"F-{filename}",
            "name": filename,
            "permalink": f"https://slack.example/files/{filename}",
        }


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


@dataclass
class CapturingPrPublisher:
    calls: list[dict[str, Any]] = field(default_factory=list)

    def publish(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "https://github.com/acme/mobile/pull/123"


@dataclass
class InvalidUrlPrPublisher(CapturingPrPublisher):
    def publish(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "https://github.com/acme/mobile/issues/123"


def _valid_approved_pr_proof(
    proof_dir: Any | None = None,
    *,
    run_id: str = "",
    worktree: str = "",
) -> dict[str, Any]:
    if proof_dir is None:
        manifest_artifact = "/tmp/monica-proof/monica-proof-manifest.json"
        ios_artifact = "/tmp/monica-proof/ios-pdp-fixed.png"
        android_artifact = "/tmp/monica-proof/android-pdp-fixed.mp4"
        ios_target_artifact = "/tmp/monica-proof/ios-target.log"
        android_target_artifact = "/tmp/monica-proof/android-ui.xml"
        ios_route_artifact = "/tmp/monica-proof/ios-metro.stdout.log"
        android_route_artifact = "/tmp/monica-proof/android-metro.stdout.log"
    else:
        root = Path(proof_dir)
        root.mkdir(parents=True, exist_ok=True)
        manifest_path = root / "monica-proof-manifest.json"
        ios_path = root / "ios-pdp-fixed.png"
        android_path = root / "android-pdp-fixed.mp4"
        ios_target_path = root / "ios-target.log"
        android_target_path = root / "android-ui.xml"
        ios_route_path = root / "ios-metro.stdout.log"
        android_route_path = root / "android-metro.stdout.log"
        proof_artifact_paths = [
            ios_path,
            android_path,
            ios_target_path,
            android_target_path,
            ios_route_path,
            android_route_path,
        ]
        manifest_payload = {
            "linear_identifier": "MOB-123",
            "linear_url": "https://linear.app/acme/issue/MOB-123",
            "branch_name": "monica/MOB-123-pdp-copy",
            "base_commit": "abc1234",
            "base_ref": "origin/dev",
            "proof_target": {
                "deep_link": "elixir-card://marketplace/offer/fitness-first",
                "expected_text": "Fitness First",
            },
            "setup_commands": ["npm run monica:seed-auth"],
            "commands": ["npm run monica:proof"],
            "platforms": ["ios", "android"],
            "proof_artifacts": [str(path) for path in proof_artifact_paths],
            "required_env_keys": ["MONICA_TEST_LOGIN_TOKEN"],
        }
        if run_id:
            manifest_payload["run_id"] = run_id
        if worktree:
            manifest_payload["worktree"] = worktree
        manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
        ios_path.write_bytes(b"ios proof")
        android_path.write_bytes(b"android proof")
        ios_target_path.write_text("iOS visible target text: Fitness First", encoding="utf-8")
        android_target_path.write_text(
            '<node text="Fitness First" resource-id="marketplace-pdp-title" />',
            encoding="utf-8",
        )
        ios_route_path.write_text(
            "LOG  [APP-PERF-METRIC] ui.load | screen.load /MarketplacePdp | 180ms | ok",
            encoding="utf-8",
        )
        android_route_path.write_text(
            "LOG  [APP-PERF-METRIC] ui.load | screen.load /MarketplacePdp | 190ms | ok",
            encoding="utf-8",
        )
        manifest_payload["proof_artifact_metadata"] = _test_proof_artifact_metadata(
            tuple(proof_artifact_paths)
        )
        manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
        manifest_artifact = str(manifest_path)
        ios_artifact = str(ios_path)
        android_artifact = str(android_path)
        ios_target_artifact = str(ios_target_path)
        android_target_artifact = str(android_target_path)
        ios_route_artifact = str(ios_route_path)
        android_route_artifact = str(android_route_path)
    return {
        "passed": True,
        "summary": "Proof captured.",
        "platforms": ["ios", "android"],
        "proof_target": {
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Fitness First",
        },
        "setup_commands": ["npm run monica:seed-auth"],
        "commands": ["npm run monica:proof"],
        "required_env_keys": ["MONICA_TEST_LOGIN_TOKEN"],
        "artifacts": [
            manifest_artifact,
            ios_artifact,
            android_artifact,
            ios_target_artifact,
            android_target_artifact,
            ios_route_artifact,
            android_route_artifact,
        ],
        "artifact_metadata": _test_proof_artifact_metadata(
            tuple(Path(path) for path in (
                ios_artifact,
                android_artifact,
                ios_target_artifact,
                android_target_artifact,
                ios_route_artifact,
                android_route_artifact,
            ))
        ),
        "shareable_artifacts": [
            {
                "platform": "ios",
                "path": ios_artifact,
                "url": "https://slack.example/files/ios-pdp-fixed.png",
                "upload_id": "F-ios-pdp-fixed.png",
                "upload_name": "ios-pdp-fixed.png",
            },
            {
                "platform": "android",
                "path": android_artifact,
                "url": "https://slack.example/files/android-pdp-fixed.mp4",
                "upload_id": "F-android-pdp-fixed.mp4",
                "upload_name": "android-pdp-fixed.mp4",
            },
        ],
    }


def _test_proof_artifact_metadata(paths: tuple[Path, ...]) -> list[dict[str, object]]:
    metadata: list[dict[str, object]] = []
    for path in paths:
        name = path.name.casefold()
        item: dict[str, object] = {
            "path": str(path),
            "platform": "ios" if "ios" in name else "android" if "android" in name else "",
        }
        if path.is_file():
            content = path.read_bytes()
            item["bytes"] = len(content)
            item["sha256"] = hashlib.sha256(content).hexdigest()
        metadata.append(item)
    return metadata


def _valid_approved_pr_worker_result(tmp_path: Any, *, run: Any | None = None) -> dict[str, Any]:
    run_id = str(getattr(run, "id", "") or "") if run is not None else ""
    return {
        "branch_name": "monica/MOB-123-pdp-copy",
        "worktree_path": str(tmp_path),
        "base_ref": "origin/dev",
        "base_commit": "abc1234",
        "summary": "Patched PDP copy.",
        "slack_permalink": "https://example.slack.com/thread",
        "changed": True,
        "proof_deep_link": "elixir-card://marketplace/offer/fitness-first",
        "proof_expected_text": "Fitness First",
        "proof": _valid_approved_pr_proof(
            Path(tmp_path) / "proof",
            run_id=run_id,
            worktree=str(tmp_path),
        ),
    }


def _update_proof_manifest(worker_result: dict[str, Any], **updates: Any) -> dict[str, Any]:
    manifest_path = Path(worker_result["proof"]["artifacts"][0])
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload.update(updates)
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def test_default_skills_create_real_linear_issue_when_not_dry_run(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Android offer card crashes when tapped",
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
        posted = skills.post_status(run, "Created Linear issue")

    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert posted is False
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


def test_default_skills_uploads_visual_proof_artifacts_to_slack_thread(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    proof_dir = tmp_path / "proof"
    proof_dir.mkdir()
    ios = proof_dir / "ios-pdp-fixed.png"
    android = proof_dir / "android-pdp-fixed.mp4"
    ios.write_bytes(b"ios")
    android.write_bytes(b"android")
    slack_client = CapturingSlackClient()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(),
        state=state,
        slack_client=slack_client,
    )

    result = skills.share_proof_artifacts(
        run,
        {
            "passed": True,
            "artifacts": [str(ios), str(android)],
        },
    )

    assert [upload["file_path"] for upload in slack_client.uploads] == [str(ios), str(android)]
    assert slack_client.uploads[0]["title"] == "Monica iOS proof: ios-pdp-fixed.png"
    assert slack_client.uploads[1]["title"] == "Monica Android proof: android-pdp-fixed.mp4"
    assert result["share_errors"] == []
    assert result["shareable_artifacts"] == [
        {
            "platform": "ios",
            "path": str(ios),
            "url": "https://slack.example/files/ios-pdp-fixed.png",
            "title": "Monica iOS proof: ios-pdp-fixed.png",
            "bytes": len(b"ios"),
            "sha256": hashlib.sha256(b"ios").hexdigest(),
            "upload_id": "F-ios-pdp-fixed.png",
            "upload_name": "ios-pdp-fixed.png",
        },
        {
            "platform": "android",
            "path": str(android),
            "url": "https://slack.example/files/android-pdp-fixed.mp4",
            "title": "Monica Android proof: android-pdp-fixed.mp4",
            "bytes": len(b"android"),
            "sha256": hashlib.sha256(b"android").hexdigest(),
            "upload_id": "F-android-pdp-fixed.mp4",
            "upload_name": "android-pdp-fixed.mp4",
        },
    ]


def test_default_skills_uploads_only_manifest_proof_visual_artifacts_to_slack_thread(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    proof_dir = tmp_path / "proof"
    proof_dir.mkdir()
    manifest = proof_dir / "monica-proof-manifest.json"
    setup_ios = proof_dir / "ios-setup-auth.png"
    ios = proof_dir / "ios-pdp-fixed.png"
    android = proof_dir / "android-pdp-fixed.mp4"
    setup_ios.write_bytes(b"setup ios")
    ios.write_bytes(b"ios")
    android.write_bytes(b"android")
    manifest.write_text(
        json.dumps({"proof_artifacts": [str(ios), str(android)]}),
        encoding="utf-8",
    )
    slack_client = CapturingSlackClient()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(),
        state=state,
        slack_client=slack_client,
    )

    result = skills.share_proof_artifacts(
        run,
        {
            "passed": True,
            "artifacts": [str(manifest), str(setup_ios), str(ios), str(android)],
        },
    )

    assert [upload["file_path"] for upload in slack_client.uploads] == [str(ios), str(android)]
    assert result["share_errors"] == []
    assert [item["path"] for item in result["shareable_artifacts"]] == [str(ios), str(android)]


def test_default_skills_does_not_upload_manifest_proof_artifacts_outside_manifest_dir(
    tmp_path,
):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    proof_dir = tmp_path / "proof"
    proof_dir.mkdir()
    outside_dir = tmp_path / "outside-proof"
    outside_dir.mkdir()
    manifest = proof_dir / "monica-proof-manifest.json"
    outside_ios = outside_dir / "ios-pdp-fixed.png"
    android = proof_dir / "android-pdp-fixed.mp4"
    outside_ios.write_bytes(b"ios")
    android.write_bytes(b"android")
    manifest.write_text(
        json.dumps({"proof_artifacts": [str(outside_ios), str(android)]}),
        encoding="utf-8",
    )
    slack_client = CapturingSlackClient()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(),
        state=state,
        slack_client=slack_client,
    )

    result = skills.share_proof_artifacts(
        run,
        {
            "passed": True,
            "artifacts": [str(manifest), str(outside_ios), str(android)],
        },
    )

    assert [upload["file_path"] for upload in slack_client.uploads] == [str(android)]
    assert [item["path"] for item in result["shareable_artifacts"]] == [str(android)]
    assert result["share_errors"] == [
        f"{outside_ios}: proof artifact is outside proof manifest directory"
    ]


def test_default_skills_does_not_fallback_to_local_visuals_when_manifest_has_no_proof_artifacts(
    tmp_path,
):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    proof_dir = tmp_path / "proof"
    proof_dir.mkdir()
    manifest = proof_dir / "monica-proof-manifest.json"
    ios = proof_dir / "ios-pdp-fixed.png"
    android = proof_dir / "android-pdp-fixed.mp4"
    ios.write_bytes(b"ios")
    android.write_bytes(b"android")
    manifest.write_text(json.dumps({"proof_artifacts": []}), encoding="utf-8")
    slack_client = CapturingSlackClient()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(),
        state=state,
        slack_client=slack_client,
    )

    result = skills.share_proof_artifacts(
        run,
        {
            "passed": True,
            "artifacts": [str(manifest), str(ios), str(android)],
        },
    )

    assert slack_client.uploads == []
    assert result["shareable_artifacts"] == []
    assert result["share_errors"] == [
        f"{manifest}: proof manifest does not list proof artifacts"
    ]


def test_default_skills_does_not_fallback_to_local_visuals_when_manifest_has_only_text_proof_artifacts(
    tmp_path,
):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    proof_dir = tmp_path / "proof"
    proof_dir.mkdir()
    manifest = proof_dir / "monica-proof-manifest.json"
    ios = proof_dir / "ios-pdp-fixed.png"
    android = proof_dir / "android-pdp-fixed.mp4"
    ios_text = proof_dir / "ios-target.log"
    ios.write_bytes(b"ios")
    android.write_bytes(b"android")
    ios_text.write_text("visible target text: Fitness First", encoding="utf-8")
    manifest.write_text(
        json.dumps({"proof_artifacts": [str(ios_text)]}),
        encoding="utf-8",
    )
    slack_client = CapturingSlackClient()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(),
        state=state,
        slack_client=slack_client,
    )

    result = skills.share_proof_artifacts(
        run,
        {
            "passed": True,
            "artifacts": [str(manifest), str(ios), str(android), str(ios_text)],
        },
    )

    assert slack_client.uploads == []
    assert result["shareable_artifacts"] == []
    assert result["share_errors"] == [
        f"{manifest}: proof manifest does not list visual proof artifacts"
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
            base_ref="origin/dev",
            base_commit="abc1234",
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
            base_ref="origin/main",
            base_commit="main123base",
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
        "base_ref": "origin/dev",
        "base_commit": "abc1234",
        "slack_permalink": "",
        "evidence": [],
    }
    saved = state.get_run(run.id)
    assert saved is not None
    assert saved.base_branch == "origin/dev"
    assert saved.base_commit == "abc1234"


def test_default_skills_approved_pr_brief_requires_proof_target_output(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP card copy is wrong",
    )
    run = state.update_run(run.id, linear_identifier="MOB-123")
    worker_calls: list[Any] = []
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            verification=VerificationConfig(commands=("npm test",)),
        ),
        state=state,
        repo_manager=FakeRepoManager([]),
        codex_worker=FakeCodexWorker(worker_calls),
    )

    skills.run_internal_codex_worker(run)

    brief = worker_calls[0][3]
    assert "## Required Proof Target" in brief
    assert "Monica proof deep link: <url>" in brief
    assert "Monica proof expected text: <text visible on the fixed screen>" in brief
    assert "required before Monica can capture iOS and Android proof" in brief
    instructions = brief.split("## Instructions", 1)[1]
    assert "Monica proof deep link: <url>" in instructions
    assert "Monica proof expected text: <text visible on the fixed screen>" in instructions


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
            "proof_expected_text": "Fitness First",
        },
        {"passed": True},
    )

    assert result["passed"] is True
    assert proof_runner.calls[0]["proof_target"] == {
        "deep_link": "elixir-card://marketplace/offer/fitness-first",
        "expected_text": "Fitness First",
    }


def test_default_skills_run_proof_passes_worker_proof_screen(tmp_path):
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
            "proof_expected_text": "Fitness First",
            "proof_screen": "/MarketplacePdp",
        },
        {"passed": True},
    )

    assert result["passed"] is True
    assert proof_runner.calls[0]["proof_target"] == {
        "deep_link": "elixir-card://marketplace/offer/fitness-first",
        "expected_text": "Fitness First",
        "screen": "/MarketplacePdp",
    }


def test_default_skills_run_proof_uses_configured_deep_link_when_worker_omits_it(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    proof_runner = CapturingProofRunner([])
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            proof=ProofConfig(
                commands=("capture-proof",),
                deep_link="elixir-card://marketplace/offer/fitness-first",
            )
        ),
        state=state,
        proof_runner=proof_runner,
    )

    result = skills.run_proof(
        run,
        {
            "worktree_path": str(tmp_path),
            "proof_expected_text": "Fitness First",
        },
        {"passed": True},
    )

    assert result["passed"] is True
    assert proof_runner.calls[0]["proof_target"] == {
        "deep_link": "elixir-card://marketplace/offer/fitness-first",
        "expected_text": "Fitness First",
    }


def test_default_skills_approved_pr_run_proof_does_not_use_configured_deep_link_fallback(
    tmp_path,
):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    proof_runner = CapturingProofRunner([])
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            proof=ProofConfig(
                commands=("capture-proof",),
                deep_link="elixir-card://marketplace/offer/fitness-first",
            ),
        ),
        state=state,
        proof_runner=proof_runner,
    )

    result = skills.run_proof(
        run,
        {
            "worktree_path": str(tmp_path),
            "proof_expected_text": "Fitness First",
        },
        {"passed": True},
    )

    assert result["passed"] is True
    assert proof_runner.calls[0]["proof_target"] == {
        "expected_text": "Fitness First",
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
            "base_ref": "origin/main",
            "base_commit": "abc1234",
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
                "setup_commands": ["npm run monica:seed-auth"],
                "commands": ["npm run monica:proof"],
                "required_env_keys": [
                    "MONICA_TEST_LOGIN_TOKEN",
                    "MONICA_TEST_LOGIN_OTP",
                ],
                "proof_target": {
                    "deep_link": "elixir-card://marketplace/offer/fitness-first",
                    "expected_text": "Fitness First",
                },
                "artifacts": [
                    "/tmp/monica-proof/monica-proof-manifest.json",
                    "/tmp/monica-proof/ios-pdp-fixed.png",
                    "/tmp/monica-proof/android-pdp-fixed.mp4",
                ],
                "shareable_artifacts": [
                    {
                        "platform": "ios",
                        "path": "/tmp/monica-proof/ios-pdp-fixed.png",
                        "url": "https://slack.example/files/ios-pdp-fixed.png",
                    },
                    {
                        "platform": "android",
                        "path": "/tmp/monica-proof/android-pdp-fixed.mp4",
                        "url": "https://slack.example/files/android-pdp-fixed.mp4",
                    },
                ],
            },
        },
        verification={"summary": "Verification passed.", "output": "npm test\nok"},
    )

    assert "- Slack: https://example.slack.com/thread" in body
    assert "## Evidence" in body
    assert "- crash.png (image/png): https://example.slack.com/file/F1" in body
    assert "## Proof" in body
    assert "Base: origin/main @ abc1234" in body
    assert "Proof captured." in body
    assert "Platforms: ios, android" in body
    assert "Required env keys: MONICA_TEST_LOGIN_TOKEN, MONICA_TEST_LOGIN_OTP" in body
    assert "Setup commands:" in body
    assert "- npm run monica:seed-auth" in body
    assert "Proof commands:" in body
    assert "- npm run monica:proof" in body
    assert "Target: elixir-card://marketplace/offer/fitness-first" in body
    assert "Expected text: Fitness First" in body
    assert "- iOS: https://slack.example/files/ios-pdp-fixed.png" in body
    assert "- Android: https://slack.example/files/android-pdp-fixed.mp4" in body
    assert "Local artifacts (debug): /tmp/monica-proof/monica-proof-manifest.json" in body
    assert "/tmp/monica-proof/ios-pdp-fixed.png" in body
    assert "/tmp/monica-proof/android-pdp-fixed.mp4" in body


def test_default_skills_pr_body_includes_proof_screen_when_worker_supplies_it(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
    )

    body = DefaultMonicaSkills._pr_body(
        run=run,
        worker_result={
            "summary": "Patched PDP copy.",
            "base_ref": "origin/dev",
            "base_commit": "abc1234",
            "proof": {
                "summary": "Proof captured.",
                "proof_target": {
                    "deep_link": "elixir-card://marketplace/offer/fitness-first",
                    "expected_text": "Fitness First",
                    "screen": "/MarketplacePdp",
                },
            },
        },
        verification={"summary": "Verification passed.", "output": "npm test\nok"},
    )

    assert "## Proof" in body
    assert "Target: elixir-card://marketplace/offer/fitness-first" in body
    assert "Expected text: Fitness First" in body
    assert "Screen: /MarketplacePdp" in body


def test_default_skills_open_draft_pr_records_base_and_proof_context_on_linear(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_issue_id="issue-id",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    linear_client = FakeLinearClient([])
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                setup_commands=("npm run monica:seed-auth",),
                commands=("npm run monica:proof",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
            ),
        ),
        state=state,
        linear_client=linear_client,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result["proof"]["required_env_keys"] = ["MONICA_TEST_LOGIN_TOKEN"]
    _update_proof_manifest(worker_result, required_env_keys=["MONICA_TEST_LOGIN_TOKEN"])

    result = skills.open_draft_pr(
        run,
        worker_result,
        {"passed": True, "summary": "Verification passed.", "output": "npm test\nok"},
    )

    assert result == {"url": "https://github.com/acme/mobile/pull/123"}
    assert len(linear_client.comment_calls) == 1
    comment = linear_client.comment_calls[0]
    assert comment.issue_id == "issue-id"
    assert "Base: origin/dev @ abc1234" in comment.body
    assert "Branch: monica/MOB-123-pdp-copy" in comment.body
    assert "Verification: Verification passed." in comment.body
    assert "npm test" in comment.body
    assert "Proof target: elixir-card://marketplace/offer/fitness-first" in comment.body
    assert "Expected text: Fitness First" in comment.body
    assert "Required env keys: MONICA_TEST_LOGIN_TOKEN" in comment.body
    assert "iOS proof: https://slack.example/files/ios-pdp-fixed.png" in comment.body
    assert "Android proof: https://slack.example/files/android-pdp-fixed.mp4" in comment.body
    assert "Slack upload: F-ios-pdp-fixed.png, ios-pdp-fixed.png" in comment.body
    assert "Slack upload: F-android-pdp-fixed.mp4, android-pdp-fixed.mp4" in comment.body
    assert publisher.calls


def test_default_skills_open_draft_pr_includes_slack_upload_metadata_in_body(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                setup_commands=("npm run monica:seed-auth",),
                commands=("npm run monica:proof",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
            ),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result["proof"]["required_env_keys"] = ["MONICA_TEST_LOGIN_TOKEN"]
    _update_proof_manifest(worker_result, required_env_keys=["MONICA_TEST_LOGIN_TOKEN"])

    skills.open_draft_pr(
        run,
        worker_result,
        {
            "passed": True,
            "summary": "Verification passed.",
            "commands": ("npm test",),
            "output": "All checks passed.",
        },
    )

    body = publisher.calls[0]["body"]
    assert "- iOS: https://slack.example/files/ios-pdp-fixed.png" in body
    assert "- Android: https://slack.example/files/android-pdp-fixed.mp4" in body
    assert "Slack upload: F-ios-pdp-fixed.png, ios-pdp-fixed.png" in body
    assert "Slack upload: F-android-pdp-fixed.mp4, android-pdp-fixed.mp4" in body
    assert "Local artifacts (debug):" in body


def test_default_skills_open_draft_pr_normalizes_configured_required_env_keys(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_issue_id="issue-id",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                setup_commands=("npm run monica:seed-auth",),
                commands=("npm run monica:proof",),
                required_env_keys=(
                    " MONICA_TEST_LOGIN_TOKEN ",
                    "MONICA_TEST_LOGIN_TOKEN",
                    " ",
                ),
            ),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result["proof"]["required_env_keys"] = ["MONICA_TEST_LOGIN_TOKEN"]
    _update_proof_manifest(worker_result, required_env_keys=["MONICA_TEST_LOGIN_TOKEN"])

    result = skills.open_draft_pr(
        run,
        worker_result,
        {"passed": True, "summary": "Verification passed.", "output": "npm test\nok"},
    )

    assert result == {"url": "https://github.com/acme/mobile/pull/123"}
    assert publisher.calls


def test_default_skills_open_draft_pr_refuses_proof_without_worker_requested_screen(
    tmp_path,
):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_issue_id="issue-id",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                setup_commands=("npm run monica:seed-auth",),
                commands=("npm run monica:proof",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
            ),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result["proof_screen"] = "/MarketplacePdp"
    worker_result["proof"]["required_env_keys"] = ["MONICA_TEST_LOGIN_TOKEN"]
    _update_proof_manifest(worker_result, required_env_keys=["MONICA_TEST_LOGIN_TOKEN"])

    with pytest.raises(DraftPrPublisherError, match="worker-requested screen"):
        skills.open_draft_pr(
            run,
            worker_result,
            {"passed": True, "summary": "Verification passed.", "output": "npm test\nok"},
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_manifest_that_drops_proof_screen(
    tmp_path,
):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_issue_id="issue-id",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                setup_commands=("npm run monica:seed-auth",),
                commands=("npm run monica:proof",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
            ),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result["proof_screen"] = "/MarketplacePdp"
    worker_result["proof"]["required_env_keys"] = ["MONICA_TEST_LOGIN_TOKEN"]
    worker_result["proof"]["proof_target"]["screen"] = "/MarketplacePdp"
    _update_proof_manifest(
        worker_result,
        required_env_keys=["MONICA_TEST_LOGIN_TOKEN"],
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Fitness First",
        },
    )

    with pytest.raises(DraftPrPublisherError, match="proof manifest target"):
        skills.open_draft_pr(
            run,
            worker_result,
            {"passed": True, "summary": "Verification passed.", "output": "npm test\nok"},
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_accepts_origin_prefixed_default_branch(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_issue_id="issue-id",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="origin/dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                setup_commands=("npm run monica:seed-auth",),
                commands=("npm run monica:proof"),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
            ),
        ),
        state=state,
        linear_client=FakeLinearClient([]),
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result["proof"]["required_env_keys"] = ["MONICA_TEST_LOGIN_TOKEN"]
    _update_proof_manifest(worker_result, required_env_keys=["MONICA_TEST_LOGIN_TOKEN"])

    result = skills.open_draft_pr(
        run,
        worker_result,
        {"passed": True, "summary": "Verification passed.", "output": "npm test\nok"},
    )

    assert result == {"url": "https://github.com/acme/mobile/pull/123"}
    assert publisher.calls


def test_default_skills_open_draft_pr_refuses_missing_proof_before_publishing(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(rollout_mode="approved_pr"),
        state=state,
        pr_publisher=publisher,
    )

    with pytest.raises(DraftPrPublisherError, match="proof evidence is required"):
        skills.open_draft_pr(
            run,
            {
                "branch_name": "monica/MOB-123-pdp-copy",
                "worktree_path": str(tmp_path),
                "summary": "Patched PDP copy.",
                "slack_permalink": "https://example.slack.com/thread",
            },
            {"summary": "Verification passed.", "output": "npm test\nok"},
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_missing_linear_issue_before_publishing(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(rollout_mode="approved_pr"),
        state=state,
        pr_publisher=publisher,
    )

    with pytest.raises(DraftPrPublisherError, match="Linear issue is required"):
        skills.open_draft_pr(
            run,
            {
                "branch_name": "monica/MOB-123-pdp-copy",
                "worktree_path": str(tmp_path),
                "summary": "Patched PDP copy.",
                "slack_permalink": "https://example.slack.com/thread",
                "proof": {
                    "passed": True,
                    "summary": "Proof captured.",
                    "platforms": ["ios", "android"],
                    "proof_target": {
                        "deep_link": "elixir-card://marketplace/offer/fitness-first",
                        "expected_text": "Fitness First",
                    },
                    "setup_commands": ["npm run monica:seed-auth"],
                    "commands": ["npm run monica:proof"],
                    "artifacts": [
                        "/tmp/monica-proof/ios-pdp-fixed.png",
                        "/tmp/monica-proof/android-pdp-fixed.mp4",
                    ],
                    "shareable_artifacts": [
                        {
                            "platform": "ios",
                            "path": "/tmp/monica-proof/ios-pdp-fixed.png",
                            "url": "https://slack.example/files/ios-pdp-fixed.png",
                        },
                        {
                            "platform": "android",
                            "path": "/tmp/monica-proof/android-pdp-fixed.mp4",
                            "url": "https://slack.example/files/android-pdp-fixed.mp4",
                        },
                    ],
                },
            },
            {"summary": "Verification passed.", "output": "npm test\nok"},
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_linear_identifier_without_url_before_publishing(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                setup_commands=("npm run monica:seed-auth",),
                commands=("npm run monica:proof",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
            ),
        ),
        state=state,
        pr_publisher=publisher,
    )

    with pytest.raises(DraftPrPublisherError, match="Linear issue URL is required"):
        skills.open_draft_pr(
            run,
            _valid_approved_pr_worker_result(tmp_path, run=run),
            {
                "passed": True,
                "summary": "Verification passed.",
                "output": "npm test\nok",
                "commands": ["npm test"],
            },
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_non_linear_issue_url_before_publishing(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_issue_id="issue-id",
        linear_url="https://linear.example/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                setup_commands=("npm run monica:seed-auth",),
                commands=("npm run monica:proof"),
            ),
        ),
        state=state,
        pr_publisher=publisher,
    )

    with pytest.raises(DraftPrPublisherError, match="Linear issue URL must be a Linear issue link"):
        skills.open_draft_pr(
            run,
            _valid_approved_pr_worker_result(tmp_path, run=run),
            {
                "passed": True,
                "summary": "Verification passed.",
                "output": "npm test\nok",
                "commands": ["npm test"],
            },
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_out_of_scope_request_before_publishing(tmp_path):
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
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                setup_commands=("npm run monica:seed-auth",),
                commands=("npm run monica:proof",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
            ),
        ),
        state=state,
        pr_publisher=publisher,
    )

    with pytest.raises(DraftPrPublisherError, match="marketplace copy/design"):
        skills.open_draft_pr(
            run,
            _valid_approved_pr_worker_result(tmp_path, run=run),
            {
                "passed": True,
                "summary": "Verification passed.",
                "output": "npm test\nok",
                "commands": ["npm test"],
            },
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_marketplace_crash_even_with_copy_terms(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Android marketplace PDP crashes after loading promo copy, please fix it",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                setup_commands=("npm run monica:seed-auth",),
                commands=("npm run monica:proof",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
            ),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result["proof"]["required_env_keys"] = ["MONICA_TEST_LOGIN_TOKEN"]
    _update_proof_manifest(worker_result, required_env_keys=["MONICA_TEST_LOGIN_TOKEN"])

    with pytest.raises(DraftPrPublisherError, match="marketplace copy/design"):
        skills.open_draft_pr(
            run,
            worker_result,
            {
                "passed": True,
                "summary": "Verification passed.",
                "output": "npm test\nok",
                "commands": ["npm test"],
            },
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_invalid_proof_deep_link_before_publishing(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                setup_commands=("npm run monica:seed-auth",),
                commands=("npm run monica:proof",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
            ),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result["proof"]["proof_target"]["deep_link"] = "marketplace PDP"

    with pytest.raises(DraftPrPublisherError, match="proof target deep link"):
        skills.open_draft_pr(
            run,
            worker_result,
            {
                "passed": True,
                "summary": "Verification passed.",
                "output": "npm test\nok",
                "commands": ["npm test"],
            },
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_generic_home_proof_target_before_publishing(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                setup_commands=("npm run monica:seed-auth",),
                commands=("npm run monica:proof",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
            ),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result["proof_deep_link"] = "elixir-card://home"
    worker_result["proof"]["proof_target"]["deep_link"] = "elixir-card://home"

    with pytest.raises(DraftPrPublisherError, match="generic proof target"):
        skills.open_draft_pr(
            run,
            worker_result,
            {
                "passed": True,
                "summary": "Verification passed.",
                "output": "npm test\nok",
                "commands": ["npm test"],
            },
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_generic_marketplace_target_before_publishing(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                setup_commands=("npm run monica:seed-auth",),
                commands=("npm run monica:proof",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
            ),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result["proof_deep_link"] = "elixir-card://marketplace"
    worker_result["proof"]["proof_target"]["deep_link"] = "elixir-card://marketplace"
    manifest_path = Path(worker_result["proof"]["artifacts"][0])
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_payload["proof_target"]["deep_link"] = "elixir-card://marketplace"
    manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")

    with pytest.raises(DraftPrPublisherError, match="generic proof target"):
        skills.open_draft_pr(
            run,
            worker_result,
            {
                "passed": True,
                "summary": "Verification passed.",
                "output": "npm test\nok",
                "commands": ["npm test"],
            },
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_generic_offer_route_target_before_publishing(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                setup_commands=("npm run monica:seed-auth",),
                commands=("npm run monica:proof",),
            ),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result["proof_deep_link"] = "elixir-card://marketplace/offer"
    worker_result["proof"]["proof_target"]["deep_link"] = "elixir-card://marketplace/offer"
    manifest_path = Path(worker_result["proof"]["artifacts"][0])
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_payload["proof_target"]["deep_link"] = "elixir-card://marketplace/offer"
    manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")

    with pytest.raises(DraftPrPublisherError, match="generic proof target"):
        skills.open_draft_pr(
            run,
            worker_result,
            {
                "passed": True,
                "summary": "Verification passed.",
                "output": "npm test\nok",
                "commands": ["npm test"],
            },
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_expo_dev_client_proof_target_before_publishing(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    expo_target = "elixir-card://expo-development-client/?url=http%3A%2F%2F127.0.0.1%3A8081"
    worker_result["proof_deep_link"] = expo_target
    worker_result["proof"]["proof_target"]["deep_link"] = expo_target

    with pytest.raises(DraftPrPublisherError, match="Expo Dev Client proof target"):
        skills.open_draft_pr(
            run,
            worker_result,
            {"passed": True, "summary": "Verification passed.", "output": "npm test\nok"},
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_local_http_proof_target_before_publishing(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    local_target = "http://127.0.0.1:8081/marketplace/offer/fitness-first"
    worker_result["proof_deep_link"] = local_target
    worker_result["proof"]["proof_target"]["deep_link"] = local_target

    with pytest.raises(DraftPrPublisherError, match="local proof target"):
        skills.open_draft_pr(
            run,
            worker_result,
            {"passed": True, "summary": "Verification passed.", "output": "npm test\nok"},
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_requires_worker_proof_deep_link_before_publishing(tmp_path):
    fallback_deep_link = "elixir-card://marketplace/fallback"
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            proof=ProofConfig(deep_link=fallback_deep_link),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result.pop("proof_deep_link")
    worker_result["proof"]["proof_target"]["deep_link"] = fallback_deep_link

    with pytest.raises(DraftPrPublisherError, match="worker proof target deep link"):
        skills.open_draft_pr(
            run,
            worker_result,
            {"passed": True, "summary": "Verification passed.", "output": "npm test\nok"},
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_requires_worker_expected_text_before_publishing(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result.pop("proof_expected_text")

    with pytest.raises(DraftPrPublisherError, match="worker proof target expected text"):
        skills.open_draft_pr(
            run,
            worker_result,
            {"passed": True, "summary": "Verification passed.", "output": "npm test\nok"},
        )

    assert publisher.calls == []


@pytest.mark.parametrize(
    "proof_expected_text",
    ("unavailable", "text visible on the fixed screen", "<text visible on the fixed screen>"),
)
def test_default_skills_open_draft_pr_rejects_placeholder_worker_expected_text_before_publishing(
    tmp_path,
    proof_expected_text,
):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result["proof_expected_text"] = proof_expected_text

    with pytest.raises(DraftPrPublisherError, match="worker proof target expected text"):
        skills.open_draft_pr(
            run,
            worker_result,
            {"passed": True, "summary": "Verification passed.", "output": "npm test\nok"},
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_rejects_generic_worker_expected_text_before_publishing(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result["proof_expected_text"] = "Marketplace"

    with pytest.raises(DraftPrPublisherError, match="worker proof target expected text"):
        skills.open_draft_pr(
            run,
            worker_result,
            {"passed": True, "summary": "Verification passed.", "output": "npm test\nok"},
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_rejects_route_container_expected_text_before_publishing(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result["proof_expected_text"] = "Offer"

    with pytest.raises(DraftPrPublisherError, match="worker proof target expected text"):
        skills.open_draft_pr(
            run,
            worker_result,
            {"passed": True, "summary": "Verification passed.", "output": "npm test\nok"},
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_mismatched_proof_target_before_publishing(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result["proof"]["proof_target"]["deep_link"] = "elixir-card://marketplace/offer/other-gym"

    with pytest.raises(DraftPrPublisherError, match="proof target does not match"):
        skills.open_draft_pr(
            run,
            worker_result,
            {"passed": True, "summary": "Verification passed.", "output": "npm test\nok"},
        )

    assert publisher.calls == []


@pytest.mark.parametrize(
    ("missing_key", "message"),
    (
        ("setup_commands", "proof setup commands are required"),
        ("commands", "proof commands are required"),
    ),
)
def test_default_skills_open_draft_pr_refuses_missing_proof_command_evidence_before_publishing(
    tmp_path, missing_key, message
):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result["proof"]["setup_commands"] = ["npm run monica:seed-auth"]
    worker_result["proof"]["commands"] = ["npm run monica:proof"]
    worker_result["proof"].pop(missing_key)

    with pytest.raises(DraftPrPublisherError, match=message):
        skills.open_draft_pr(
            run,
            worker_result,
            {"passed": True, "summary": "Verification passed.", "output": "npm test\nok"},
        )

    assert publisher.calls == []


@pytest.mark.parametrize(
    ("proof_config", "message"),
    (
        (
            ProofConfig(commands=("npm run monica:proof",)),
            "configured proof setup commands are required",
        ),
        (
            ProofConfig(setup_commands=("npm run monica:seed-auth",)),
            "configured proof commands are required",
        ),
    ),
)
def test_default_skills_open_draft_pr_requires_configured_proof_commands_before_publishing(
    tmp_path, proof_config, message
):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=proof_config,
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)

    with pytest.raises(DraftPrPublisherError, match=message):
        skills.open_draft_pr(
            run,
            worker_result,
            {
                "passed": True,
                "summary": "Verification passed.",
                "output": "npm test\nok",
                "commands": ["npm test"],
            },
        )

    assert publisher.calls == []


@pytest.mark.parametrize(
    ("proof_config", "proof_updates", "message"),
    (
        (
            ProofConfig(
                setup_commands=("<auth/session seed command>",),
                commands=("npm run monica:proof",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
            ),
            {"setup_commands": ["<auth/session seed command>"]},
            "configured proof setup commands cannot be placeholder",
        ),
        (
            ProofConfig(
                setup_commands=("npm run monica:seed-auth",),
                commands=("<simulator proof command>",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
            ),
            {"commands": ["<simulator proof command>"]},
            "configured proof commands cannot be placeholder",
        ),
    ),
)
def test_default_skills_open_draft_pr_rejects_placeholder_configured_proof_commands(
    tmp_path, proof_config, proof_updates, message
):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=proof_config,
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result["proof"].update(proof_updates)
    worker_result["proof"]["required_env_keys"] = ["MONICA_TEST_LOGIN_TOKEN"]
    _update_proof_manifest(
        worker_result,
        required_env_keys=["MONICA_TEST_LOGIN_TOKEN"],
        **proof_updates,
    )

    with pytest.raises(DraftPrPublisherError, match=message):
        skills.open_draft_pr(
            run,
            worker_result,
            {
                "passed": True,
                "summary": "Verification passed.",
                "output": "npm test\nok",
                "commands": ["npm test"],
            },
        )

    assert publisher.calls == []


@pytest.mark.parametrize(
    ("proof_config", "proof_updates", "message"),
    (
        (
            ProofConfig(
                setup_commands=("true", "exit 0"),
                commands=("npm run monica:proof",),
            ),
            {"setup_commands": ["true", "exit 0"]},
            "configured proof setup commands cannot be no-op",
        ),
        (
            ProofConfig(
                setup_commands=("npm run monica:seed-auth",),
                commands=("true", "exit 0"),
            ),
            {"commands": ["true", "exit 0"]},
            "configured proof commands cannot be no-op",
        ),
    ),
)
def test_default_skills_open_draft_pr_rejects_noop_configured_proof_commands(
    tmp_path, proof_config, proof_updates, message
):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=proof_config,
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result["proof"].update(proof_updates)
    _update_proof_manifest(worker_result, **proof_updates)

    with pytest.raises(DraftPrPublisherError, match=message):
        skills.open_draft_pr(
            run,
            worker_result,
            {
                "passed": True,
                "summary": "Verification passed.",
                "output": "npm test\nok",
                "commands": ["npm test"],
            },
        )

    assert publisher.calls == []


@pytest.mark.parametrize(
    ("proof_config", "proof_updates", "message"),
    (
        (
            ProofConfig(
                setup_commands=("MONICA_TEST_LOGIN_TOKEN=secret npm run monica:seed-auth",),
                commands=("npm run monica:proof",),
            ),
            {"setup_commands": ["MONICA_TEST_LOGIN_TOKEN=secret npm run monica:seed-auth"]},
            "proof setup commands must not inline secret env",
        ),
        (
            ProofConfig(
                setup_commands=("npm run monica:seed-auth",),
                commands=("MONICA_TEST_LOGIN_OTP=123456 npm run monica:proof",),
            ),
            {"commands": ["MONICA_TEST_LOGIN_OTP=123456 npm run monica:proof"]},
            "proof commands must not inline secret env",
        ),
    ),
)
def test_default_skills_open_draft_pr_rejects_inline_secret_proof_commands(
    tmp_path, proof_config, proof_updates, message
):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=proof_config,
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result["proof"].update(proof_updates)
    _update_proof_manifest(worker_result, **proof_updates)

    with pytest.raises(DraftPrPublisherError, match=message):
        skills.open_draft_pr(
            run,
            worker_result,
            {
                "passed": True,
                "summary": "Verification passed.",
                "output": "npm test\nok",
                "commands": ["npm test"],
            },
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_missing_approval_before_publishing(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev"),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)

    with pytest.raises(DraftPrPublisherError, match="explicit approver approval is required"):
        skills.open_draft_pr(
            run,
            worker_result,
            {"summary": "Verification passed.", "output": "npm test\nok"},
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_unconfigured_approver_before_publishing(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_NOT_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)

    with pytest.raises(DraftPrPublisherError, match="configured approver approval is required"):
        skills.open_draft_pr(
            run,
            worker_result,
            {"summary": "Verification passed.", "output": "npm test\nok"},
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_no_code_changes_before_publishing(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result["changed"] = False

    with pytest.raises(DraftPrPublisherError, match="code changes are required"):
        skills.open_draft_pr(
            run,
            worker_result,
            {"summary": "Verification passed.", "output": "npm test\nok"},
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_failed_verification_before_publishing(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)

    with pytest.raises(DraftPrPublisherError, match="verification must pass"):
        skills.open_draft_pr(
            run,
            worker_result,
            {"passed": False, "summary": "Verification failed.", "output": "npm test\nfail"},
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_verification_without_command_evidence(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                setup_commands=("npm run monica:seed-auth",),
                commands=("npm run monica:proof",),
            ),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)

    with pytest.raises(DraftPrPublisherError, match="verification command evidence"):
        skills.open_draft_pr(
            run,
            worker_result,
            {"passed": True, "summary": "Verification passed.", "output": "All checks passed."},
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_includes_verification_commands_in_body(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                setup_commands=("npm run monica:seed-auth",),
                commands=("npm run monica:proof",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
            ),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result["proof"]["required_env_keys"] = ["MONICA_TEST_LOGIN_TOKEN"]
    _update_proof_manifest(worker_result, required_env_keys=["MONICA_TEST_LOGIN_TOKEN"])

    skills.open_draft_pr(
        run,
        worker_result,
        {
            "passed": True,
            "summary": "Verification passed.",
            "commands": ("npm test",),
            "output": "All checks passed.",
        },
    )

    assert len(publisher.calls) == 1
    body = publisher.calls[0]["body"]
    assert "## Verification" in body
    assert "- npm test" in body
    assert "All checks passed." in body


def test_default_skills_open_draft_pr_fills_configured_verification_commands_in_body(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                setup_commands=("npm run monica:seed-auth",),
                commands=("npm run monica:proof",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
            ),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result["proof"]["required_env_keys"] = ["MONICA_TEST_LOGIN_TOKEN"]
    _update_proof_manifest(worker_result, required_env_keys=["MONICA_TEST_LOGIN_TOKEN"])

    skills.open_draft_pr(
        run,
        worker_result,
        {
            "passed": True,
            "summary": "Verification passed.",
            "output": "MONICA_ENV=1 npm test\nAll checks passed.",
        },
    )

    assert len(publisher.calls) == 1
    body = publisher.calls[0]["body"]
    assert "## Verification" in body
    assert "- npm test" in body
    assert "MONICA_ENV=1 npm test" in body


def test_default_skills_open_draft_pr_includes_proof_artifact_digests_in_body(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                setup_commands=("npm run monica:seed-auth",),
                commands=("npm run monica:proof",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
            ),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)

    skills.open_draft_pr(
        run,
        worker_result,
        {
            "passed": True,
            "summary": "Verification passed.",
            "commands": ("npm test",),
            "output": "All checks passed.",
        },
    )

    body = publisher.calls[0]["body"]
    assert "Proof artifact digests:" in body
    assert (
        "iOS: ios-pdp-fixed.png "
        f"bytes={len(b'ios proof')} sha256={hashlib.sha256(b'ios proof').hexdigest()}"
    ) in body
    assert (
        "Android: android-pdp-fixed.mp4 "
        f"bytes={len(b'android proof')} sha256={hashlib.sha256(b'android proof').hexdigest()}"
    ) in body


def test_default_skills_open_draft_pr_refuses_non_pull_request_url_from_publisher(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = InvalidUrlPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                setup_commands=("npm run monica:seed-auth",),
                commands=("npm run monica:proof",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
            ),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result["proof"]["required_env_keys"] = ["MONICA_TEST_LOGIN_TOKEN"]
    _update_proof_manifest(worker_result, required_env_keys=["MONICA_TEST_LOGIN_TOKEN"])

    with pytest.raises(DraftPrPublisherError, match="valid draft PR URL"):
        skills.open_draft_pr(
            run,
            worker_result,
            {"passed": True, "summary": "Verification passed.", "output": "npm test\nok"},
        )

    assert publisher.calls


@pytest.mark.parametrize(
    ("missing_key", "message"),
    (
        ("branch_name", "branch is required"),
        ("worktree_path", "worktree is required"),
        ("base_ref", "base commit metadata is required"),
        ("base_commit", "base commit metadata is required"),
    ),
)
def test_default_skills_open_draft_pr_refuses_missing_publish_metadata_before_publishing(
    tmp_path, missing_key, message
):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result.pop(missing_key)

    with pytest.raises(DraftPrPublisherError, match=message):
        skills.open_draft_pr(
            run,
            worker_result,
            {"passed": True, "summary": "Verification passed.", "output": "npm test\nok"},
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_non_sha_base_commit_before_publishing(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                setup_commands=("npm run monica:seed-auth",),
                commands=("npm run monica:proof",),
            ),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result["base_commit"] = "abc123base"

    with pytest.raises(DraftPrPublisherError, match="base commit metadata"):
        skills.open_draft_pr(
            run,
            worker_result,
            {"passed": True, "summary": "Verification passed.", "output": "npm test\nok"},
        )

    assert publisher.calls == []


@pytest.mark.parametrize(
    ("branch_name", "message"),
    (
        ("feature/MOB-123-pdp-copy", "Monica branch prefix"),
        ("monica/MOB-999-pdp-copy", "Linear issue identifier"),
    ),
)
def test_default_skills_open_draft_pr_refuses_unexpected_branch_before_publishing(
    tmp_path, branch_name, message
):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result["branch_name"] = branch_name

    with pytest.raises(DraftPrPublisherError, match=message):
        skills.open_draft_pr(
            run,
            worker_result,
            {"passed": True, "summary": "Verification passed.", "output": "npm test\nok"},
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_wrong_base_ref_before_publishing(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                setup_commands=("npm run monica:seed-auth",),
                commands=("npm run monica:proof",),
            ),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result["base_ref"] = "origin/main"

    with pytest.raises(DraftPrPublisherError, match="configured default branch"):
        skills.open_draft_pr(
            run,
            worker_result,
            {"passed": True, "summary": "Verification passed.", "output": "npm test\nok"},
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_local_only_proof_before_publishing(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev"),
        ),
        state=state,
        pr_publisher=publisher,
    )
    proof = _valid_approved_pr_proof(
        tmp_path / "proof-local-only",
        run_id=run.id,
        worktree=str(tmp_path),
    )
    proof.pop("shareable_artifacts")
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result["proof"] = proof

    with pytest.raises(DraftPrPublisherError, match="shareable proof links are required"):
        skills.open_draft_pr(
            run,
            worker_result,
            {"summary": "Verification passed.", "output": "npm test\nok"},
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_shareable_links_without_slack_upload_metadata(
    tmp_path,
):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                setup_commands=("npm run monica:seed-auth",),
                commands=("npm run monica:proof",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
            ),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result["proof"]["required_env_keys"] = ["MONICA_TEST_LOGIN_TOKEN"]
    for item in worker_result["proof"]["shareable_artifacts"]:
        item.pop("upload_id", None)
        item.pop("upload_name", None)
    _update_proof_manifest(worker_result, required_env_keys=["MONICA_TEST_LOGIN_TOKEN"])

    with pytest.raises(DraftPrPublisherError, match="Slack upload metadata"):
        skills.open_draft_pr(
            run,
            worker_result,
            {
                "passed": True,
                "summary": "Verification passed.",
                "commands": ("npm test",),
                "output": "All checks passed.",
            },
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_missing_local_visual_artifact_file_before_publishing(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    Path(worker_result["proof"]["artifacts"][1]).unlink()

    with pytest.raises(DraftPrPublisherError, match="proof artifact files are required"):
        skills.open_draft_pr(
            run,
            worker_result,
            {"passed": True, "summary": "Verification passed.", "output": "npm test\nok"},
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_empty_local_visual_artifact_file_before_publishing(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                setup_commands=("npm run monica:seed-auth",),
                commands=("npm run monica:proof",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
            ),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result["proof"]["required_env_keys"] = ["MONICA_TEST_LOGIN_TOKEN"]
    _update_proof_manifest(worker_result, required_env_keys=["MONICA_TEST_LOGIN_TOKEN"])
    Path(worker_result["proof"]["artifacts"][1]).write_bytes(b"")

    with pytest.raises(DraftPrPublisherError, match="empty proof artifact files"):
        skills.open_draft_pr(
            run,
            worker_result,
            {
                "passed": True,
                "summary": "Verification passed.",
                "commands": ["npm test"],
                "output": "npm test\nok",
            },
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_missing_proof_manifest_before_publishing(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                setup_commands=("npm run monica:seed-auth",),
                commands=("npm run monica:proof",),
            ),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result["proof"]["artifacts"] = [
        artifact
        for artifact in worker_result["proof"]["artifacts"]
        if Path(str(artifact)).name != "monica-proof-manifest.json"
    ]

    with pytest.raises(DraftPrPublisherError, match="proof manifest artifact"):
        skills.open_draft_pr(
            run,
            worker_result,
            {
                "passed": True,
                "summary": "Verification passed.",
                "output": "npm test\nok",
                "commands": ["npm test"],
            },
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_stale_proof_manifest_base_commit(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    _update_proof_manifest(worker_result, base_commit="stale-base")

    with pytest.raises(DraftPrPublisherError, match="proof manifest base commit"):
        skills.open_draft_pr(
            run,
            worker_result,
            {
                "passed": True,
                "summary": "Verification passed.",
                "output": "npm test\nok",
                "commands": ["npm test"],
            },
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_stale_proof_manifest_base_ref(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    _update_proof_manifest(worker_result, base_ref="origin/stale")

    with pytest.raises(DraftPrPublisherError, match="proof manifest base ref"):
        skills.open_draft_pr(
            run,
            worker_result,
            {
                "passed": True,
                "summary": "Verification passed.",
                "output": "npm test\nok",
                "commands": ["npm test"],
            },
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_stale_proof_manifest_branch_name(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    _update_proof_manifest(worker_result, branch_name="monica/MOB-999-pdp-copy")

    with pytest.raises(DraftPrPublisherError, match="proof manifest branch"):
        skills.open_draft_pr(
            run,
            worker_result,
            {
                "passed": True,
                "summary": "Verification passed.",
                "output": "npm test\nok",
                "commands": ["npm test"],
            },
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_stale_proof_manifest_linear_issue(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                setup_commands=("npm run monica:seed-auth",),
                commands=("npm run monica:proof",),
            ),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result["proof"]["required_env_keys"] = ["MONICA_TEST_LOGIN_TOKEN"]
    manifest_path = Path(worker_result["proof"]["artifacts"][0])
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_payload["linear_identifier"] = "MOB-999"
    manifest_payload["linear_url"] = "https://linear.app/acme/issue/MOB-999"
    manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")

    with pytest.raises(DraftPrPublisherError, match="proof manifest Linear issue"):
        skills.open_draft_pr(
            run,
            worker_result,
            {
                "passed": True,
                "summary": "Verification passed.",
                "output": "npm test\nok",
                "commands": ["npm test"],
            },
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_stale_proof_manifest_run_id(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                setup_commands=("npm run monica:seed-auth",),
                commands=("npm run monica:proof",),
            ),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result["proof"]["required_env_keys"] = ["MONICA_TEST_LOGIN_TOKEN"]
    manifest_path = Path(worker_result["proof"]["artifacts"][0])
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_payload["run_id"] = "stale-run"
    manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")

    with pytest.raises(DraftPrPublisherError, match="proof manifest run id"):
        skills.open_draft_pr(
            run,
            worker_result,
            {
                "passed": True,
                "summary": "Verification passed.",
                "output": "npm test\nok",
                "commands": ["npm test"],
            },
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_missing_proof_manifest_run_id(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                setup_commands=("npm run monica:seed-auth",),
                commands=("npm run monica:proof",),
            ),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result["proof"]["required_env_keys"] = ["MONICA_TEST_LOGIN_TOKEN"]
    manifest_path = Path(worker_result["proof"]["artifacts"][0])
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_payload["worktree"] = str(tmp_path)
    manifest_payload.pop("run_id", None)
    manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")

    with pytest.raises(DraftPrPublisherError, match="proof manifest run id"):
        skills.open_draft_pr(
            run,
            worker_result,
            {
                "passed": True,
                "summary": "Verification passed.",
                "output": "npm test\nok",
                "commands": ["npm test"],
            },
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_stale_proof_manifest_worktree(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    _update_proof_manifest(worker_result, worktree=str(tmp_path / "stale-worktree"))

    with pytest.raises(DraftPrPublisherError, match="proof manifest worktree"):
        skills.open_draft_pr(
            run,
            worker_result,
            {
                "passed": True,
                "summary": "Verification passed.",
                "output": "npm test\nok",
                "commands": ["npm test"],
            },
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_stale_proof_manifest_target(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    _update_proof_manifest(
        worker_result,
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/stale-offer",
            "expected_text": "Stale Offer",
        },
    )

    with pytest.raises(DraftPrPublisherError, match="proof manifest target"):
        skills.open_draft_pr(
            run,
            worker_result,
            {
                "passed": True,
                "summary": "Verification passed.",
                "output": "npm test\nok",
                "commands": ["npm test"],
            },
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_stale_proof_manifest_setup_commands(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    _update_proof_manifest(worker_result, setup_commands=["npm run stale-auth"])

    with pytest.raises(DraftPrPublisherError, match="proof manifest setup commands"):
        skills.open_draft_pr(
            run,
            worker_result,
            {
                "passed": True,
                "summary": "Verification passed.",
                "output": "npm test\nok",
                "commands": ["npm test"],
            },
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_stale_proof_manifest_capture_commands(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    _update_proof_manifest(worker_result, commands=["npm run stale-proof"])

    with pytest.raises(DraftPrPublisherError, match="proof manifest commands"):
        skills.open_draft_pr(
            run,
            worker_result,
            {
                "passed": True,
                "summary": "Verification passed.",
                "output": "npm test\nok",
                "commands": ["npm test"],
            },
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_stale_proof_manifest_required_env_keys(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                setup_commands=("npm run monica:seed-auth",),
                commands=("npm run monica:proof",),
            ),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result["proof"]["required_env_keys"] = ["MONICA_TEST_LOGIN_TOKEN"]
    _update_proof_manifest(worker_result, required_env_keys=["MONICA_STALE_LOGIN_TOKEN"])

    with pytest.raises(DraftPrPublisherError, match="proof manifest required env keys"):
        skills.open_draft_pr(
            run,
            worker_result,
            {
                "passed": True,
                "summary": "Verification passed.",
                "output": "npm test\nok",
                "commands": ["npm test"],
            },
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_invalid_proof_manifest_required_env_key(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                setup_commands=("npm run monica:seed-auth",),
                commands=("npm run monica:proof",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
            ),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result["proof"]["required_env_keys"] = ["MONICA_TEST_LOGIN_TOKEN"]
    _update_proof_manifest(
        worker_result,
        required_env_keys=["MONICA_TEST_LOGIN_TOKEN=secret"],
    )

    with pytest.raises(DraftPrPublisherError) as excinfo:
        skills.open_draft_pr(
            run,
            worker_result,
            {
                "passed": True,
                "summary": "Verification passed.",
                "output": "npm test\nok",
                "commands": ["npm test"],
            },
        )

    message = str(excinfo.value)
    assert "proof manifest required env keys are invalid" in message
    assert "MONICA_TEST_LOGIN_TOKEN" in message
    assert "secret" not in message
    assert publisher.calls == []


def test_default_skills_open_draft_pr_requires_proof_setup_commands_to_match_config(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                setup_commands=("npm run monica:seed-current-auth",),
                commands=("npm run monica:proof",),
            ),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)

    with pytest.raises(DraftPrPublisherError, match="configured proof setup commands"):
        skills.open_draft_pr(
            run,
            worker_result,
            {
                "passed": True,
                "summary": "Verification passed.",
                "output": "npm test\nok",
                "commands": ["npm test"],
            },
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_requires_proof_required_env_keys_to_match_config(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                setup_commands=("npm run monica:seed-auth",),
                commands=("npm run monica:proof",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
            ),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result["proof"]["required_env_keys"] = ["OTHER_TEST_TOKEN"]

    with pytest.raises(DraftPrPublisherError, match="configured proof required env keys"):
        skills.open_draft_pr(
            run,
            worker_result,
            {
                "passed": True,
                "summary": "Verification passed.",
                "output": "npm test\nok",
                "commands": ["npm test"],
            },
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_requires_configured_proof_required_env_keys(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                setup_commands=("npm run monica:seed-auth",),
                commands=("npm run monica:proof",),
            ),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)

    with pytest.raises(DraftPrPublisherError, match="configured proof required env keys"):
        skills.open_draft_pr(
            run,
            worker_result,
            {
                "passed": True,
                "summary": "Verification passed.",
                "output": "npm test\nok",
                "commands": ["npm test"],
            },
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_rejects_invalid_configured_required_env_keys(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                setup_commands=("npm run monica:seed-auth",),
                commands=("npm run monica:proof",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN=secret",),
            ),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result["proof"]["required_env_keys"] = ["MONICA_TEST_LOGIN_TOKEN=secret"]
    _update_proof_manifest(
        worker_result,
        required_env_keys=["MONICA_TEST_LOGIN_TOKEN=secret"],
    )

    with pytest.raises(DraftPrPublisherError) as excinfo:
        skills.open_draft_pr(
            run,
            worker_result,
            {
                "passed": True,
                "summary": "Verification passed.",
                "output": "npm test\nok",
                "commands": ["npm test"],
            },
        )

    message = str(excinfo.value)
    assert "configured proof required env keys are invalid" in message
    assert "MONICA_TEST_LOGIN_TOKEN" in message
    assert "secret" not in message
    assert publisher.calls == []


def test_default_skills_open_draft_pr_requires_proof_capture_commands_to_match_config(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                setup_commands=("npm run monica:seed-auth",),
                commands=("npm run monica:proof-current-target",),
            ),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)

    with pytest.raises(DraftPrPublisherError, match="configured proof commands"):
        skills.open_draft_pr(
            run,
            worker_result,
            {
                "passed": True,
                "summary": "Verification passed.",
                "output": "npm test\nok",
                "commands": ["npm test"],
            },
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_requires_target_text_evidence_artifacts(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                setup_commands=("npm run monica:seed-auth",),
                commands=("npm run monica:proof",),
            ),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    proof = worker_result["proof"]
    artifacts_without_target_text = [
        proof["artifacts"][0],
        proof["artifacts"][1],
        proof["artifacts"][2],
        proof["artifacts"][5],
        proof["artifacts"][6],
    ]
    proof["artifacts"] = artifacts_without_target_text
    manifest_path = Path(artifacts_without_target_text[0])
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_payload["proof_artifacts"] = artifacts_without_target_text[1:]
    manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")

    with pytest.raises(DraftPrPublisherError, match="proof target evidence"):
        skills.open_draft_pr(
            run,
            worker_result,
            {
                "passed": True,
                "summary": "Verification passed.",
                "output": "npm test\nok",
                "commands": ["npm test"],
            },
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_requires_target_route_evidence_artifacts(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                setup_commands=("npm run monica:seed-auth",),
                commands=("npm run monica:proof",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
            ),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    proof = worker_result["proof"]
    proof["required_env_keys"] = ["MONICA_TEST_LOGIN_TOKEN"]
    proof["artifacts"] = [
        artifact
        for artifact in proof["artifacts"]
        if "metro.stdout.log" not in Path(str(artifact)).name
    ]
    manifest_path = Path(proof["artifacts"][0])
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_payload["proof_artifacts"] = [
        artifact
        for artifact in proof["artifacts"]
        if Path(str(artifact)).name != "monica-proof-manifest.json"
    ]
    manifest_payload["required_env_keys"] = ["MONICA_TEST_LOGIN_TOKEN"]
    manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")

    with pytest.raises(DraftPrPublisherError, match="proof target route evidence"):
        skills.open_draft_pr(
            run,
            worker_result,
            {
                "passed": True,
                "summary": "Verification passed.",
                "output": "npm test\nok",
                "commands": ["npm test"],
            },
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_rejects_route_that_does_not_match_target(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                setup_commands=("npm run monica:seed-auth",),
                commands=("npm run monica:proof",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
            ),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    proof = worker_result["proof"]
    for artifact in proof["artifacts"]:
        path = Path(str(artifact))
        if path.name in {"ios-metro.stdout.log", "android-metro.stdout.log"}:
            path.write_text(
                "LOG  [APP-PERF-METRIC] ui.load | screen.load /Home | 180ms | ok",
                encoding="utf-8",
            )

    with pytest.raises(DraftPrPublisherError, match="proof target route does not match"):
        skills.open_draft_pr(
            run,
            worker_result,
            {
                "passed": True,
                "summary": "Verification passed.",
                "output": "npm test\nok",
                "commands": ["npm test"],
            },
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_does_not_accept_manifest_as_target_text_evidence(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                setup_commands=("npm run monica:seed-auth",),
                commands=("npm run monica:proof",),
            ),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    proof = worker_result["proof"]
    manifest_path = Path(proof["artifacts"][0])
    artifacts_without_target_text = [
        proof["artifacts"][1],
        proof["artifacts"][2],
        proof["artifacts"][5],
        proof["artifacts"][6],
    ]
    manifest_payload = json.loads(Path(proof["artifacts"][0]).read_text(encoding="utf-8"))
    manifest_payload["proof_artifacts"] = artifacts_without_target_text
    manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
    proof["artifacts"] = [str(manifest_path), *artifacts_without_target_text]

    with pytest.raises(DraftPrPublisherError, match="proof target evidence"):
        skills.open_draft_pr(
            run,
            worker_result,
            {
                "passed": True,
                "summary": "Verification passed.",
                "output": "npm test\nok",
                "commands": ["npm test"],
            },
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_auth_fallback_proof_artifact(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                setup_commands=("npm run monica:seed-auth",),
                commands=("npm run monica:proof",),
            ),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    proof = worker_result["proof"]
    auth_log = tmp_path / "proof" / "ios-metro.stdout.log"
    auth_log.write_text("LOG [auth] not logged in -> onboarding\n", encoding="utf-8")
    proof["artifacts"].append(str(auth_log))
    _update_proof_manifest(
        worker_result,
        proof_artifacts=[
            path
            for path in proof["artifacts"]
            if Path(path).name != "monica-proof-manifest.json"
        ],
    )

    with pytest.raises(DraftPrPublisherError, match="auth/onboarding proof fallback"):
        skills.open_draft_pr(
            run,
            worker_result,
            {
                "passed": True,
                "summary": "Verification passed.",
                "output": "npm test\nok",
                "commands": ["npm test"],
            },
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_stale_proof_manifest_artifacts(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    _update_proof_manifest(
        worker_result,
        proof_artifacts=[
            worker_result["proof"]["artifacts"][1],
            str(tmp_path / "proof" / "stale-android-pdp-fixed.mp4"),
        ],
    )

    with pytest.raises(DraftPrPublisherError, match="proof manifest artifacts"):
        skills.open_draft_pr(
            run,
            worker_result,
            {
                "passed": True,
                "summary": "Verification passed.",
                "output": "npm test\nok",
                "commands": ["npm test"],
            },
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_artifacts_outside_manifest_dir_before_linear_context(
    tmp_path,
):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_issue_id="issue-id",
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    linear_client = FakeLinearClient([])
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                setup_commands=("npm run monica:seed-auth",),
                commands=("npm run monica:proof",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
            ),
        ),
        state=state,
        linear_client=linear_client,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result["proof"]["required_env_keys"] = ["MONICA_TEST_LOGIN_TOKEN"]
    manifest_path = Path(worker_result["proof"]["artifacts"][0])
    stale_root = tmp_path / "stale-proof"
    stale_root.mkdir()
    stale_artifacts = [
        stale_root / "ios-pdp-fixed.png",
        stale_root / "android-pdp-fixed.mp4",
        stale_root / "ios-target.log",
        stale_root / "android-ui.xml",
        stale_root / "ios-metro.stdout.log",
        stale_root / "android-metro.stdout.log",
    ]
    stale_artifacts[0].write_bytes(b"ios proof")
    stale_artifacts[1].write_bytes(b"android proof")
    stale_artifacts[2].write_text("iOS visible target text: Fitness First", encoding="utf-8")
    stale_artifacts[3].write_text(
        '<node text="Fitness First" resource-id="marketplace-pdp-title" />',
        encoding="utf-8",
    )
    stale_artifacts[4].write_text(
        "LOG  [APP-PERF-METRIC] ui.load | screen.load /MarketplacePdp | 180ms | ok",
        encoding="utf-8",
    )
    stale_artifacts[5].write_text(
        "LOG  [APP-PERF-METRIC] ui.load | screen.load /MarketplacePdp | 190ms | ok",
        encoding="utf-8",
    )
    stale_artifact_paths = [str(path) for path in stale_artifacts]
    worker_result["proof"]["artifacts"] = [str(manifest_path), *stale_artifact_paths]
    worker_result["proof"]["shareable_artifacts"] = [
        {
            "platform": "ios",
            "path": stale_artifact_paths[0],
            "url": "https://slack.example/files/ios-pdp-fixed.png",
        },
        {
            "platform": "android",
            "path": stale_artifact_paths[1],
            "url": "https://slack.example/files/android-pdp-fixed.mp4",
        },
    ]
    _update_proof_manifest(
        worker_result,
        proof_artifacts=stale_artifact_paths,
        required_env_keys=["MONICA_TEST_LOGIN_TOKEN"],
    )

    with pytest.raises(DraftPrPublisherError, match="proof manifest directory"):
        skills.open_draft_pr(
            run,
            worker_result,
            {
                "passed": True,
                "summary": "Verification passed.",
                "output": "npm test\nok",
                "commands": ["npm test"],
            },
        )

    assert linear_client.comment_calls == []
    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_stale_proof_manifest_platforms(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
            verification=VerificationConfig(commands=("npm test",)),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    _update_proof_manifest(
        worker_result,
        platforms=["ios"],
        proof_artifacts=[
            worker_result["proof"]["artifacts"][1],
            worker_result["proof"]["artifacts"][2],
        ],
    )

    with pytest.raises(DraftPrPublisherError, match="proof manifest platforms"):
        skills.open_draft_pr(
            run,
            worker_result,
            {
                "passed": True,
                "summary": "Verification passed.",
                "output": "npm test\nok",
                "commands": ["npm test"],
            },
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_duplicate_shareable_links_before_publishing(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    shared_url = "https://slack.example/files/shared-proof.png"
    worker_result["proof"]["shareable_artifacts"][0]["url"] = shared_url
    worker_result["proof"]["shareable_artifacts"][1]["url"] = shared_url

    with pytest.raises(DraftPrPublisherError, match="duplicate shareable proof links"):
        skills.open_draft_pr(
            run,
            worker_result,
            {"passed": True, "summary": "Verification passed.", "output": "npm test\nok"},
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_shareable_links_for_wrong_local_artifacts(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result["proof"]["shareable_artifacts"] = [
        {
            "platform": "ios",
            "path": str(tmp_path / "other-proof" / "ios-pdp-fixed.png"),
            "url": "https://slack.example/files/ios-pdp-fixed.png",
        },
        {
            "platform": "android",
            "path": str(tmp_path / "other-proof" / "android-pdp-fixed.mp4"),
            "url": "https://slack.example/files/android-pdp-fixed.mp4",
        },
    ]

    with pytest.raises(DraftPrPublisherError, match="shareable proof links must match"):
        skills.open_draft_pr(
            run,
            worker_result,
            {"passed": True, "summary": "Verification passed.", "output": "npm test\nok"},
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_loopback_shareable_links(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result["proof"]["shareable_artifacts"] = [
        {
            "platform": "ios",
            "path": worker_result["proof"]["artifacts"][0],
            "url": "http://localhost:3000/ios-pdp-fixed.png",
        },
        {
            "platform": "android",
            "path": worker_result["proof"]["artifacts"][1],
            "url": "https://127.0.0.1/android-pdp-fixed.mp4",
        },
    ]

    with pytest.raises(DraftPrPublisherError, match="shareable proof links are required"):
        skills.open_draft_pr(
            run,
            worker_result,
            {"passed": True, "summary": "Verification passed.", "output": "npm test\nok"},
        )

    assert publisher.calls == []


def test_default_skills_open_draft_pr_refuses_hostless_shareable_links(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result["proof"]["shareable_artifacts"] = [
        {
            "platform": "ios",
            "path": worker_result["proof"]["artifacts"][0],
            "url": "https:///ios-pdp-fixed.png",
        },
        {
            "platform": "android",
            "path": worker_result["proof"]["artifacts"][1],
            "url": "http:///android-pdp-fixed.mp4",
        },
    ]

    with pytest.raises(DraftPrPublisherError, match="shareable proof links are required"):
        skills.open_draft_pr(
            run,
            worker_result,
            {"passed": True, "summary": "Verification passed.", "output": "npm test\nok"},
        )

    assert publisher.calls == []


@pytest.mark.parametrize(
    ("ios_url", "android_url"),
    (
        ("https://10.0.0.5/ios-pdp-fixed.png", "http://192.168.1.12/android-pdp-fixed.mp4"),
        ("https://proof.local/ios-pdp-fixed.png", "http://monica-proof/android-pdp-fixed.mp4"),
        ("https://slack.example/files/ios-pdp-fixed.png", "https://proof.internal/android-pdp-fixed.mp4"),
    ),
)
def test_default_skills_open_draft_pr_refuses_private_network_shareable_links(
    tmp_path,
    ios_url,
    android_url,
):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="Marketplace PDP copy is wrong",
    )
    run = state.update_run(
        run.id,
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        approved_by_user_id="U_ALLOWED",
    )
    publisher = CapturingPrPublisher()
    skills = DefaultMonicaSkills(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev", branch_prefix="monica"),
            slack=SlackConfig(approver_user_ids=("U_ALLOWED",)),
        ),
        state=state,
        pr_publisher=publisher,
    )
    worker_result = _valid_approved_pr_worker_result(tmp_path, run=run)
    worker_result["proof"]["shareable_artifacts"] = [
        {
            "platform": "ios",
            "path": worker_result["proof"]["artifacts"][0],
            "url": ios_url,
        },
        {
            "platform": "android",
            "path": worker_result["proof"]["artifacts"][1],
            "url": android_url,
        },
    ]

    with pytest.raises(DraftPrPublisherError, match="shareable proof links are required"):
        skills.open_draft_pr(
            run,
            worker_result,
            {"passed": True, "summary": "Verification passed.", "output": "npm test\nok"},
        )

    assert publisher.calls == []


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
