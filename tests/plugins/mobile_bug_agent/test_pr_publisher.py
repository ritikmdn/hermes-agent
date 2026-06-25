from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from plugins.mobile_bug_agent.pr_publisher import DraftPrPublisher, DraftPrPublisherError


def _png_bytes() -> bytes:
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAFElEQVR4nGNgYGD4//8/w38GEAMAIewE/ITr/YQAAAAASUVORK5CYII="
    )


def _artifact_metadata(paths: tuple[Path, ...]) -> list[dict[str, object]]:
    metadata: list[dict[str, object]] = []
    for path in paths:
        name = path.name.casefold()
        platform = "ios" if "ios" in name else "android" if "android" in name else ""
        metadata.append(
            {
                "path": str(path),
                "platform": platform,
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    return metadata


VALID_PR_BODY = """## Links
- Linear: https://linear.app/acme/issue/MOB-42
- Slack: https://example.slack.com/archives/C_MOBILE/p1710000000000200
- Base: origin/main @ abc1234

## Verification
Verification passed.

```
npm test
ok
```

## Proof
Setup commands:
- npm run monica:seed-auth
Proof commands:
- npm run monica:proof
Required env keys: MONICA_TEST_LOGIN_TOKEN
Target: elixir-card://marketplace/offer/fitness-first
Expected text: Fitness First
- iOS: https://slack.example/files/ios-pdp-fixed.png (Slack upload: F-ios-pdp-fixed.png, ios-pdp-fixed.png)
- Android: https://slack.example/files/android-pdp-fixed.mp4 (Slack upload: F-android-pdp-fixed.mp4, android-pdp-fixed.mp4)
Local artifacts (debug): /tmp/monica-proof/monica-proof-manifest.json, /tmp/monica-proof/ios-pdp-fixed.png, /tmp/monica-proof/android-pdp-fixed.mp4
"""


GENERIC_VERIFICATION_PR_BODY = """## Links
- Linear: https://linear.app/acme/issue/MOB-42
- Slack: https://example.slack.com/archives/C_MOBILE/p1710000000000200
- Base: origin/main @ abc1234

## Verification
Verification passed.

## Proof
Setup commands:
- npm run monica:seed-auth
Proof commands:
- npm run monica:proof
Required env keys: MONICA_TEST_LOGIN_TOKEN
Target: elixir-card://marketplace/offer/fitness-first
Expected text: Fitness First
- iOS: https://slack.example/files/ios-pdp-fixed.png (Slack upload: F-ios-pdp-fixed.png, ios-pdp-fixed.png)
- Android: https://slack.example/files/android-pdp-fixed.mp4 (Slack upload: F-android-pdp-fixed.mp4, android-pdp-fixed.mp4)
"""


def _mark_git_worktree(path):
    (path / ".git").write_text("gitdir: /tmp/fake-mobile-worktree-git-dir", encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _valid_pr_body_local_artifacts(tmp_path):
    proof_dir = Path("/tmp/monica-proof")
    proof_dir.mkdir(parents=True, exist_ok=True)
    visual_artifacts = (
        "ios-pdp-fixed.png",
        "ios-pdp-fixed.heic",
        "android-pdp-fixed.mp4",
        "android-pdp-fixed.webm",
    )
    target_text_artifacts = (
        "ios-target.log",
        "android-ui.xml",
        "ios-metro.stdout.log",
        "android-metro.stdout.log",
    )
    for name in visual_artifacts:
        if Path(name).suffix.lower() == ".png":
            (proof_dir / name).write_bytes(_png_bytes())
        else:
            (proof_dir / name).write_bytes(b"proof artifact")
    (proof_dir / "ios-target.log").write_text(
        "Monica iOS proof target text observed.\nexpected_text=Fitness First\n",
        encoding="utf-8",
    )
    (proof_dir / "android-ui.xml").write_text(
        "<hierarchy><node text='Fitness First' /></hierarchy>\n",
        encoding="utf-8",
    )
    (proof_dir / "ios-metro.stdout.log").write_text(
        "LOG  [APP-PERF-METRIC] ui.load | screen.load /MarketplacePdp | 180ms | ok\n",
        encoding="utf-8",
    )
    (proof_dir / "android-metro.stdout.log").write_text(
        "LOG  [APP-PERF-METRIC] ui.load | screen.load /MarketplacePdp | 190ms | ok\n",
        encoding="utf-8",
    )
    artifact_paths = tuple(
        proof_dir / name for name in (*visual_artifacts, *target_text_artifacts)
    )
    (proof_dir / "monica-proof-manifest.json").write_text(
        json.dumps(
            {
                "run_id": "run-123",
                "proof_artifacts": [str(path) for path in artifact_paths],
                "proof_artifact_metadata": _artifact_metadata(artifact_paths),
                "proof_target": {
                    "deep_link": "elixir-card://marketplace/offer/fitness-first",
                    "expected_text": "Fitness First",
                },
                "linear_identifier": "MOB-42",
                "linear_url": "https://linear.app/acme/issue/MOB-42",
                "branch_name": "monica/MOB-42-checkout-crash",
                "worktree": str(tmp_path),
                "base_ref": "origin/main",
                "base_commit": "abc1234",
                "setup_commands": ["npm run monica:seed-auth"],
                "commands": ["npm run monica:proof"],
                "required_env_keys": ["MONICA_TEST_LOGIN_TOKEN"],
                "platforms": ["ios", "android"],
            }
        ),
        encoding="utf-8",
    )


def test_publisher_commits_pushes_and_creates_draft_pr(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return " M src/Checkout.tsx\n"
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    url = publisher.publish(
        worktree=_mark_git_worktree(tmp_path),
        branch_name="monica/MOB-42-checkout-crash",
        base_branch="main",
        title="[MOB-42] Fix Android checkout crash",
        body=VALID_PR_BODY,
    )

    assert url == "https://github.com/acme/mobile/pull/123"
    assert calls[0][0] == ["git", "branch", "--show-current"]
    assert calls[1][0] == ["git", "status", "--porcelain"]
    assert calls[2][0] == ["git", "add", "-A"]
    assert calls[3][0] == [
        "git",
        "-c",
        "user.name=Monica",
        "-c",
        "user.email=monica@hermes.local",
        "commit",
        "-m",
        "[MOB-42] Fix Android checkout crash",
    ]
    assert calls[4][0] == ["git", "diff", "--name-only", "origin/main...HEAD"]
    assert calls[5][0] == ["git", "push", "origin", "HEAD:monica/MOB-42-checkout-crash"]
    assert calls[6][0][:4] == ["gh", "pr", "create", "--draft"]
    assert calls[6][1] == tmp_path


def test_publisher_rejects_manifest_that_drops_body_proof_screen_before_running_commands(
    tmp_path,
):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Marketplace.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)
    body = VALID_PR_BODY.replace(
        "Expected text: Fitness First\n",
        "Expected text: Fitness First\nScreen: /MarketplacePdp\n",
    )

    with pytest.raises(DraftPrPublisherError, match="manifest target"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=body,
        )

    assert calls == []


def test_publisher_accepts_origin_prefixed_base_branch(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    url = publisher.publish(
        worktree=_mark_git_worktree(tmp_path),
        branch_name="monica/MOB-42-checkout-crash",
        base_branch="origin/main",
        title="[MOB-42] Fix Android checkout crash",
        body=VALID_PR_BODY,
    )

    assert url == "https://github.com/acme/mobile/pull/123"
    assert ["git", "diff", "--name-only", "origin/main...HEAD"] in [
        call[0] for call in calls
    ]
    assert not any("origin/origin/main" in call[0] for call in calls)


def test_publisher_refuses_noop_draft_pr(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return ""
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="No committed changes"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY,
        )

    assert [call[0][0:2] for call in calls] == [
        ["git", "branch"],
        ["git", "status"],
        ["git", "diff"],
    ]


def test_publisher_requires_branch_before_running_commands(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="branch_name is required"):
        publisher.publish(
            worktree=tmp_path,
            branch_name="",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY,
        )

    assert calls == []


def test_publisher_requires_title_before_running_commands(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="title is required"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="   ",
            body=VALID_PR_BODY,
        )

    assert calls == []


def test_publisher_requires_body_before_running_commands(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Marketplace.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="body is required"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body="",
        )

    assert calls == []


def test_publisher_requires_linear_reference_before_running_commands(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Marketplace.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="body must include Linear"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body="""## Links
- Slack: https://example.slack.com/archives/C_MOBILE/p1710000000000200

## Verification
Verification passed.

```
npm test
ok
```
""",
        )

    assert calls == []


def test_publisher_rejects_non_linear_url_before_running_commands(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="body must include Linear"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace(
                "- Linear: https://linear.app/acme/issue/MOB-42",
                "- Linear: https://not-linear.example/acme/issue/MOB-42",
            ),
        )

    assert calls == []


def test_publisher_requires_slack_reference_before_running_commands(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="body must include Slack"):
        publisher.publish(
            worktree=tmp_path,
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body="""## Links
- Linear: https://linear.app/acme/issue/MOB-42

## Verification
Verification passed.

```
npm test
ok
```
""",
        )

    assert calls == []


def test_publisher_rejects_empty_slack_channel_thread_context_before_running_commands(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="body must include Slack thread context"):
        publisher.publish(
            worktree=tmp_path,
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace(
                "- Slack: https://example.slack.com/archives/C_MOBILE/p1710000000000200",
                "- Slack: channel= thread=",
            ),
        )

    assert calls == []


def test_publisher_rejects_invalid_slack_channel_thread_context_before_running_commands(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="body must include Slack thread context"):
        publisher.publish(
            worktree=tmp_path,
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace(
                "- Slack: https://example.slack.com/archives/C_MOBILE/p1710000000000200",
                "- Slack: channel=mobile thread=latest",
            ),
        )

    assert calls == []


def test_publisher_rejects_non_slack_url_as_slack_context_before_running_commands(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="body must include Slack thread context"):
        publisher.publish(
            worktree=tmp_path,
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace(
                "- Slack: https://example.slack.com/archives/C_MOBILE/p1710000000000200",
                "- Slack: https://not-slack.example/thread",
            ),
        )

    assert calls == []


def test_publisher_requires_verification_evidence_before_running_commands(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="body must include verification"):
        publisher.publish(
            worktree=tmp_path,
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body="""## Links
- Linear: https://linear.app/acme/issue/MOB-42
- Slack: https://example.slack.com/archives/C_MOBILE/p1710000000000200
""",
        )

    assert calls == []


def test_publisher_rejects_generic_verification_success_without_test_evidence(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="verification evidence"):
        publisher.publish(
            worktree=tmp_path,
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=GENERIC_VERIFICATION_PR_BODY,
        )

    assert calls == []


def test_publisher_rejects_prose_only_verification_without_command_evidence(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="verification evidence"):
        publisher.publish(
            worktree=tmp_path,
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body="""## Links
- Linear: https://linear.app/acme/issue/MOB-42
- Slack: https://example.slack.com/archives/C_MOBILE/p1710000000000200
- Base: origin/main @ abc1234

## Verification
All Monica verification checks passed.

## Proof
Setup commands:
- npm run monica:seed-auth
Proof commands:
- npm run monica:proof
Target: elixir-card://marketplace/offer/fitness-first
Expected text: Fitness First
- iOS: https://slack.example/files/ios-pdp-fixed.png (Slack upload: F-ios-pdp-fixed.png, ios-pdp-fixed.png)
- Android: https://slack.example/files/android-pdp-fixed.mp4 (Slack upload: F-android-pdp-fixed.mp4, android-pdp-fixed.mp4)
Local artifacts (debug): /tmp/monica-proof/monica-proof-manifest.json, /tmp/monica-proof/ios-pdp-fixed.png, /tmp/monica-proof/android-pdp-fixed.mp4
""",
        )

    assert calls == []


def test_publisher_accepts_git_diff_check_as_verification_evidence(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Marketplace.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    result = publisher.publish(
        worktree=_mark_git_worktree(tmp_path),
        branch_name="monica/MOB-42-checkout-crash",
        base_branch="main",
        title="[MOB-42] Fix Marketplace copy",
        body=VALID_PR_BODY.replace("npm test\nok", "git diff --check\nok"),
    )

    assert result == "https://github.com/acme/mobile/pull/123"
    assert calls


def test_publisher_accepts_markdown_bullet_verification_command_evidence(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Marketplace.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)
    body = VALID_PR_BODY.replace(
        "```\nnpm test\nok\n```",
        "Commands:\n- npm test\n\nAll checks passed.",
    )

    result = publisher.publish(
        worktree=_mark_git_worktree(tmp_path),
        branch_name="monica/MOB-42-checkout-crash",
        base_branch="main",
        title="[MOB-42] Fix Marketplace copy",
        body=body,
    )

    assert result == "https://github.com/acme/mobile/pull/123"
    assert calls


def test_publisher_rejects_manifest_without_artifact_metadata(tmp_path):
    manifest_path = Path("/tmp/monica-proof/monica-proof-manifest.json")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload.pop("proof_artifact_metadata", None)
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Marketplace.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="proof manifest artifact metadata"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Marketplace copy",
            body=VALID_PR_BODY,
        )

    assert calls == []


def test_publisher_does_not_count_required_env_key_names_as_setup_commands(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Marketplace.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="proof setup and capture commands"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Marketplace copy",
            body="""## Links
- Linear: https://linear.app/acme/issue/MOB-42
- Slack: https://example.slack.com/archives/C_MOBILE/p1710000000000200
- Base: origin/main @ abc1234

## Verification

```
npm test
ok
```

## Proof
Setup commands:
Required env keys: MONICA_TEST_LOGIN_TOKEN
- MONICA_TEST_LOGIN_OTP
Proof commands:
- npm run monica:proof
Target: elixir-card://marketplace/offer/fitness-first
Expected text: Fitness First
- iOS: https://slack.example/files/ios-pdp-fixed.png (Slack upload: F-ios-pdp-fixed.png, ios-pdp-fixed.png)
- Android: https://slack.example/files/android-pdp-fixed.mp4 (Slack upload: F-android-pdp-fixed.mp4, android-pdp-fixed.mp4)
Local artifacts (debug): /tmp/monica-proof/monica-proof-manifest.json, /tmp/monica-proof/ios-pdp-fixed.png, /tmp/monica-proof/android-pdp-fixed.mp4
""",
        )

    assert calls == []


def test_publisher_requires_proof_evidence_before_running_commands(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="body must include proof"):
        publisher.publish(
            worktree=tmp_path,
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body="""## Links
- Linear: https://linear.app/acme/issue/MOB-42
- Slack: https://example.slack.com/archives/C_MOBILE/p1710000000000200

## Verification
Verification passed.

```
npm test
ok
```
""",
        )

    assert calls == []


def test_publisher_requires_ios_and_android_shareable_proof_links_before_running_commands(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="iOS and Android shareable proof links"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body="""## Links
- Linear: https://linear.app/acme/issue/MOB-42
- Slack: https://example.slack.com/archives/C_MOBILE/p1710000000000200

## Verification
Verification passed.

```
npm test
ok
```

## Proof
Target: elixir-card://marketplace/offer/fitness-first
Expected text: Fitness First
- /tmp/monica-proof/ios-pdp-fixed.png
- /tmp/monica-proof/android-pdp-fixed.mp4
""",
        )

    assert calls == []


@pytest.mark.parametrize(
    ("ios_url", "android_url"),
    (
        ("http://localhost/files/ios-pdp-fixed.png", "https://slack.example/files/android-pdp-fixed.mp4"),
        ("https://127.0.0.1/files/ios-pdp-fixed.png", "https://slack.example/files/android-pdp-fixed.mp4"),
        ("https://10.0.0.8/files/ios-pdp-fixed.png", "https://slack.example/files/android-pdp-fixed.mp4"),
        ("https://proof.local/files/ios-pdp-fixed.png", "https://slack.example/files/android-pdp-fixed.mp4"),
        ("https://monica-proof/files/ios-pdp-fixed.png", "https://slack.example/files/android-pdp-fixed.mp4"),
        ("https:///files/ios-pdp-fixed.png", "https://slack.example/files/android-pdp-fixed.mp4"),
        ("https://slack.example/files/ios-pdp-fixed.png", "http://localhost/files/android-pdp-fixed.mp4"),
        ("https://slack.example/files/ios-pdp-fixed.png", "https://proof.internal/files/android-pdp-fixed.mp4"),
    ),
)
def test_publisher_refuses_unshareable_proof_links_before_running_commands(
    tmp_path,
    ios_url,
    android_url,
):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        return ""

    publisher = DraftPrPublisher(run_command=run_command)
    body = (
        VALID_PR_BODY
        .replace("https://slack.example/files/ios-pdp-fixed.png", ios_url)
        .replace("https://slack.example/files/android-pdp-fixed.mp4", android_url)
    )

    with pytest.raises(DraftPrPublisherError, match="iOS and Android shareable proof links"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=body,
        )

    assert calls == []


def test_publisher_requires_distinct_ios_and_android_shareable_proof_links(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="distinct iOS and Android shareable proof links"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body="""## Links
- Linear: https://linear.app/acme/issue/MOB-42
- Slack: https://example.slack.com/archives/C_MOBILE/p1710000000000200
- Base: origin/main @ abc1234

## Verification
Verification passed.

```
npm test
ok
```

## Proof
Target: elixir-card://marketplace/offer/fitness-first
Expected text: Fitness First
- iOS: https://slack.example/files/shared-proof.png
- Android: https://slack.example/files/shared-proof.png
""",
        )

    assert calls == []


def test_publisher_requires_slack_upload_metadata_for_shareable_proof_links(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)
    body = VALID_PR_BODY.replace(
        " (Slack upload: F-ios-pdp-fixed.png, ios-pdp-fixed.png)",
        "",
    ).replace(
        " (Slack upload: F-android-pdp-fixed.mp4, android-pdp-fixed.mp4)",
        "",
    )

    with pytest.raises(DraftPrPublisherError, match="Slack upload metadata"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=body,
        )

    assert calls == []


def test_publisher_requires_local_ios_and_android_artifacts_before_running_commands(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="local iOS and Android proof artifacts"):
        publisher.publish(
            worktree=tmp_path,
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace(
                "Local artifacts (debug): /tmp/monica-proof/monica-proof-manifest.json, /tmp/monica-proof/ios-pdp-fixed.png, /tmp/monica-proof/android-pdp-fixed.mp4\n",
                "",
            ),
        )

    assert calls == []


def test_publisher_requires_local_proof_manifest_before_running_commands(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="proof manifest"):
        publisher.publish(
            worktree=tmp_path,
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace(
                "/tmp/monica-proof/monica-proof-manifest.json, ",
                "",
            ),
        )

    assert calls == []


def test_publisher_rejects_missing_local_proof_artifact_files_before_running_commands(tmp_path):
    calls = []
    missing_root = tmp_path / "missing-proof"

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="local proof artifact files must exist"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace("/tmp/monica-proof", str(missing_root)),
        )

    assert calls == []


def test_publisher_rejects_unreadable_local_proof_screenshot_before_running_commands(tmp_path):
    calls = []
    proof_root = Path("/tmp/monica-proof")
    (proof_root / "ios-pdp-fixed.png").write_text("not a png", encoding="utf-8")

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="local proof artifact files must be readable images"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY,
        )

    assert calls == []


def test_publisher_rejects_unreadable_local_proof_manifest_before_running_commands(tmp_path):
    calls = []
    proof_root = tmp_path / "proof"
    proof_root.mkdir()
    (proof_root / "monica-proof-manifest.json").write_text("not json", encoding="utf-8")
    (proof_root / "ios-pdp-fixed.png").write_bytes(_png_bytes())
    (proof_root / "android-pdp-fixed.mp4").write_bytes(b"android proof")

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="local proof manifest"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace("/tmp/monica-proof", str(proof_root)),
        )

    assert calls == []


def test_publisher_rejects_auth_fallback_manifest_artifact_before_running_commands(tmp_path):
    calls = []
    proof_root = tmp_path / "proof"
    proof_root.mkdir()
    ios_artifact = proof_root / "ios-pdp-fixed.png"
    android_artifact = proof_root / "android-pdp-fixed.mp4"
    ios_target = proof_root / "ios-target.log"
    android_target = proof_root / "android-ui.xml"
    ios_auth_log = proof_root / "ios-metro.stdout.log"
    ios_artifact.write_bytes(_png_bytes())
    android_artifact.write_bytes(b"android proof")
    ios_target.write_text("visible target text: Fitness First", encoding="utf-8")
    android_target.write_text("<node text='Fitness First' />", encoding="utf-8")
    ios_auth_log.write_text("LOG [auth] not logged in -> onboarding\n", encoding="utf-8")
    (proof_root / "monica-proof-manifest.json").write_text(
        json.dumps(
            {
                "proof_artifacts": [
                    str(ios_artifact),
                    str(android_artifact),
                    str(ios_target),
                    str(android_target),
                    str(ios_auth_log),
                ],
                "proof_target": {
                    "deep_link": "elixir-card://marketplace/offer/fitness-first",
                    "expected_text": "Fitness First",
                },
                "base_ref": "origin/main",
                "base_commit": "abc1234",
                "setup_commands": ["npm run monica:seed-auth"],
                "commands": ["npm run monica:proof"],
                "required_env_keys": ["MONICA_TEST_LOGIN_TOKEN"],
                "platforms": ["ios", "android"],
            }
        ),
        encoding="utf-8",
    )

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="auth/onboarding proof fallback"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace("/tmp/monica-proof", str(proof_root)),
        )

    assert calls == []


def test_publisher_rejects_unattributed_auth_fallback_manifest_artifact_before_running_commands(
    tmp_path,
):
    calls = []
    proof_root = tmp_path / "proof"
    proof_root.mkdir()
    ios_artifact = proof_root / "ios-pdp-fixed.png"
    android_artifact = proof_root / "android-pdp-fixed.mp4"
    ios_target = proof_root / "ios-target.log"
    android_target = proof_root / "android-ui.xml"
    auth_log = proof_root / "metro.stdout.log"
    ios_artifact.write_bytes(_png_bytes())
    android_artifact.write_bytes(b"android proof")
    ios_target.write_text("visible target text: Fitness First", encoding="utf-8")
    android_target.write_text("<node text='Fitness First' />", encoding="utf-8")
    auth_log.write_text("LOG [auth] not logged in -> onboarding\n", encoding="utf-8")
    (proof_root / "monica-proof-manifest.json").write_text(
        json.dumps(
            {
                "proof_artifacts": [
                    str(ios_artifact),
                    str(android_artifact),
                    str(ios_target),
                    str(android_target),
                    str(auth_log),
                ],
                "proof_target": {
                    "deep_link": "elixir-card://marketplace/offer/fitness-first",
                    "expected_text": "Fitness First",
                },
                "base_ref": "origin/main",
                "base_commit": "abc1234",
                "setup_commands": ["npm run monica:seed-auth"],
                "commands": ["npm run monica:proof"],
                "required_env_keys": ["MONICA_TEST_LOGIN_TOKEN"],
                "platforms": ["ios", "android"],
            }
        ),
        encoding="utf-8",
    )

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="auth/onboarding proof fallback"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace("/tmp/monica-proof", str(proof_root)),
        )

    assert calls == []


@pytest.mark.parametrize(
    ("ios_route_log_text", "android_route_log_text"),
    (
        (
            "LOG  [APP-PERF-METRIC] ui.load | screen.load /SplashScreen | 6ms | ok\n",
            "LOG  [APP-PERF-METRIC] ui.load | screen.load /SplashScreen | 19ms | ok\n",
        ),
        (
            'LOG  [APP-PERF-METRIC] ui.load | ok {"screen": "/SplashScreen"}\n',
            'LOG  [APP-PERF-METRIC] ui.load | ok {"screen": "/SplashScreen"}\n',
        ),
        (
            "LOG  [APP-PERF-METRIC] ui.load | screen.load /Splash | 6ms | ok\n",
            "LOG  [APP-PERF-METRIC] ui.load | screen.load /Splash | 19ms | ok\n",
        ),
        (
            "LOG  [APP-PERF-METRIC] ui.load | screen.load /Splash-screen | 6ms | ok\n",
            "LOG  [APP-PERF-METRIC] ui.load | screen.load /Splash-screen | 19ms | ok\n",
        ),
    ),
)
def test_publisher_rejects_non_target_splash_manifest_artifact_before_running_commands(
    tmp_path,
    ios_route_log_text,
    android_route_log_text,
):
    calls = []
    proof_root = tmp_path / "proof"
    proof_root.mkdir()
    ios_artifact = proof_root / "ios-pdp-fixed.png"
    android_artifact = proof_root / "android-pdp-fixed.mp4"
    ios_target = proof_root / "ios-target.log"
    android_target = proof_root / "android-ui.xml"
    ios_route_log = proof_root / "ios-metro.stdout.log"
    android_route_log = proof_root / "android-metro.stdout.log"
    ios_artifact.write_bytes(_png_bytes())
    android_artifact.write_bytes(b"android proof")
    ios_target.write_text("visible target text: Fitness First", encoding="utf-8")
    android_target.write_text("<node text='Fitness First' />", encoding="utf-8")
    ios_route_log.write_text(ios_route_log_text, encoding="utf-8")
    android_route_log.write_text(android_route_log_text, encoding="utf-8")
    (proof_root / "monica-proof-manifest.json").write_text(
        json.dumps(
            {
                "run_id": "run-123",
                "proof_artifacts": [
                    str(ios_artifact),
                    str(android_artifact),
                    str(ios_target),
                    str(android_target),
                    str(ios_route_log),
                    str(android_route_log),
                ],
                "proof_target": {
                    "deep_link": "elixir-card://marketplace/offer/fitness-first",
                    "expected_text": "Fitness First",
                },
                "linear_identifier": "MOB-42",
                "linear_url": "https://linear.app/acme/issue/MOB-42",
                "branch_name": "monica/MOB-42-checkout-crash",
                "worktree": str(tmp_path),
                "base_ref": "origin/main",
                "base_commit": "abc1234",
                "setup_commands": ["npm run monica:seed-auth"],
                "commands": ["npm run monica:proof"],
                "required_env_keys": ["MONICA_TEST_LOGIN_TOKEN"],
                "platforms": ["ios", "android"],
            }
        ),
        encoding="utf-8",
    )

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Marketplace.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="non-target app screen"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace("/tmp/monica-proof", str(proof_root)),
        )

    assert calls == []


def test_publisher_rejects_manifest_missing_target_route_before_running_commands(tmp_path):
    calls = []
    proof_root = tmp_path / "proof"
    proof_root.mkdir()
    ios_artifact = proof_root / "ios-pdp-fixed.png"
    android_artifact = proof_root / "android-pdp-fixed.mp4"
    ios_target = proof_root / "ios-target.log"
    android_target = proof_root / "android-ui.xml"
    ios_artifact.write_bytes(_png_bytes())
    android_artifact.write_bytes(b"android proof")
    ios_target.write_text("visible target text: Fitness First", encoding="utf-8")
    android_target.write_text("<node text='Fitness First' />", encoding="utf-8")
    (proof_root / "monica-proof-manifest.json").write_text(
        json.dumps(
            {
                "run_id": "run-123",
                "proof_artifacts": [
                    str(ios_artifact),
                    str(android_artifact),
                    str(ios_target),
                    str(android_target),
                ],
                "proof_target": {
                    "deep_link": "elixir-card://marketplace/offer/fitness-first",
                    "expected_text": "Fitness First",
                },
                "linear_identifier": "MOB-42",
                "linear_url": "https://linear.app/acme/issue/MOB-42",
                "branch_name": "monica/MOB-42-checkout-crash",
                "worktree": str(tmp_path),
                "base_ref": "origin/main",
                "base_commit": "abc1234",
                "setup_commands": ["npm run monica:seed-auth"],
                "commands": ["npm run monica:proof"],
                "required_env_keys": ["MONICA_TEST_LOGIN_TOKEN"],
                "platforms": ["ios", "android"],
            }
        ),
        encoding="utf-8",
    )

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Marketplace.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="proof manifest target route"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace("/tmp/monica-proof", str(proof_root)),
        )

    assert calls == []


def test_publisher_rejects_manifest_route_that_does_not_match_target(tmp_path):
    calls = []
    proof_root = tmp_path / "proof"
    proof_root.mkdir()
    ios_artifact = proof_root / "ios-pdp-fixed.png"
    android_artifact = proof_root / "android-pdp-fixed.mp4"
    ios_target = proof_root / "ios-target.log"
    android_target = proof_root / "android-ui.xml"
    ios_route = proof_root / "ios-metro.stdout.log"
    android_route = proof_root / "android-metro.stdout.log"
    ios_artifact.write_bytes(_png_bytes())
    android_artifact.write_bytes(b"android proof")
    ios_target.write_text("visible target text: Fitness First", encoding="utf-8")
    android_target.write_text("<node text='Fitness First' />", encoding="utf-8")
    ios_route.write_text(
        "LOG  [APP-PERF-METRIC] ui.load | screen.load /Home | 180ms | ok",
        encoding="utf-8",
    )
    android_route.write_text(
        "LOG  [APP-PERF-METRIC] ui.load | screen.load /Home | 190ms | ok",
        encoding="utf-8",
    )
    (proof_root / "monica-proof-manifest.json").write_text(
        json.dumps(
            {
                "run_id": "run-123",
                "proof_artifacts": [
                    str(ios_artifact),
                    str(android_artifact),
                    str(ios_target),
                    str(android_target),
                    str(ios_route),
                    str(android_route),
                ],
                "proof_target": {
                    "deep_link": "elixir-card://marketplace/offer/fitness-first",
                    "expected_text": "Fitness First",
                },
                "linear_identifier": "MOB-42",
                "linear_url": "https://linear.app/acme/issue/MOB-42",
                "branch_name": "monica/MOB-42-checkout-crash",
                "worktree": str(tmp_path),
                "base_ref": "origin/main",
                "base_commit": "abc1234",
                "setup_commands": ["npm run monica:seed-auth"],
                "commands": ["npm run monica:proof"],
                "required_env_keys": ["MONICA_TEST_LOGIN_TOKEN"],
                "platforms": ["ios", "android"],
            }
        ),
        encoding="utf-8",
    )

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Marketplace.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="proof manifest target route does not match"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace("/tmp/monica-proof", str(proof_root)),
        )

    assert calls == []


def test_publisher_rejects_manifest_route_that_is_only_generic_marketplace(tmp_path):
    calls = []
    proof_root = tmp_path / "proof"
    proof_root.mkdir()
    ios_artifact = proof_root / "ios-pdp-fixed.png"
    android_artifact = proof_root / "android-pdp-fixed.mp4"
    ios_target = proof_root / "ios-target.log"
    android_target = proof_root / "android-ui.xml"
    ios_route = proof_root / "ios-metro.stdout.log"
    android_route = proof_root / "android-metro.stdout.log"
    ios_artifact.write_bytes(_png_bytes())
    android_artifact.write_bytes(b"android proof")
    ios_target.write_text("visible target text: Fitness First", encoding="utf-8")
    android_target.write_text("<node text='Fitness First' />", encoding="utf-8")
    ios_route.write_text(
        "LOG  [APP-PERF-METRIC] ui.load | screen.load /Marketplace | 180ms | ok",
        encoding="utf-8",
    )
    android_route.write_text(
        "LOG  [APP-PERF-METRIC] ui.load | screen.load /Marketplace | 190ms | ok",
        encoding="utf-8",
    )
    (proof_root / "monica-proof-manifest.json").write_text(
        json.dumps(
            {
                "run_id": "run-123",
                "proof_artifacts": [
                    str(ios_artifact),
                    str(android_artifact),
                    str(ios_target),
                    str(android_target),
                    str(ios_route),
                    str(android_route),
                ],
                "proof_target": {
                    "deep_link": "elixir-card://marketplace/offer/fitness-first",
                    "expected_text": "Fitness First",
                },
                "linear_identifier": "MOB-42",
                "linear_url": "https://linear.app/acme/issue/MOB-42",
                "branch_name": "monica/MOB-42-checkout-crash",
                "worktree": str(tmp_path),
                "base_ref": "origin/main",
                "base_commit": "abc1234",
                "setup_commands": ["npm run monica:seed-auth"],
                "commands": ["npm run monica:proof"],
                "required_env_keys": ["MONICA_TEST_LOGIN_TOKEN"],
                "platforms": ["ios", "android"],
            }
        ),
        encoding="utf-8",
    )

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Marketplace.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="proof manifest target route does not match"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace("/tmp/monica-proof", str(proof_root)),
        )

    assert calls == []


def test_publisher_rejects_manifest_that_omits_body_visual_artifacts_before_running_commands(tmp_path):
    calls = []
    proof_root = tmp_path / "proof"
    proof_root.mkdir()
    ios_artifact = proof_root / "ios-pdp-fixed.png"
    android_artifact = proof_root / "android-pdp-fixed.mp4"
    (proof_root / "monica-proof-manifest.json").write_text(
        json.dumps({"proof_artifacts": [str(ios_artifact)]}),
        encoding="utf-8",
    )
    ios_artifact.write_bytes(_png_bytes())
    android_artifact.write_bytes(b"android proof")

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="proof manifest artifacts"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace("/tmp/monica-proof", str(proof_root)),
        )

    assert calls == []


def test_publisher_rejects_manifest_target_mismatch_before_running_commands(tmp_path):
    calls = []
    proof_root = tmp_path / "proof"
    proof_root.mkdir()
    ios_artifact = proof_root / "ios-pdp-fixed.png"
    android_artifact = proof_root / "android-pdp-fixed.mp4"
    (proof_root / "monica-proof-manifest.json").write_text(
        json.dumps(
            {
                "proof_artifacts": [str(ios_artifact), str(android_artifact)],
                "proof_target": {
                    "deep_link": "elixir-card://marketplace/offer/other-offer",
                    "expected_text": "Other Offer",
                },
            }
        ),
        encoding="utf-8",
    )
    ios_artifact.write_bytes(_png_bytes())
    android_artifact.write_bytes(b"android proof")

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="proof manifest target"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace("/tmp/monica-proof", str(proof_root)),
        )

    assert calls == []


def test_publisher_rejects_manifest_base_mismatch_before_running_commands(tmp_path):
    calls = []
    proof_root = tmp_path / "proof"
    proof_root.mkdir()
    ios_artifact = proof_root / "ios-pdp-fixed.png"
    android_artifact = proof_root / "android-pdp-fixed.mp4"
    ios_route_log = proof_root / "ios-metro.stdout.log"
    android_route_log = proof_root / "android-metro.stdout.log"
    (proof_root / "monica-proof-manifest.json").write_text(
        json.dumps(
            {
                "proof_artifacts": [
                    str(ios_artifact),
                    str(android_artifact),
                    str(ios_route_log),
                    str(android_route_log),
                ],
                "proof_target": {
                    "deep_link": "elixir-card://marketplace/offer/fitness-first",
                    "expected_text": "Fitness First",
                },
                "base_ref": "origin/dev",
                "base_commit": "def4567",
            }
        ),
        encoding="utf-8",
    )
    ios_artifact.write_bytes(_png_bytes())
    android_artifact.write_bytes(b"android proof")
    ios_route_log.write_text(
        "LOG  [APP-PERF-METRIC] ui.load | screen.load /MarketplacePdp | 180ms | ok",
        encoding="utf-8",
    )
    android_route_log.write_text(
        "LOG  [APP-PERF-METRIC] ui.load | screen.load /MarketplacePdp | 190ms | ok",
        encoding="utf-8",
    )

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="proof manifest base"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace("/tmp/monica-proof", str(proof_root)),
        )

    assert calls == []


def test_publisher_rejects_manifest_linear_mismatch_before_running_commands(tmp_path):
    calls = []
    manifest_path = Path("/tmp/monica-proof/monica-proof-manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["linear_url"] = "https://linear.app/acme/issue/MOB-999"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="proof manifest Linear issue"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY,
        )

    assert calls == []


def test_publisher_rejects_manifest_branch_mismatch_before_running_commands(tmp_path):
    calls = []
    manifest_path = Path("/tmp/monica-proof/monica-proof-manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["branch_name"] = "monica/MOB-999-stale-proof"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="proof manifest branch"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY,
        )

    assert calls == []


def test_publisher_rejects_manifest_worktree_mismatch_before_running_commands(tmp_path):
    calls = []
    manifest_path = Path("/tmp/monica-proof/monica-proof-manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["worktree"] = str(tmp_path / "stale-worktree")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="proof manifest worktree"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY,
        )

    assert calls == []


def test_publisher_rejects_manifest_missing_worktree_before_running_commands(tmp_path):
    calls = []
    manifest_path = Path("/tmp/monica-proof/monica-proof-manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("worktree", None)
    manifest.pop("worktree_path", None)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="proof manifest worktree"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY,
        )

    assert calls == []


def test_publisher_rejects_manifest_missing_run_id_before_running_commands(tmp_path):
    calls = []
    manifest_path = Path("/tmp/monica-proof/monica-proof-manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("run_id", None)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="proof manifest run id"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY,
        )

    assert calls == []


def test_publisher_rejects_manifest_setup_command_mismatch_before_running_commands(tmp_path):
    calls = []
    proof_root = tmp_path / "proof"
    proof_root.mkdir()
    ios_artifact = proof_root / "ios-pdp-fixed.png"
    android_artifact = proof_root / "android-pdp-fixed.mp4"
    (proof_root / "monica-proof-manifest.json").write_text(
        json.dumps(
            {
                "proof_artifacts": [str(ios_artifact), str(android_artifact)],
                "proof_target": {
                    "deep_link": "elixir-card://marketplace/offer/fitness-first",
                    "expected_text": "Fitness First",
                },
                "base_ref": "origin/main",
                "base_commit": "abc1234",
                "setup_commands": ["npm run monica:seed-other-auth"],
            }
        ),
        encoding="utf-8",
    )
    ios_artifact.write_bytes(_png_bytes())
    android_artifact.write_bytes(b"android proof")

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="proof manifest setup commands"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace("/tmp/monica-proof", str(proof_root)),
        )

    assert calls == []


def test_publisher_rejects_manifest_proof_command_mismatch_before_running_commands(tmp_path):
    calls = []
    proof_root = tmp_path / "proof"
    proof_root.mkdir()
    ios_artifact = proof_root / "ios-pdp-fixed.png"
    android_artifact = proof_root / "android-pdp-fixed.mp4"
    (proof_root / "monica-proof-manifest.json").write_text(
        json.dumps(
            {
                "proof_artifacts": [str(ios_artifact), str(android_artifact)],
                "proof_target": {
                    "deep_link": "elixir-card://marketplace/offer/fitness-first",
                    "expected_text": "Fitness First",
                },
                "base_ref": "origin/main",
                "base_commit": "abc1234",
                "setup_commands": ["npm run monica:seed-auth"],
                "commands": ["npm run monica:proof:other"],
            }
        ),
        encoding="utf-8",
    )
    ios_artifact.write_bytes(_png_bytes())
    android_artifact.write_bytes(b"android proof")

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="proof manifest proof commands"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace("/tmp/monica-proof", str(proof_root)),
        )

    assert calls == []


def test_publisher_rejects_manifest_required_env_key_mismatch_before_running_commands(tmp_path):
    calls = []
    proof_root = tmp_path / "proof"
    proof_root.mkdir()
    ios_artifact = proof_root / "ios-pdp-fixed.png"
    android_artifact = proof_root / "android-pdp-fixed.mp4"
    (proof_root / "monica-proof-manifest.json").write_text(
        json.dumps(
            {
                "proof_artifacts": [str(ios_artifact), str(android_artifact)],
                "proof_target": {
                    "deep_link": "elixir-card://marketplace/offer/fitness-first",
                    "expected_text": "Fitness First",
                },
                "base_ref": "origin/main",
                "base_commit": "abc1234",
                "setup_commands": ["npm run monica:seed-auth"],
                "commands": ["npm run monica:proof"],
                "required_env_keys": ["MONICA_STALE_LOGIN_TOKEN"],
            }
        ),
        encoding="utf-8",
    )
    ios_artifact.write_bytes(_png_bytes())
    android_artifact.write_bytes(b"android proof")

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="proof manifest required env keys"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace("/tmp/monica-proof", str(proof_root)),
        )

    assert calls == []


def test_publisher_rejects_invalid_manifest_required_env_key_before_running_commands(tmp_path):
    calls = []
    manifest_path = Path("/tmp/monica-proof/monica-proof-manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["required_env_keys"] = [
        "MONICA_TEST_LOGIN_TOKEN",
        "MONICA_TEST_LOGIN_TOKEN=secret",
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="proof manifest required env keys"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY,
        )

    assert calls == []


def test_publisher_rejects_manifest_missing_android_platform_before_running_commands(tmp_path):
    calls = []
    proof_root = tmp_path / "proof"
    proof_root.mkdir()
    ios_artifact = proof_root / "ios-pdp-fixed.png"
    android_artifact = proof_root / "android-pdp-fixed.mp4"
    (proof_root / "monica-proof-manifest.json").write_text(
        json.dumps(
            {
                "proof_artifacts": [str(ios_artifact), str(android_artifact)],
                "proof_target": {
                    "deep_link": "elixir-card://marketplace/offer/fitness-first",
                    "expected_text": "Fitness First",
                },
                "base_ref": "origin/main",
                "base_commit": "abc1234",
                "setup_commands": ["npm run monica:seed-auth"],
                "commands": ["npm run monica:proof"],
                "required_env_keys": ["MONICA_TEST_LOGIN_TOKEN"],
                "platforms": ["ios"],
            }
        ),
        encoding="utf-8",
    )
    ios_artifact.write_bytes(_png_bytes())
    android_artifact.write_bytes(b"android proof")

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="proof manifest platforms"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace("/tmp/monica-proof", str(proof_root)),
        )

    assert calls == []


def test_publisher_rejects_manifest_missing_target_text_evidence_before_running_commands(tmp_path):
    calls = []
    proof_root = tmp_path / "proof"
    proof_root.mkdir()
    ios_artifact = proof_root / "ios-pdp-fixed.png"
    android_artifact = proof_root / "android-pdp-fixed.mp4"
    ios_route_log = proof_root / "ios-metro.stdout.log"
    android_route_log = proof_root / "android-metro.stdout.log"
    (proof_root / "monica-proof-manifest.json").write_text(
        json.dumps(
            {
                "proof_artifacts": [
                    str(ios_artifact),
                    str(android_artifact),
                    str(ios_route_log),
                    str(android_route_log),
                ],
                "proof_target": {
                    "deep_link": "elixir-card://marketplace/offer/fitness-first",
                    "expected_text": "Fitness First",
                },
                "base_ref": "origin/main",
                "base_commit": "abc1234",
                "setup_commands": ["npm run monica:seed-auth"],
                "commands": ["npm run monica:proof"],
                "required_env_keys": ["MONICA_TEST_LOGIN_TOKEN"],
                "platforms": ["ios", "android"],
            }
        ),
        encoding="utf-8",
    )
    ios_artifact.write_bytes(_png_bytes())
    android_artifact.write_bytes(b"android proof")
    ios_route_log.write_text(
        "LOG  [APP-PERF-METRIC] ui.load | screen.load /MarketplacePdp | 180ms | ok",
        encoding="utf-8",
    )
    android_route_log.write_text(
        "LOG  [APP-PERF-METRIC] ui.load | screen.load /MarketplacePdp | 190ms | ok",
        encoding="utf-8",
    )

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="proof manifest target text evidence"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace("/tmp/monica-proof", str(proof_root)),
        )

    assert calls == []


def test_publisher_rejects_manifest_target_text_evidence_outside_proof_dir_before_running_commands(tmp_path):
    calls = []
    proof_root = tmp_path / "proof"
    stale_root = tmp_path / "stale-proof"
    proof_root.mkdir()
    stale_root.mkdir()
    ios_artifact = proof_root / "ios-pdp-fixed.png"
    android_artifact = proof_root / "android-pdp-fixed.mp4"
    ios_text_artifact = stale_root / "ios-target.log"
    android_text_artifact = stale_root / "android-ui.xml"
    (proof_root / "monica-proof-manifest.json").write_text(
        json.dumps(
            {
                "proof_artifacts": [
                    str(ios_artifact),
                    str(android_artifact),
                    str(ios_text_artifact),
                    str(android_text_artifact),
                ],
                "proof_target": {
                    "deep_link": "elixir-card://marketplace/offer/fitness-first",
                    "expected_text": "Fitness First",
                },
                "base_ref": "origin/main",
                "base_commit": "abc1234",
                "setup_commands": ["npm run monica:seed-auth"],
                "commands": ["npm run monica:proof"],
                "required_env_keys": ["MONICA_TEST_LOGIN_TOKEN"],
                "platforms": ["ios", "android"],
            }
        ),
        encoding="utf-8",
    )
    ios_artifact.write_bytes(_png_bytes())
    android_artifact.write_bytes(b"android proof")
    ios_text_artifact.write_text("visible target text: Fitness First", encoding="utf-8")
    android_text_artifact.write_text("<node text='Fitness First' />", encoding="utf-8")

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="proof manifest artifacts"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace("/tmp/monica-proof", str(proof_root)),
        )

    assert calls == []


def test_publisher_rejects_manifest_visual_artifacts_outside_proof_dir_before_running_commands(tmp_path):
    calls = []
    proof_root = tmp_path / "proof"
    stale_root = tmp_path / "stale-proof"
    proof_root.mkdir()
    stale_root.mkdir()
    manifest_path = proof_root / "monica-proof-manifest.json"
    ios_artifact = stale_root / "ios-pdp-fixed.png"
    android_artifact = stale_root / "android-pdp-fixed.mp4"
    ios_text_artifact = proof_root / "ios-target.log"
    android_text_artifact = proof_root / "android-ui.xml"
    manifest_path.write_text(
        json.dumps(
            {
                "proof_artifacts": [
                    str(ios_artifact),
                    str(android_artifact),
                    str(ios_text_artifact),
                    str(android_text_artifact),
                ],
                "proof_target": {
                    "deep_link": "elixir-card://marketplace/offer/fitness-first",
                    "expected_text": "Fitness First",
                },
                "base_ref": "origin/main",
                "base_commit": "abc1234",
                "setup_commands": ["npm run monica:seed-auth"],
                "commands": ["npm run monica:proof"],
                "required_env_keys": ["MONICA_TEST_LOGIN_TOKEN"],
                "platforms": ["ios", "android"],
            }
        ),
        encoding="utf-8",
    )
    ios_artifact.write_bytes(_png_bytes())
    android_artifact.write_bytes(b"android proof")
    ios_text_artifact.write_text("visible target text: Fitness First", encoding="utf-8")
    android_text_artifact.write_text("<node text='Fitness First' />", encoding="utf-8")

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="proof manifest artifacts"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY
            .replace("/tmp/monica-proof/monica-proof-manifest.json", str(manifest_path))
            .replace("/tmp/monica-proof/ios-pdp-fixed.png", str(ios_artifact))
            .replace("/tmp/monica-proof/android-pdp-fixed.mp4", str(android_artifact)),
        )

    assert calls == []


def test_publisher_rejects_extra_body_artifacts_outside_manifest_dir_before_running_commands(tmp_path):
    calls = []
    stale_root = tmp_path / "stale-proof"
    stale_root.mkdir()
    stale_ios_target = stale_root / "ios-target.log"
    stale_android_target = stale_root / "android-ui.xml"
    stale_ios_target.write_text("visible target text: Fitness First", encoding="utf-8")
    stale_android_target.write_text("<node text='Fitness First' />", encoding="utf-8")

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)
    body = VALID_PR_BODY.replace(
        "/tmp/monica-proof/android-pdp-fixed.mp4",
        (
            "/tmp/monica-proof/android-pdp-fixed.mp4, "
            f"{stale_ios_target}, {stale_android_target}"
        ),
    )

    with pytest.raises(DraftPrPublisherError, match="proof manifest directory"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=body,
        )

    assert calls == []


def test_publisher_accepts_all_runner_visual_proof_suffixes(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    url = publisher.publish(
        worktree=_mark_git_worktree(tmp_path),
        branch_name="monica/MOB-42-checkout-crash",
        base_branch="main",
        title="[MOB-42] Fix Android checkout crash",
        body=VALID_PR_BODY.replace("ios-pdp-fixed.png", "ios-pdp-fixed.heic").replace(
            "android-pdp-fixed.mp4",
            "android-pdp-fixed.webm",
        ),
    )

    assert url == "https://github.com/acme/mobile/pull/123"


def test_publisher_requires_distinct_local_visual_artifacts_per_platform(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="distinct local iOS and Android"):
        publisher.publish(
            worktree=tmp_path,
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace(
                "/tmp/monica-proof/ios-pdp-fixed.png, /tmp/monica-proof/android-pdp-fixed.mp4",
                "/tmp/monica-proof/ios-android-proof.png",
            ),
        )

    assert calls == []


def test_publisher_requires_proof_setup_and_capture_commands_before_running_commands(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="proof setup and capture commands"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body="""## Links
- Linear: https://linear.app/acme/issue/MOB-42
- Slack: https://example.slack.com/archives/C_MOBILE/p1710000000000200
- Base: origin/main @ abc1234

## Verification
Verification passed.

```
npm test
ok
```

## Proof
Target: elixir-card://marketplace/offer/fitness-first
Expected text: Fitness First
- iOS: https://slack.example/files/ios-pdp-fixed.png (Slack upload: F-ios-pdp-fixed.png, ios-pdp-fixed.png)
- Android: https://slack.example/files/android-pdp-fixed.mp4 (Slack upload: F-android-pdp-fixed.mp4, android-pdp-fixed.mp4)
Local artifacts (debug): /tmp/monica-proof/monica-proof-manifest.json, /tmp/monica-proof/ios-pdp-fixed.png, /tmp/monica-proof/android-pdp-fixed.mp4
""",
        )

    assert calls == []


def test_publisher_requires_actual_proof_commands_under_command_labels(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="proof setup and capture commands"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body="""## Links
- Linear: https://linear.app/acme/issue/MOB-42
- Slack: https://example.slack.com/archives/C_MOBILE/p1710000000000200
- Base: origin/main @ abc1234

## Verification
Verification passed.

```
npm test
ok
```

## Proof
Setup commands:
Target: elixir-card://marketplace/offer/fitness-first
Proof commands:
Expected text: Fitness First
- iOS: https://slack.example/files/ios-pdp-fixed.png (Slack upload: F-ios-pdp-fixed.png, ios-pdp-fixed.png)
- Android: https://slack.example/files/android-pdp-fixed.mp4 (Slack upload: F-android-pdp-fixed.mp4, android-pdp-fixed.mp4)
Local artifacts (debug): /tmp/monica-proof/monica-proof-manifest.json, /tmp/monica-proof/ios-pdp-fixed.png, /tmp/monica-proof/android-pdp-fixed.mp4
""",
        )

    assert calls == []


def test_publisher_rejects_placeholder_proof_setup_command_before_running_commands(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="proof setup and capture commands"):
        publisher.publish(
            worktree=tmp_path,
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace(
                "- npm run monica:seed-auth",
                "- unavailable",
            ),
        )

    assert calls == []


@pytest.mark.parametrize(
    "body",
    (
        VALID_PR_BODY.replace(
            "- npm run monica:seed-auth",
            "- <auth/session seed command>",
        ),
        VALID_PR_BODY.replace(
            "- npm run monica:proof",
            "- <simulator proof command>",
        ),
    ),
)
def test_publisher_rejects_angle_placeholder_proof_commands_before_running_commands(
    tmp_path,
    body,
):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="placeholder proof"):
        publisher.publish(
            worktree=tmp_path,
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=body,
        )

    assert calls == []


def test_publisher_rejects_embedded_placeholder_proof_commands_before_running_commands(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="placeholder proof"):
        publisher.publish(
            worktree=tmp_path,
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace(
                "- npm run monica:seed-auth",
                "- npm run monica:seed -- '<auth/session seed command>'",
            ).replace(
                "- npm run monica:proof",
                "- sh -c '<simulator proof command>'",
            ),
        )

    assert calls == []


def test_publisher_rejects_noop_proof_commands_before_running_commands(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="proof setup and capture commands"):
        publisher.publish(
            worktree=tmp_path,
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace(
                "- npm run monica:seed-auth",
                "- true",
            ).replace(
                "- npm run monica:proof",
                "- exit 0",
            ),
        )

    assert calls == []


def test_publisher_rejects_inline_secret_proof_commands_before_running_commands(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="inline secret env"):
        publisher.publish(
            worktree=tmp_path,
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace(
                "- npm run monica:seed-auth",
                "- MONICA_TEST_LOGIN_TOKEN=secret npm run monica:seed-auth",
            ).replace(
                "- npm run monica:proof",
                "- MONICA_TEST_LOGIN_OTP=123456 npm run monica:proof",
            ),
        )

    assert calls == []


@pytest.mark.parametrize(
    ("old_command", "new_command"),
    (
        (
            "- npm run monica:seed-auth",
            "- npm run monica:seed-auth -- --token profile-secret",
        ),
        (
            "- npm run monica:proof",
            "- npm run monica:proof -- --token=profile-secret",
        ),
    ),
)
def test_publisher_rejects_literal_required_env_values_in_proof_commands_before_running_commands(
    tmp_path, monkeypatch, old_command, new_command
):
    monkeypatch.setenv("MONICA_TEST_LOGIN_TOKEN", "profile-secret")
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="literal required env"):
        publisher.publish(
            worktree=tmp_path,
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace(old_command, new_command),
        )

    assert calls == []


def test_publisher_requires_required_env_key_names_before_running_commands(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="required env key names"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace("Required env keys: MONICA_TEST_LOGIN_TOKEN\n", ""),
        )

    assert calls == []


def test_publisher_rejects_inline_secret_required_env_key_values_before_running_commands(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Marketplace.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="required env key names"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace(
                "Proof commands:",
                "Required env keys: MONICA_TEST_LOGIN_TOKEN=secret\nProof commands:",
            ),
        )

    assert calls == []


def test_publisher_rejects_bulleted_required_env_key_values_before_running_commands(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Marketplace.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="MONICA_TEST_LOGIN_OTP"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace(
                "Proof commands:",
                "Required env keys:\n- MONICA_TEST_LOGIN_OTP=123456\nProof commands:",
            ),
        )

    assert calls == []


def test_publisher_rejects_builtin_context_required_env_key_names_before_running_commands(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Marketplace.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="built-in Monica proof context"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace(
                "Required env keys: MONICA_TEST_LOGIN_TOKEN",
                "Required env keys: MONICA_WORKTREE",
            ),
        )

    assert calls == []


def test_publisher_requires_exact_proof_target_before_running_commands(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="proof target"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body="""## Links
- Linear: https://linear.app/acme/issue/MOB-42
- Slack: https://example.slack.com/archives/C_MOBILE/p1710000000000200

## Verification
Verification passed.

```
npm test
ok
```

## Proof
- iOS: https://slack.example/files/ios-pdp-fixed.png (Slack upload: F-ios-pdp-fixed.png, ios-pdp-fixed.png)
- Android: https://slack.example/files/android-pdp-fixed.mp4 (Slack upload: F-android-pdp-fixed.mp4, android-pdp-fixed.mp4)
""",
        )

    assert calls == []


@pytest.mark.parametrize(
    "expected_text",
    ("unavailable", "text visible on the fixed screen", "<text visible on the fixed screen>"),
)
def test_publisher_rejects_placeholder_expected_text_before_running_commands(
    tmp_path,
    expected_text,
):
    calls = []
    proof_dir = Path("/tmp/monica-proof")
    manifest_path = proof_dir / "monica-proof-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["proof_target"]["expected_text"] = expected_text
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (proof_dir / "ios-target.log").write_text(
        f"Monica iOS proof target text observed.\nexpected_text={expected_text}\n",
        encoding="utf-8",
    )
    (proof_dir / "android-ui.xml").write_text(
        f"<hierarchy><node text='{expected_text}' /></hierarchy>\n",
        encoding="utf-8",
    )

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="proof target"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace(
                "Expected text: Fitness First",
                f"Expected text: {expected_text}",
            ),
        )

    assert calls == []


def test_publisher_rejects_generic_expected_text_before_running_commands(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="proof target"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace(
                "Expected text: Fitness First",
                "Expected text: Marketplace",
            ),
        )

    assert calls == []


def test_publisher_rejects_route_container_expected_text_before_running_commands(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="proof target"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace(
                "Expected text: Fitness First",
                "Expected text: Offer",
            ),
        )

    assert calls == []


def test_publisher_rejects_generic_home_proof_target_before_running_commands(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="generic proof target"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace(
                "Target: elixir-card://marketplace/offer/fitness-first",
                "Target: elixir-card://home",
            ),
        )

    assert calls == []


def test_publisher_rejects_generic_marketplace_proof_target_before_running_commands(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="generic proof target"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace(
                "Target: elixir-card://marketplace/offer/fitness-first",
                "Target: elixir-card://marketplace",
            ),
        )

    assert calls == []


def test_publisher_rejects_generic_offer_route_target_before_running_commands(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="generic proof target"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace(
                "Target: elixir-card://marketplace/offer/fitness-first",
                "Target: elixir-card://marketplace/offer",
            ),
        )

    assert calls == []


def test_publisher_rejects_expo_dev_client_proof_target_before_running_commands(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="Expo Dev Client proof target"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace(
                "Target: elixir-card://marketplace/offer/fitness-first",
                "Target: elixir-card://expo-development-client/?url=http%3A%2F%2F127.0.0.1%3A8081",
            ),
        )

    assert calls == []


def test_publisher_rejects_expo_runtime_proof_target_before_running_commands(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="Expo runtime proof target"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace(
                "Target: elixir-card://marketplace/offer/fitness-first",
                "Target: exp://127.0.0.1:8081/--/marketplace/offer/fitness-first",
            ),
        )

    assert calls == []


@pytest.mark.parametrize(
    "target",
    (
        "http://127.0.0.1:8081/marketplace/offer/fitness-first",
        "http://proof.local:8081/marketplace/offer/fitness-first",
        "http://monica-proof/marketplace/offer/fitness-first",
    ),
)
def test_publisher_rejects_local_http_proof_target_before_running_commands(tmp_path, target):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="local proof target"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace(
                "Target: elixir-card://marketplace/offer/fitness-first",
                f"Target: {target}",
            ),
        )

    assert calls == []


def test_publisher_requires_base_commit_before_running_commands(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="base commit"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body="""## Links
- Linear: https://linear.app/acme/issue/MOB-42
- Slack: https://example.slack.com/archives/C_MOBILE/p1710000000000200

## Verification
Verification passed.

```
npm test
ok
```

## Proof
Target: elixir-card://marketplace/offer/fitness-first
Expected text: Fitness First
- iOS: https://slack.example/files/ios-pdp-fixed.png (Slack upload: F-ios-pdp-fixed.png, ios-pdp-fixed.png)
- Android: https://slack.example/files/android-pdp-fixed.mp4 (Slack upload: F-android-pdp-fixed.mp4, android-pdp-fixed.mp4)
Local artifacts (debug): /tmp/monica-proof/monica-proof-manifest.json, /tmp/monica-proof/ios-pdp-fixed.png, /tmp/monica-proof/android-pdp-fixed.mp4
""",
        )

    assert calls == []


def test_publisher_rejects_non_sha_base_commit_before_running_commands(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="base commit"):
        publisher.publish(
            worktree=tmp_path,
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY.replace(
                "- Base: origin/main @ abc1234",
                "- Base: origin/main @ abc123base",
            ),
        )

    assert calls == []


def test_publisher_accepts_fenced_verification_output(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    url = publisher.publish(
        worktree=_mark_git_worktree(tmp_path),
        branch_name="monica/MOB-42-checkout-crash",
        base_branch="main",
        title="[MOB-42] Fix Android checkout crash",
        body="""## Links
- Linear: https://linear.app/acme/issue/MOB-42
- Slack: https://example.slack.com/archives/C_MOBILE/p1710000000000200
- Base: origin/main @ abc1234

## Verification

```
$ npm test
ok
```

## Proof
Setup commands:
- npm run monica:seed-auth
Proof commands:
- npm run monica:proof
Required env keys: MONICA_TEST_LOGIN_TOKEN
Target: elixir-card://marketplace/offer/fitness-first
Expected text: Fitness First
- iOS: https://slack.example/files/ios-pdp-fixed.png (Slack upload: F-ios-pdp-fixed.png, ios-pdp-fixed.png)
- Android: https://slack.example/files/android-pdp-fixed.mp4 (Slack upload: F-android-pdp-fixed.mp4, android-pdp-fixed.mp4)
Local artifacts (debug): /tmp/monica-proof/monica-proof-manifest.json, /tmp/monica-proof/ios-pdp-fixed.png, /tmp/monica-proof/android-pdp-fixed.mp4
""",
    )

    assert url == "https://github.com/acme/mobile/pull/123"
    assert calls[0][0] == ["git", "branch", "--show-current"]


def test_publisher_rejects_unsafe_base_branch_before_running_commands(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="base_branch must be a safe git branch name"):
        publisher.publish(
            worktree=tmp_path,
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="../main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY,
        )

    assert calls == []


def test_publisher_rejects_unsafe_head_branch_before_running_commands(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="branch_name must be a safe git branch name"):
        publisher.publish(
            worktree=tmp_path,
            branch_name="../monica/MOB-42",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY,
        )

    assert calls == []


def test_publisher_requires_existing_worktree_before_running_commands(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="worktree does not exist"):
        publisher.publish(
            worktree=tmp_path / "missing-worktree",
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY,
        )

    assert calls == []


def test_publisher_requires_git_worktree_before_running_commands(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="worktree is not a git worktree"):
        publisher.publish(
            worktree=tmp_path,
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY,
        )

    assert calls == []


def test_publisher_refuses_worktree_on_unexpected_branch(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "main\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="worktree branch mismatch"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY,
        )

    assert calls == [(["git", "branch", "--show-current"], tmp_path)]


def test_publisher_uses_existing_committed_diff_without_empty_commit(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    url = publisher.publish(
        worktree=_mark_git_worktree(tmp_path),
        branch_name="monica/MOB-42-checkout-crash",
        base_branch="main",
        title="[MOB-42] Fix Android checkout crash",
        body=VALID_PR_BODY,
    )

    assert url == "https://github.com/acme/mobile/pull/123"
    assert not any("commit" in call[0] for call in calls)
    assert calls[3][0] == ["git", "push", "origin", "HEAD:monica/MOB-42-checkout-crash"]


def test_publisher_recovers_existing_pr_url_from_gh_create_error(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            raise DraftPrPublisherError(
                "command failed (1): gh pr create --draft\n"
                "stderr: a pull request already exists for this branch: "
                "https://github.com/acme/mobile/pull/456"
            )
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        if args[:3] == ["gh", "pr", "edit"]:
            return ""
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    url = publisher.publish(
        worktree=_mark_git_worktree(tmp_path),
        branch_name="monica/MOB-42-checkout-crash",
        base_branch="main",
        title="[MOB-42] Fix Android checkout crash",
        body=VALID_PR_BODY,
    )

    assert url == "https://github.com/acme/mobile/pull/456"
    assert calls[-3][0][:4] == ["gh", "pr", "create", "--draft"]
    assert calls[-2][0] == [
        "gh",
        "pr",
        "view",
        "https://github.com/acme/mobile/pull/456",
        "--json",
        "isDraft",
        "--jq",
        ".isDraft",
    ]
    assert calls[-1][0] == [
        "gh",
        "pr",
        "edit",
        "https://github.com/acme/mobile/pull/456",
        "--base",
        "main",
        "--title",
        "[MOB-42] Fix Android checkout crash",
        "--body",
        VALID_PR_BODY.strip(),
    ]


def test_publisher_strips_punctuation_from_recovered_pr_url(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            raise DraftPrPublisherError(
                "stderr: a pull request already exists for this branch "
                "(https://github.com/acme/mobile/pull/456)."
            )
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    url = publisher.publish(
        worktree=_mark_git_worktree(tmp_path),
        branch_name="monica/MOB-42-checkout-crash",
        base_branch="main",
        title="[MOB-42] Fix Android checkout crash",
        body=VALID_PR_BODY,
    )

    assert url == "https://github.com/acme/mobile/pull/456"


def test_publisher_refuses_recovered_pr_when_existing_pr_is_not_draft(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            raise DraftPrPublisherError(
                "stderr: a pull request already exists for this branch: "
                "https://github.com/acme/mobile/pull/456"
            )
        if args[:3] == ["gh", "pr", "view"]:
            return "false\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="existing PR is not draft"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY,
        )

    assert calls[-1][0] == [
        "gh",
        "pr",
        "view",
        "https://github.com/acme/mobile/pull/456",
        "--json",
        "isDraft",
        "--jq",
        ".isDraft",
    ]


def test_publisher_converts_recovered_existing_pr_to_draft_before_refresh(tmp_path):
    calls = []
    converted = False

    def run_command(args, cwd=None):
        nonlocal converted
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            raise DraftPrPublisherError(
                "stderr: a pull request already exists for this branch: "
                "https://github.com/acme/mobile/pull/456"
            )
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n" if converted else "false\n"
        if args[:3] == ["gh", "pr", "ready"]:
            converted = True
            return ""
        if args[:3] == ["gh", "pr", "edit"]:
            return ""
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    url = publisher.publish(
        worktree=_mark_git_worktree(tmp_path),
        branch_name="monica/MOB-42-checkout-crash",
        base_branch="main",
        title="[MOB-42] Fix Android checkout crash",
        body=VALID_PR_BODY,
    )

    assert url == "https://github.com/acme/mobile/pull/456"
    assert [
        "gh",
        "pr",
        "ready",
        "https://github.com/acme/mobile/pull/456",
        "--undo",
    ] in [call[0] for call in calls]
    assert calls[-1][0][:3] == ["gh", "pr", "edit"]


def test_publisher_refuses_created_pr_when_gh_reports_not_draft(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "false\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="created PR is not draft"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY,
        )

    assert calls[-1][0] == [
        "gh",
        "pr",
        "view",
        "https://github.com/acme/mobile/pull/123",
        "--json",
        "isDraft",
        "--jq",
        ".isDraft",
    ]


def test_publisher_converts_created_pr_to_draft_before_returning(tmp_path):
    calls = []
    converted = False

    def run_command(args, cwd=None):
        nonlocal converted
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://github.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n" if converted else "false\n"
        if args[:3] == ["gh", "pr", "ready"]:
            converted = True
            return ""
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    url = publisher.publish(
        worktree=_mark_git_worktree(tmp_path),
        branch_name="monica/MOB-42-checkout-crash",
        base_branch="main",
        title="[MOB-42] Fix Android checkout crash",
        body=VALID_PR_BODY,
    )

    assert url == "https://github.com/acme/mobile/pull/123"
    assert calls[-2][0] == [
        "gh",
        "pr",
        "ready",
        "https://github.com/acme/mobile/pull/123",
        "--undo",
    ]
    assert calls[-1][0] == [
        "gh",
        "pr",
        "view",
        "https://github.com/acme/mobile/pull/123",
        "--json",
        "isDraft",
        "--jq",
        ".isDraft",
    ]


def test_publisher_refuses_non_github_pr_url_from_gh_create(tmp_path):
    calls = []

    def run_command(args, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "branch", "--show-current"]:
            return "monica/MOB-42-checkout-crash\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "src/Checkout.tsx\n"
        if args[:4] == ["gh", "pr", "create", "--draft"]:
            return "https://example.com/acme/mobile/pull/123\n"
        if args[:3] == ["gh", "pr", "view"]:
            return "true\n"
        return ""

    publisher = DraftPrPublisher(run_command=run_command)

    with pytest.raises(DraftPrPublisherError, match="gh did not return a draft PR URL"):
        publisher.publish(
            worktree=_mark_git_worktree(tmp_path),
            branch_name="monica/MOB-42-checkout-crash",
            base_branch="main",
            title="[MOB-42] Fix Android checkout crash",
            body=VALID_PR_BODY,
        )

    assert not any(call[0][:3] == ["gh", "pr", "view"] for call in calls)


def test_publisher_missing_executable_raises_typed_error(tmp_path, monkeypatch):
    def missing_run(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("gh")

    monkeypatch.setattr(subprocess, "run", missing_run)
    publisher = DraftPrPublisher()

    with pytest.raises(DraftPrPublisherError, match="executable not found: gh"):
        publisher._default_run(["gh", "pr", "create"], tmp_path)


def test_publisher_nonzero_exit_includes_command_context(tmp_path, monkeypatch):
    def failed_run(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["gh", "pr", "create"],
            returncode=1,
            stdout="stdout detail",
            stderr="stderr detail",
        )

    monkeypatch.setattr(subprocess, "run", failed_run)
    publisher = DraftPrPublisher()

    with pytest.raises(DraftPrPublisherError) as exc_info:
        publisher._default_run(["gh", "pr", "create"], tmp_path)

    message = str(exc_info.value)
    assert "command failed (1): gh pr create" in message
    assert f"cwd: {tmp_path}" in message
    assert "stdout: stdout detail" in message
    assert "stderr: stderr detail" in message
