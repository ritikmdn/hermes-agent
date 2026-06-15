from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass

import pytest

from plugins.mobile_bug_agent.config import MonicaConfig, ProofConfig, RepoConfig, RuntimeConfig
from plugins.mobile_bug_agent.proof import ProofRunner


@pytest.fixture(autouse=True)
def _proof_auth_env(monkeypatch):
    monkeypatch.setenv("MONICA_TEST_LOGIN_TOKEN", "test-login-token")


@dataclass(frozen=True)
class FakeRun:
    id: str = "run-123"
    linear_identifier: str = "ENG-123"
    linear_url: str = ""
    branch_name: str = "monica/ENG-123-proof"
    base_branch: str = "origin/dev"
    base_commit: str = "abc1234"


def _mark_git_worktree(path):
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").write_text("gitdir: /tmp/fake-worktree-git-dir", encoding="utf-8")
    return path


def _png_bytes() -> bytes:
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAFElEQVR4nGNgYGD4//8/w38GEAMAIewE/ITr/YQAAAAASUVORK5CYII="
    )


def _config(tmp_path, **proof_overrides):
    proof_values = {
        "enabled": True,
        "required_for_done": True,
        "commands": ("capture-proof",),
        "platform_order": ("ios",),
        "artifact_dir": "proof",
    }
    proof_values.update(proof_overrides)
    return MonicaConfig(
        proof=ProofConfig(**proof_values),
        runtime=RuntimeConfig(home_subdir=str(tmp_path / "monica-runtime")),
    )


def test_proof_runner_blocks_when_commands_are_empty(tmp_path):
    runner = ProofRunner(config=_config(tmp_path, commands=()))

    result = runner.run(run=FakeRun(), worktree=_mark_git_worktree(tmp_path / "worktree"))

    assert result.passed is False
    assert result.summary == "Proof blocked: proof.commands is empty."
    assert result.artifacts == ()


def test_proof_runner_blocks_approved_pr_when_setup_commands_are_empty(tmp_path):
    calls = []

    def run(command, _cwd, _timeout, _env):
        calls.append(command)
        proof_dir = tmp_path / "monica-runtime" / "proof" / "run-123"
        (proof_dir / "ios-screenshot.png").write_text("ios image bytes", encoding="utf-8")
        (proof_dir / "android-screenshot.png").write_text("android image bytes", encoding="utf-8")
        return 0, "captured", ""

    runner = ProofRunner(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev"),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                commands=("capture-proof",),
                platform_order=("ios",),
                artifact_dir="proof",
            ),
            runtime=RuntimeConfig(home_subdir=str(tmp_path / "monica-runtime")),
        ),
        run_command=run,
    )

    result = runner.run(
        run=FakeRun(),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Fitness First",
        },
    )

    assert result.passed is False
    assert result.summary == "Proof blocked: proof.setup_commands is empty."
    assert result.artifacts == ()
    assert result.to_dict()["setup_commands"] == []
    assert result.to_dict()["commands"] == ["capture-proof"]
    assert calls == []


def test_proof_runner_creates_fresh_proof_dir_before_setup_commands(tmp_path):
    calls: list[tuple[str, bool]] = []

    def run(command, _cwd, _timeout, env):
        proof_dir = tmp_path / "monica-runtime" / "proof" / "run-123"
        calls.append((command, proof_dir.is_dir()))
        assert env["MONICA_PROOF_DIR"] == str(proof_dir)
        if command == "capture-proof":
            (proof_dir / "ios-screenshot.png").write_bytes(_png_bytes())
            (proof_dir / "android-screenshot.png").write_bytes(_png_bytes())
            (proof_dir / "ios-ui.xml").write_text("<node text='Fitness First' />", encoding="utf-8")
            (proof_dir / "android-ui.xml").write_text("<node text='Fitness First' />", encoding="utf-8")
            (proof_dir / "ios-metro.stdout.log").write_text(
                "LOG  [APP-PERF-METRIC] ui.load | screen.load /MarketplacePdp | 180ms | ok",
                encoding="utf-8",
            )
            (proof_dir / "android-metro.stdout.log").write_text(
                "LOG  [APP-PERF-METRIC] ui.load | screen.load /MarketplacePdp | 190ms | ok",
                encoding="utf-8",
            )
        return 0, "ok", ""

    runner = ProofRunner(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev"),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                setup_commands=("seed-test-auth",),
                commands=("capture-proof",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
                platform_order=("ios", "android"),
                artifact_dir="proof",
            ),
            runtime=RuntimeConfig(home_subdir=str(tmp_path / "monica-runtime")),
        ),
        run_command=run,
    )

    result = runner.run(
        run=FakeRun(),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Fitness First",
        },
    )

    assert result.passed is True
    assert calls == [("seed-test-auth", True), ("capture-proof", True)]


def test_proof_runner_blocks_approved_pr_with_placeholder_setup_command(tmp_path):
    calls = []

    runner = ProofRunner(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev"),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                setup_commands=("<auth/session seed command>",),
                commands=("capture-proof",),
                platform_order=("ios", "android"),
                artifact_dir="proof",
            ),
            runtime=RuntimeConfig(home_subdir=str(tmp_path / "monica-runtime")),
        ),
        run_command=lambda *args: calls.append(args) or (0, "captured", ""),
    )

    result = runner.run(
        run=FakeRun(),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Fitness First",
        },
    )

    assert result.passed is False
    assert result.summary == "Proof blocked: proof.setup_commands contains a placeholder."
    assert "<auth/session seed command>" in result.output
    assert result.artifacts == ()
    assert calls == []


def test_proof_runner_blocks_approved_pr_with_placeholder_proof_command(tmp_path):
    calls = []

    runner = ProofRunner(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev"),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                setup_commands=("seed-test-auth",),
                commands=("<simulator proof command>",),
                platform_order=("ios", "android"),
                artifact_dir="proof",
            ),
            runtime=RuntimeConfig(home_subdir=str(tmp_path / "monica-runtime")),
        ),
        run_command=lambda *args: calls.append(args) or (0, "captured", ""),
    )

    result = runner.run(
        run=FakeRun(),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Fitness First",
        },
    )

    assert result.passed is False
    assert result.summary == "Proof blocked: proof.commands contains a placeholder."
    assert "<simulator proof command>" in result.output
    assert result.artifacts == ()
    assert calls == []


def test_proof_runner_blocks_approved_pr_with_only_noop_setup_commands(tmp_path):
    calls = []

    runner = ProofRunner(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev"),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                setup_commands=("true", "exit 0"),
                commands=("capture-proof",),
                platform_order=("ios", "android"),
                artifact_dir="proof",
            ),
            runtime=RuntimeConfig(home_subdir=str(tmp_path / "monica-runtime")),
        ),
        run_command=lambda *args: calls.append(args) or (0, "captured", ""),
    )

    result = runner.run(
        run=FakeRun(),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Fitness First",
        },
    )

    assert result.passed is False
    assert result.summary == "Proof blocked: proof.setup_commands contains only no-op commands."
    assert result.artifacts == ()
    assert result.to_dict()["setup_commands"] == ["true", "exit 0"]
    assert result.to_dict()["commands"] == ["capture-proof"]
    assert calls == []


def test_proof_runner_blocks_approved_pr_with_only_noop_proof_commands(tmp_path):
    calls = []

    runner = ProofRunner(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev"),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                setup_commands=("seed-test-auth",),
                commands=("true", "exit 0"),
                platform_order=("ios", "android"),
                artifact_dir="proof",
            ),
            runtime=RuntimeConfig(home_subdir=str(tmp_path / "monica-runtime")),
        ),
        run_command=lambda *args: calls.append(args) or (0, "captured", ""),
    )

    result = runner.run(
        run=FakeRun(),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Fitness First",
        },
    )

    assert result.passed is False
    assert result.summary == "Proof blocked: proof.commands contains only no-op commands."
    assert result.artifacts == ()
    assert result.to_dict()["setup_commands"] == ["seed-test-auth"]
    assert result.to_dict()["commands"] == ["true", "exit 0"]
    assert calls == []


def test_proof_runner_blocks_approved_pr_with_inline_secret_setup_command(tmp_path):
    calls = []

    runner = ProofRunner(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev"),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                setup_commands=("MONICA_TEST_LOGIN_TOKEN=secret seed-test-auth",),
                commands=("capture-proof",),
                platform_order=("ios", "android"),
                artifact_dir="proof",
            ),
            runtime=RuntimeConfig(home_subdir=str(tmp_path / "monica-runtime")),
        ),
        run_command=lambda *args: calls.append(args) or (0, "captured", ""),
    )

    result = runner.run(
        run=FakeRun(),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Fitness First",
        },
    )

    assert result.passed is False
    assert result.summary == (
        "Proof blocked: proof.setup_commands must not inline secret env assignment(s)."
    )
    assert "MONICA_TEST_LOGIN_TOKEN" in result.output
    assert result.artifacts == ()
    assert calls == []


def test_proof_runner_blocks_approved_pr_with_inline_secret_proof_command(tmp_path):
    calls = []

    runner = ProofRunner(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev"),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                setup_commands=("seed-test-auth",),
                commands=("MONICA_TEST_LOGIN_OTP=123456 capture-proof",),
                platform_order=("ios", "android"),
                artifact_dir="proof",
            ),
            runtime=RuntimeConfig(home_subdir=str(tmp_path / "monica-runtime")),
        ),
        run_command=lambda *args: calls.append(args) or (0, "captured", ""),
    )

    result = runner.run(
        run=FakeRun(),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Fitness First",
        },
    )

    assert result.passed is False
    assert result.summary == (
        "Proof blocked: proof.commands must not inline secret env assignment(s)."
    )
    assert "MONICA_TEST_LOGIN_OTP" in result.output
    assert result.artifacts == ()
    assert calls == []


def test_proof_runner_blocks_approved_pr_without_both_required_platforms(tmp_path):
    calls = []

    runner = ProofRunner(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev"),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                setup_commands=("seed-test-auth",),
                commands=("capture-proof",),
                platform_order=("ios",),
                artifact_dir="proof",
            ),
            runtime=RuntimeConfig(home_subdir=str(tmp_path / "monica-runtime")),
        ),
        run_command=lambda *args: calls.append(args) or (0, "captured", ""),
    )

    result = runner.run(
        run=FakeRun(),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Fitness First",
        },
    )

    assert result.passed is False
    assert result.summary == (
        "Proof blocked: proof.platform_order must include both ios and android in approved_pr mode; "
        "missing android."
    )
    assert result.artifacts == ()
    assert result.to_dict()["setup_commands"] == ["seed-test-auth"]
    assert result.to_dict()["commands"] == ["capture-proof"]
    assert result.platforms == ("ios",)
    assert calls == []


def test_proof_runner_blocks_approved_pr_when_base_commit_is_not_sha(tmp_path):
    calls = []

    runner = ProofRunner(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev"),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                setup_commands=("seed-test-auth",),
                commands=("capture-proof",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
                platform_order=("ios", "android"),
                artifact_dir="proof",
            ),
            runtime=RuntimeConfig(home_subdir=str(tmp_path / "monica-runtime")),
        ),
        run_command=lambda *args: calls.append(args) or (0, "", ""),
    )

    result = runner.run(
        run=FakeRun(base_commit="abc123base"),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
    )

    assert result.passed is False
    assert result.summary == "Proof blocked: run base commit metadata is invalid."
    assert result.artifacts == ()
    assert result.to_dict()["setup_commands"] == ["seed-test-auth"]
    assert result.to_dict()["commands"] == ["capture-proof"]
    assert calls == []


def test_proof_runner_accepts_origin_prefixed_default_branch(tmp_path):
    proof_dir = tmp_path / "monica-runtime" / "proof" / "run-123"

    def run(command, _cwd, _timeout, _env):
            if command == "capture-proof":
                (proof_dir / "ios-screenshot.png").write_bytes(_png_bytes())
                (proof_dir / "android-screenshot.png").write_bytes(_png_bytes())
                (proof_dir / "ios-target.log").write_text("Fitness First", encoding="utf-8")
                (proof_dir / "android-ui.xml").write_text("Fitness First", encoding="utf-8")
                (proof_dir / "ios-metro.stdout.log").write_text(
                    "LOG  [APP-PERF-METRIC] ui.load | screen.load /MarketplacePdp | 180ms | ok",
                    encoding="utf-8",
                )
                (proof_dir / "android-metro.stdout.log").write_text(
                    "LOG  [APP-PERF-METRIC] ui.load | screen.load /MarketplacePdp | 190ms | ok",
                    encoding="utf-8",
                )
            return 0, "captured Fitness First", ""

    runner = ProofRunner(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="origin/dev"),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                setup_commands=("seed-test-auth",),
                commands=("capture-proof",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
                platform_order=("ios", "android"),
                artifact_dir="proof",
            ),
            runtime=RuntimeConfig(home_subdir=str(tmp_path / "monica-runtime")),
        ),
        run_command=run,
    )

    result = runner.run(
        run=FakeRun(base_branch="origin/dev", base_commit="abc1234"),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Fitness First",
        },
    )

    assert result.passed is True
    assert result.summary == "Proof captured."
    manifest_path = proof_dir / "monica-proof-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["base_ref"] == "origin/dev"


def test_proof_runner_blocks_approved_pr_when_target_deep_link_is_missing(tmp_path):
    calls = []
    runner = ProofRunner(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev"),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                setup_commands=("seed-test-auth",),
                commands=("capture-proof",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
                platform_order=("ios", "android"),
                artifact_dir="proof",
            ),
            runtime=RuntimeConfig(home_subdir=str(tmp_path / "monica-runtime")),
        ),
        run_command=lambda *args: calls.append(args) or (0, "", ""),
    )

    result = runner.run(run=FakeRun(), worktree=_mark_git_worktree(tmp_path / "worktree"))

    assert result.passed is False
    assert result.summary == "Proof blocked: proof target deep link is required."
    assert result.artifacts == ()
    assert result.to_dict()["setup_commands"] == ["seed-test-auth"]
    assert result.to_dict()["commands"] == ["capture-proof"]
    assert calls == []


def test_proof_runner_blocks_approved_pr_when_target_expected_text_is_missing(tmp_path):
    calls = []
    runner = ProofRunner(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev"),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                setup_commands=("seed-test-auth",),
                commands=("capture-proof",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
                platform_order=("ios", "android"),
                artifact_dir="proof",
            ),
            runtime=RuntimeConfig(home_subdir=str(tmp_path / "monica-runtime")),
        ),
        run_command=lambda *args: calls.append(args) or (0, "", ""),
    )

    result = runner.run(
        run=FakeRun(),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
        proof_target={"deep_link": "elixir-card://marketplace/offer/fitness-first"},
    )

    assert result.passed is False
    assert result.summary == "Proof blocked: proof target expected text is required."
    assert result.artifacts == ()
    assert result.to_dict()["proof_target"] == {
        "deep_link": "elixir-card://marketplace/offer/fitness-first"
    }
    assert result.to_dict()["setup_commands"] == ["seed-test-auth"]
    assert result.to_dict()["commands"] == ["capture-proof"]
    assert calls == []


@pytest.mark.parametrize(
    "expected_text",
    ("text visible on the fixed screen", "<text visible on the fixed screen>"),
)
def test_proof_runner_blocks_approved_pr_when_target_expected_text_is_placeholder_instruction(
    tmp_path,
    expected_text,
):
    calls = []
    runner = ProofRunner(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev"),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                setup_commands=("seed-test-auth",),
                commands=("capture-proof",),
                platform_order=("ios", "android"),
                artifact_dir="proof",
            ),
            runtime=RuntimeConfig(home_subdir=str(tmp_path / "monica-runtime")),
        ),
        run_command=lambda *args: calls.append(args) or (0, "", ""),
    )

    result = runner.run(
        run=FakeRun(),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": expected_text,
        },
    )

    assert result.passed is False
    assert result.summary == "Proof blocked: proof target expected text is required."
    assert result.artifacts == ()
    assert result.to_dict()["setup_commands"] == ["seed-test-auth"]
    assert result.to_dict()["commands"] == ["capture-proof"]
    assert calls == []


def test_proof_runner_blocks_approved_pr_when_target_expected_text_is_generic(tmp_path):
    calls = []
    runner = ProofRunner(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev"),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                setup_commands=("seed-test-auth",),
                commands=("capture-proof",),
                platform_order=("ios", "android"),
                artifact_dir="proof",
            ),
            runtime=RuntimeConfig(home_subdir=str(tmp_path / "monica-runtime")),
        ),
        run_command=lambda *args: calls.append(args) or (0, "", ""),
    )

    result = runner.run(
        run=FakeRun(),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Marketplace",
        },
    )

    assert result.passed is False
    assert result.summary == "Proof blocked: proof target expected text is too generic."
    assert result.artifacts == ()
    assert result.to_dict()["proof_target"] == {
        "deep_link": "elixir-card://marketplace/offer/fitness-first",
        "expected_text": "Marketplace",
    }
    assert result.to_dict()["setup_commands"] == ["seed-test-auth"]
    assert result.to_dict()["commands"] == ["capture-proof"]
    assert calls == []


def test_proof_runner_blocks_approved_pr_when_target_expected_text_is_route_container(tmp_path):
    calls = []
    runner = ProofRunner(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev"),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                setup_commands=("seed-test-auth",),
                commands=("capture-proof",),
                platform_order=("ios", "android"),
                artifact_dir="proof",
            ),
            runtime=RuntimeConfig(home_subdir=str(tmp_path / "monica-runtime")),
        ),
        run_command=lambda *args: calls.append(args) or (0, "", ""),
    )

    result = runner.run(
        run=FakeRun(),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Offer",
        },
    )

    assert result.passed is False
    assert result.summary == "Proof blocked: proof target expected text is too generic."
    assert result.artifacts == ()
    assert result.to_dict()["proof_target"] == {
        "deep_link": "elixir-card://marketplace/offer/fitness-first",
        "expected_text": "Offer",
    }
    assert result.to_dict()["setup_commands"] == ["seed-test-auth"]
    assert result.to_dict()["commands"] == ["capture-proof"]
    assert calls == []


def test_proof_runner_blocks_approved_pr_when_target_is_generic_home(tmp_path):
    calls = []
    runner = ProofRunner(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev"),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                setup_commands=("seed-test-auth",),
                commands=("capture-proof",),
                platform_order=("ios", "android"),
                artifact_dir="proof",
            ),
            runtime=RuntimeConfig(home_subdir=str(tmp_path / "monica-runtime")),
        ),
        run_command=lambda *args: calls.append(args) or (0, "", ""),
    )

    result = runner.run(
        run=FakeRun(),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
        proof_target={"deep_link": "elixir-card://home", "expected_text": "Home"},
    )

    assert result.passed is False
    assert result.summary == "Proof blocked: generic proof target is not enough."
    assert result.artifacts == ()
    assert result.to_dict()["proof_target"] == {
        "deep_link": "elixir-card://home",
        "expected_text": "Home",
    }
    assert result.to_dict()["setup_commands"] == ["seed-test-auth"]
    assert result.to_dict()["commands"] == ["capture-proof"]
    assert calls == []


def test_proof_runner_blocks_approved_pr_when_target_is_generic_marketplace(tmp_path):
    calls = []
    runner = ProofRunner(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev"),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                setup_commands=("seed-test-auth",),
                commands=("capture-proof",),
                platform_order=("ios", "android"),
                artifact_dir="proof",
            ),
            runtime=RuntimeConfig(home_subdir=str(tmp_path / "monica-runtime")),
        ),
        run_command=lambda *args: calls.append(args) or (0, "", ""),
    )

    result = runner.run(
        run=FakeRun(),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
        proof_target={
            "deep_link": "elixir-card://marketplace",
            "expected_text": "Fitness First",
        },
    )

    assert result.passed is False
    assert result.summary == "Proof blocked: generic proof target is not enough."
    assert result.artifacts == ()
    assert result.to_dict()["proof_target"] == {
        "deep_link": "elixir-card://marketplace",
        "expected_text": "Fitness First",
    }
    assert result.to_dict()["setup_commands"] == ["seed-test-auth"]
    assert result.to_dict()["commands"] == ["capture-proof"]
    assert calls == []


def test_proof_runner_blocks_approved_pr_when_target_is_generic_offer_route(tmp_path):
    calls = []
    runner = ProofRunner(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev"),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                setup_commands=("seed-test-auth",),
                commands=("capture-proof",),
                platform_order=("ios", "android"),
                artifact_dir="proof",
            ),
            runtime=RuntimeConfig(home_subdir=str(tmp_path / "monica-runtime")),
        ),
        run_command=lambda *args: calls.append(args) or (0, "", ""),
    )

    result = runner.run(
        run=FakeRun(),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
        proof_target={
            "deep_link": "elixir-card://marketplace/offer",
            "expected_text": "Fitness First",
        },
    )

    assert result.passed is False
    assert result.summary == "Proof blocked: generic proof target is not enough."
    assert result.artifacts == ()
    assert result.to_dict()["proof_target"] == {
        "deep_link": "elixir-card://marketplace/offer",
        "expected_text": "Fitness First",
    }
    assert result.to_dict()["setup_commands"] == ["seed-test-auth"]
    assert result.to_dict()["commands"] == ["capture-proof"]
    assert calls == []


def test_proof_runner_blocks_approved_pr_when_target_is_expo_runtime_url(tmp_path):
    calls = []
    runner = ProofRunner(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev"),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                setup_commands=("seed-test-auth",),
                commands=("capture-proof",),
                platform_order=("ios", "android"),
                artifact_dir="proof",
            ),
            runtime=RuntimeConfig(home_subdir=str(tmp_path / "monica-runtime")),
        ),
        run_command=lambda *args: calls.append(args) or (0, "", ""),
    )

    result = runner.run(
        run=FakeRun(),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
        proof_target={
            "deep_link": "exp://127.0.0.1:8081/--/marketplace/offer/fitness-first",
            "expected_text": "Fitness First",
        },
    )

    assert result.passed is False
    assert result.summary == "Proof blocked: Expo runtime proof target is not enough."
    assert result.artifacts == ()
    assert result.to_dict()["proof_target"] == {
        "deep_link": "exp://127.0.0.1:8081/--/marketplace/offer/fitness-first",
        "expected_text": "Fitness First",
    }
    assert result.to_dict()["setup_commands"] == ["seed-test-auth"]
    assert result.to_dict()["commands"] == ["capture-proof"]
    assert calls == []


@pytest.mark.parametrize(
    "target",
    (
        "http://192.168.1.25:8081/marketplace/offer/fitness-first",
        "http://proof.local:8081/marketplace/offer/fitness-first",
        "http://monica-proof/marketplace/offer/fitness-first",
    ),
)
def test_proof_runner_blocks_approved_pr_when_target_is_private_network_url(tmp_path, target):
    calls = []
    runner = ProofRunner(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev"),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                setup_commands=("seed-test-auth",),
                commands=("capture-proof",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
                platform_order=("ios", "android"),
                artifact_dir="proof",
            ),
            runtime=RuntimeConfig(home_subdir=str(tmp_path / "monica-runtime")),
        ),
        run_command=lambda *args: calls.append(args) or (0, "", ""),
    )

    result = runner.run(
        run=FakeRun(),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
        proof_target={
            "deep_link": target,
            "expected_text": "Fitness First",
        },
    )

    assert result.passed is False
    assert result.summary == "Proof blocked: local proof target is not enough."
    assert result.artifacts == ()
    assert result.to_dict()["proof_target"] == {
        "deep_link": target,
        "expected_text": "Fitness First",
    }
    assert result.to_dict()["setup_commands"] == ["seed-test-auth"]
    assert result.to_dict()["commands"] == ["capture-proof"]
    assert calls == []


def test_proof_runner_preserves_target_and_commands_when_worktree_preflight_blocks(tmp_path):
    calls = []
    runner = ProofRunner(
        config=_config(
            tmp_path,
            setup_commands=("seed-test-auth",),
            commands=("capture-proof",),
            platform_order=("ios", "android"),
        ),
        run_command=lambda *args: calls.append(args) or (0, "", ""),
    )

    result = runner.run(
        run=FakeRun(),
        worktree=tmp_path / "missing-worktree",
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Fitness First",
        },
    )

    assert result.passed is False
    assert result.summary == "Proof blocked: worktree does not exist."
    assert result.artifacts == ()
    assert result.to_dict()["proof_target"] == {
        "deep_link": "elixir-card://marketplace/offer/fitness-first",
        "expected_text": "Fitness First",
    }
    assert result.to_dict()["setup_commands"] == ["seed-test-auth"]
    assert result.to_dict()["commands"] == ["capture-proof"]
    assert calls == []


def test_proof_runner_passes_when_command_creates_artifact(tmp_path):
    calls = []

    def run(command, cwd, timeout, env):
        calls.append(
            (
                command,
                cwd,
                timeout,
                env["MONICA_PROOF_DIR"],
                env["MONICA_DEV_CLIENT_SCHEME"],
                env["MONICA_IOS_SIMULATOR_UDID"],
                env["MONICA_IOS_BUNDLE_ID"],
                env["MONICA_ANDROID_SERIAL"],
                env["MONICA_ANDROID_AVD"],
                env["MONICA_ANDROID_PACKAGE"],
            )
        )
        proof_path = tmp_path / "monica-runtime" / "proof" / "run-123" / "ios-screenshot.png"
        proof_path.write_text("image bytes", encoding="utf-8")
        return 0, "captured", ""

    runner = ProofRunner(
        config=_config(
            tmp_path,
            timeout_minutes=3,
            dev_client_scheme="elixir-card",
            ios_simulator_udid="IOS-DEVICE-UDID",
            ios_bundle_id="com.elixir.card",
            android_serial="emulator-5554",
            android_avd="MonicaPixel",
            android_package="com.joinelixir.elixirclub",
        ),
        run_command=run,
    )

    result = runner.run(run=FakeRun(), worktree=_mark_git_worktree(tmp_path / "worktree"))

    assert result.passed is True
    assert result.summary == "Proof captured."
    proof_dir = tmp_path / "monica-runtime" / "proof" / "run-123"
    manifest_path = proof_dir / "monica-proof-manifest.json"
    assert result.artifacts == (
        str(manifest_path),
        str(proof_dir / "ios-screenshot.png"),
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["run_id"] == "run-123"
    assert manifest["linear_identifier"] == "ENG-123"
    assert manifest["branch_name"] == "monica/ENG-123-proof"
    assert manifest["base_branch"] == "origin/dev"
    assert manifest["base_ref"] == "origin/dev"
    assert manifest["base_commit"] == "abc1234"
    assert manifest["worktree"] == str(tmp_path / "worktree")
    assert manifest["platforms"] == ["ios"]
    assert manifest["proof_artifacts"] == [str(proof_dir / "ios-screenshot.png")]
    assert manifest["setup_commands"] == []
    assert manifest["commands"] == ["capture-proof"]
    assert result.to_dict()["setup_commands"] == []
    assert result.to_dict()["commands"] == ["capture-proof"]
    assert calls == [
        (
            "capture-proof",
            tmp_path / "worktree",
            180,
            str(tmp_path / "monica-runtime" / "proof" / "run-123"),
            "elixir-card",
            "IOS-DEVICE-UDID",
            "com.elixir.card",
            "emulator-5554",
            "MonicaPixel",
            "com.joinelixir.elixirclub",
        )
    ]


def test_proof_runner_runs_setup_commands_before_capture(tmp_path):
    calls = []
    proof_dir = tmp_path / "monica-runtime" / "proof" / "run-123"

    def run(command, cwd, timeout, env):
        calls.append((command, cwd, timeout, env["MONICA_PROOF_DIR"], env["MONICA_DEEP_LINK"]))
        if command == "seed-test-auth":
            (proof_dir / "seeded-session.txt").write_text("seeded", encoding="utf-8")
            return 0, "seeded auth", ""
        assert (proof_dir / "seeded-session.txt").read_text(encoding="utf-8") == "seeded"
        (proof_dir / "ios-screenshot.png").write_text("image bytes", encoding="utf-8")
        return 0, "captured", ""

    runner = ProofRunner(
        config=_config(
            tmp_path,
            setup_commands=("seed-test-auth",),
            commands=("capture-proof",),
            deep_link="elixir-card://gymMembership/screens/GymPdpScreen?programSlug=fitness-first",
        ),
        run_command=run,
    )

    result = runner.run(run=FakeRun(), worktree=_mark_git_worktree(tmp_path / "worktree"))

    assert result.passed is True
    assert result.to_dict()["setup_commands"] == ["seed-test-auth"]
    assert result.to_dict()["commands"] == ["capture-proof"]
    manifest_path = proof_dir / "monica-proof-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["setup_commands"] == ["seed-test-auth"]
    assert manifest["commands"] == ["capture-proof"]
    assert calls == [
        (
            "seed-test-auth",
            tmp_path / "worktree",
            600,
            str(proof_dir),
            "elixir-card://gymMembership/screens/GymPdpScreen?programSlug=fitness-first",
        ),
        (
            "capture-proof",
            tmp_path / "worktree",
            600,
            str(proof_dir),
            "elixir-card://gymMembership/screens/GymPdpScreen?programSlug=fitness-first",
        ),
    ]


def test_proof_runner_passes_profile_env_credentials_to_setup_commands(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    (hermes_home / ".env").write_text(
        "MONICA_TEST_LOGIN_TOKEN=profile-secret\n"
        "MONICA_TEST_LOGIN_OTP=123456\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("MONICA_TEST_LOGIN_TOKEN", "stale-shell-secret")
    seen = {}
    proof_dir = tmp_path / "monica-runtime" / "proof" / "run-123"

    def run(command, _cwd, _timeout, env):
        seen[command] = {
            "token": env.get("MONICA_TEST_LOGIN_TOKEN"),
            "otp": env.get("MONICA_TEST_LOGIN_OTP"),
        }
        if command == "seed-test-auth":
            return 0, "seeded auth", ""
        (proof_dir / "ios-screenshot.png").write_text("image bytes", encoding="utf-8")
        return 0, "captured", ""

    runner = ProofRunner(
        config=_config(
            tmp_path,
            setup_commands=("seed-test-auth",),
            commands=("capture-proof",),
            required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
        ),
        run_command=run,
    )

    result = runner.run(run=FakeRun(), worktree=_mark_git_worktree(tmp_path / "worktree"))

    assert result.passed is True
    assert seen["seed-test-auth"] == {
        "token": "profile-secret",
        "otp": "123456",
    }
    assert seen["capture-proof"] == {
        "token": "profile-secret",
        "otp": "123456",
    }


def test_proof_runner_redacts_profile_env_values_from_command_output(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    (hermes_home / ".env").write_text(
        "MONICA_TEST_LOGIN_TOKEN=profile-secret\n"
        "MONICA_TEST_LOGIN_OTP=123456\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    proof_dir = tmp_path / "monica-runtime" / "proof" / "run-123"

    def run(command, _cwd, _timeout, _env):
        if command == "seed-test-auth":
            return 0, "seeded token profile-secret", "otp 123456"
        (proof_dir / "ios-screenshot.png").write_text("image bytes", encoding="utf-8")
        return 0, "captured with profile-secret", "confirmed otp 123456"

    runner = ProofRunner(
        config=_config(
            tmp_path,
            setup_commands=("seed-test-auth",),
            commands=("capture-proof",),
            required_env_keys=("MONICA_TEST_LOGIN_TOKEN", "MONICA_TEST_LOGIN_OTP"),
        ),
        run_command=run,
    )

    result = runner.run(run=FakeRun(), worktree=_mark_git_worktree(tmp_path / "worktree"))

    assert result.passed is True
    assert "profile-secret" not in result.output
    assert "123456" not in result.output
    assert "[redacted:MONICA_TEST_LOGIN_TOKEN]" in result.output
    assert "[redacted:MONICA_TEST_LOGIN_OTP]" in result.output


def test_proof_runner_redacts_short_required_env_values_from_command_output(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    (hermes_home / ".env").write_text(
        "MONICA_TEST_LOGIN_PIN=123\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    proof_dir = tmp_path / "monica-runtime" / "proof" / "run-123"

    def run(command, _cwd, _timeout, _env):
        if command == "seed-test-auth":
            return 0, "seeded pin 123", ""
        (proof_dir / "ios-screenshot.png").write_text("image bytes", encoding="utf-8")
        return 0, "captured with pin 123", ""

    runner = ProofRunner(
        config=_config(
            tmp_path,
            setup_commands=("seed-test-auth",),
            commands=("capture-proof",),
            required_env_keys=("MONICA_TEST_LOGIN_PIN",),
        ),
        run_command=run,
    )

    result = runner.run(run=FakeRun(), worktree=_mark_git_worktree(tmp_path / "worktree"))

    assert result.passed is True
    assert "123" not in result.output
    assert "[redacted:MONICA_TEST_LOGIN_PIN]" in result.output


@pytest.mark.parametrize(
    ("proof_overrides", "expected_summary"),
    (
        (
            {
                "setup_commands": ("seed-test-auth profile-secret",),
                "commands": ("capture-proof",),
            },
            "Proof blocked: proof.setup_commands must not include required environment values.",
        ),
        (
            {
                "setup_commands": ("seed-test-auth",),
                "commands": ("capture-proof --token=profile-secret",),
            },
            "Proof blocked: proof.commands must not include required environment values.",
        ),
    ),
)
def test_proof_runner_blocks_commands_that_embed_required_env_values(
    tmp_path,
    monkeypatch,
    proof_overrides,
    expected_summary,
):
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    (hermes_home / ".env").write_text(
        "MONICA_TEST_LOGIN_TOKEN=profile-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    calls = []

    def run(command, _cwd, _timeout, _env):
        calls.append(command)
        return 0, "", ""

    proof_values = {
        "enabled": True,
        "required_for_done": True,
        "platform_order": ("ios", "android"),
        "artifact_dir": "proof",
        "required_env_keys": ("MONICA_TEST_LOGIN_TOKEN",),
    }
    proof_values.update(proof_overrides)
    runner = ProofRunner(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev"),
            proof=ProofConfig(**proof_values),
            runtime=RuntimeConfig(home_subdir=str(tmp_path / "monica-runtime")),
        ),
        run_command=run,
    )

    result = runner.run(
        run=FakeRun(),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Fitness First",
        },
    )

    assert result.passed is False
    assert result.summary == expected_summary
    assert "profile-secret" not in result.output
    assert "[redacted:MONICA_TEST_LOGIN_TOKEN]" in result.output
    assert calls == []


def test_proof_runner_exports_required_env_keys_for_simulator_harness(tmp_path, monkeypatch):
    monkeypatch.setenv("MONICA_TEST_LOGIN_OTP", "123456")
    seen = {}
    proof_dir = tmp_path / "monica-runtime" / "proof" / "run-123"

    def run(command, _cwd, _timeout, env):
        seen[command] = env.get("MONICA_REQUIRED_ENV_KEYS")
        if command == "seed-test-auth":
            return 0, "seeded auth", ""
        (proof_dir / "ios-screenshot.png").write_text("image bytes", encoding="utf-8")
        return 0, "captured", ""

    runner = ProofRunner(
        config=_config(
            tmp_path,
            setup_commands=("seed-test-auth",),
            commands=("capture-proof",),
            required_env_keys=(
                " MONICA_TEST_LOGIN_TOKEN ",
                "MONICA_TEST_LOGIN_OTP",
                "MONICA_TEST_LOGIN_TOKEN",
                " ",
            ),
        ),
        run_command=run,
    )

    result = runner.run(run=FakeRun(), worktree=_mark_git_worktree(tmp_path / "worktree"))

    assert result.passed is True
    assert result.to_dict()["required_env_keys"] == [
        "MONICA_TEST_LOGIN_TOKEN",
        "MONICA_TEST_LOGIN_OTP",
    ]
    manifest_path = proof_dir / "monica-proof-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["required_env_keys"] == [
        "MONICA_TEST_LOGIN_TOKEN",
        "MONICA_TEST_LOGIN_OTP",
    ]
    assert seen == {
        "seed-test-auth": "MONICA_TEST_LOGIN_TOKEN,MONICA_TEST_LOGIN_OTP",
        "capture-proof": "MONICA_TEST_LOGIN_TOKEN,MONICA_TEST_LOGIN_OTP",
    }


def test_proof_runner_blocks_invalid_required_env_key_before_running_commands(tmp_path):
    calls = []
    runner = ProofRunner(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev"),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                setup_commands=("seed-test-auth",),
                commands=("capture-proof",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN=secret",),
                platform_order=("ios", "android"),
                artifact_dir="proof",
            ),
            runtime=RuntimeConfig(home_subdir=str(tmp_path / "monica-runtime")),
        ),
        run_command=lambda *args: calls.append(args) or (0, "", ""),
    )

    result = runner.run(run=FakeRun(), worktree=_mark_git_worktree(tmp_path / "worktree"))

    assert result.passed is False
    assert result.summary == "Proof blocked: proof.required_env_keys contains invalid key names."
    assert "MONICA_TEST_LOGIN_TOKEN" in result.output
    assert "secret" not in result.output
    assert calls == []


@pytest.mark.parametrize("required_env_key", ("MONICA_WORKTREE", "MONICA_PROOF_SCREEN"))
def test_proof_runner_blocks_builtin_context_required_env_key_before_running_commands(
    tmp_path,
    required_env_key,
):
    calls = []
    runner = ProofRunner(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev"),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                setup_commands=("seed-test-auth",),
                commands=("capture-proof",),
                required_env_keys=(required_env_key,),
                platform_order=("ios", "android"),
                artifact_dir="proof",
            ),
            runtime=RuntimeConfig(home_subdir=str(tmp_path / "monica-runtime")),
        ),
        run_command=lambda *args: calls.append(args) or (0, "", ""),
    )

    result = runner.run(
        run=FakeRun(),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Fitness First",
        },
    )

    assert result.passed is False
    assert result.summary == "Proof blocked: proof.required_env_keys contains invalid key names."
    assert required_env_key in result.output
    assert "built-in Monica proof context" in result.output
    assert calls == []


def test_proof_runner_blocks_approved_pr_when_required_env_keys_are_empty(tmp_path):
    calls = []
    runner = ProofRunner(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev"),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                setup_commands=("seed-test-auth",),
                commands=("capture-proof",),
                platform_order=("ios", "android"),
                artifact_dir="proof",
            ),
            runtime=RuntimeConfig(home_subdir=str(tmp_path / "monica-runtime")),
        ),
        run_command=lambda *args: calls.append(args) or (0, "", ""),
    )

    result = runner.run(
        run=FakeRun(),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Fitness First",
        },
    )

    assert result.passed is False
    assert result.summary == "Proof blocked: proof.required_env_keys is empty."
    assert result.output == "mobile_bug_agent.proof.required_env_keys is empty."
    assert calls == []


def test_proof_runner_blocks_missing_required_env_key_before_setup_commands(
    tmp_path,
    monkeypatch,
):
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("MONICA_TEST_LOGIN_TOKEN", raising=False)
    calls = []
    runner = ProofRunner(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev"),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                setup_commands=("seed-test-auth",),
                commands=("capture-proof",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
                platform_order=("ios", "android"),
                artifact_dir="proof",
            ),
            runtime=RuntimeConfig(home_subdir=str(tmp_path / "monica-runtime")),
        ),
        run_command=lambda *args: calls.append(args) or (0, "", ""),
    )

    result = runner.run(
        run=FakeRun(),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Fitness First",
        },
    )

    assert result.passed is False
    assert result.summary == "Proof blocked: required proof environment keys are missing."
    assert result.output == "Missing proof environment keys: MONICA_TEST_LOGIN_TOKEN"
    assert calls == []


def test_proof_runner_derives_linear_identifier_from_linear_url_for_env_and_manifest(
    tmp_path,
):
    captured_env = {}

    def run(_command, _cwd, _timeout, env):
        captured_env["MONICA_LINEAR_IDENTIFIER"] = env["MONICA_LINEAR_IDENTIFIER"]
        proof_dir = tmp_path / "monica-runtime" / "proof" / "run-123"
        (proof_dir / "ios-screenshot.png").write_text("image bytes", encoding="utf-8")
        return 0, "captured", ""

    runner = ProofRunner(config=_config(tmp_path), run_command=run)

    result = runner.run(
        run=FakeRun(
            linear_identifier="",
            linear_url="https://linear.app/acme/issue/MOB-123/fix-marketplace-copy",
        ),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
    )

    assert result.passed is True
    assert captured_env == {"MONICA_LINEAR_IDENTIFIER": "MOB-123"}
    manifest_path = tmp_path / "monica-runtime" / "proof" / "run-123" / "monica-proof-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["linear_identifier"] == "MOB-123"


def test_proof_runner_passes_run_and_base_context_to_setup_commands(tmp_path):
    seen = {}
    proof_dir = tmp_path / "monica-runtime" / "proof" / "run-123"

    def run(command, _cwd, _timeout, env):
        seen[command] = {
            "run_id": env.get("MONICA_RUN_ID"),
            "linear_identifier": env.get("MONICA_LINEAR_IDENTIFIER"),
            "linear_url": env.get("MONICA_LINEAR_URL"),
            "branch_name": env.get("MONICA_BRANCH_NAME"),
            "base_ref": env.get("MONICA_BASE_REF"),
            "base_commit": env.get("MONICA_BASE_COMMIT"),
        }
        if command == "seed-test-auth":
            return 0, "seeded auth", ""
        (proof_dir / "ios-screenshot.png").write_text("image bytes", encoding="utf-8")
        return 0, "captured", ""

    runner = ProofRunner(
        config=_config(
            tmp_path,
            setup_commands=("seed-test-auth",),
            commands=("capture-proof",),
            required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
        ),
        run_command=run,
    )

    result = runner.run(
        run=FakeRun(linear_url="https://linear.app/acme/issue/ENG-123"),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
    )

    expected = {
        "run_id": "run-123",
        "linear_identifier": "ENG-123",
        "linear_url": "https://linear.app/acme/issue/ENG-123",
        "branch_name": "monica/ENG-123-proof",
        "base_ref": "origin/dev",
        "base_commit": "abc1234",
    }
    assert result.passed is True
    assert seen["seed-test-auth"] == expected
    assert seen["capture-proof"] == expected


def test_proof_runner_records_linear_url_and_base_ref_context_in_manifest(tmp_path):
    @dataclass(frozen=True)
    class BaseRefRun:
        id: str = "run-456"
        linear_identifier: str = "MOB-456"
        linear_url: str = "https://linear.app/acme/issue/MOB-456/fix-pdp-copy"
        branch_name: str = "monica/MOB-456-pdp-copy"
        base_ref: str = "origin/dev"
        base_commit: str = "def4567"

    proof_dir = tmp_path / "monica-runtime" / "proof" / "run-456"

    def run(_command, _cwd, _timeout, _env):
        (proof_dir / "ios-screenshot.png").write_text("image bytes", encoding="utf-8")
        return 0, "captured", ""

    runner = ProofRunner(config=_config(tmp_path), run_command=run)

    result = runner.run(run=BaseRefRun(), worktree=_mark_git_worktree(tmp_path / "worktree"))

    assert result.passed is True
    manifest = json.loads((proof_dir / "monica-proof-manifest.json").read_text(encoding="utf-8"))
    assert manifest["linear_identifier"] == "MOB-456"
    assert manifest["linear_url"] == "https://linear.app/acme/issue/MOB-456/fix-pdp-copy"
    assert manifest["branch_name"] == "monica/MOB-456-pdp-copy"
    assert manifest["base_branch"] == "origin/dev"
    assert manifest["base_ref"] == "origin/dev"
    assert manifest["base_commit"] == "def4567"


def test_proof_runner_blocks_when_setup_command_fails(tmp_path):
    calls = []
    proof_dir = tmp_path / "monica-runtime" / "proof" / "run-123"

    def run(command, _cwd, _timeout, _env):
        calls.append(command)
        if command == "seed-test-auth":
            (proof_dir / "setup.log").write_text("otp bypass unavailable", encoding="utf-8")
            return 1, "", "missing MONICA_TEST_LOGIN_OTP"
        raise AssertionError("proof capture should not run after setup failure")

    runner = ProofRunner(
        config=_config(
            tmp_path,
            setup_commands=("seed-test-auth",),
            commands=("capture-proof",),
            required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
        ),
        run_command=run,
    )

    result = runner.run(run=FakeRun(), worktree=_mark_git_worktree(tmp_path / "worktree"))

    assert result.passed is False
    assert result.summary == "Proof blocked: proof setup failed: seed-test-auth"
    assert result.artifacts == (str(proof_dir / "setup.log"),)
    assert result.to_dict()["setup_commands"] == ["seed-test-auth"]
    assert result.to_dict()["commands"] == ["capture-proof"]
    assert result.to_dict()["required_env_keys"] == ["MONICA_TEST_LOGIN_TOKEN"]
    assert "missing MONICA_TEST_LOGIN_OTP" in result.output
    assert calls == ["seed-test-auth"]


def test_proof_runner_does_not_count_setup_artifacts_as_proof_command_evidence(tmp_path):
    calls = []
    proof_dir = tmp_path / "monica-runtime" / "proof" / "run-123"

    def run(command, _cwd, _timeout, _env):
        calls.append(command)
        if command == "seed-test-auth":
            (proof_dir / "ios-screenshot.png").write_text("setup ios image", encoding="utf-8")
            (proof_dir / "android-screenshot.png").write_text("setup android image", encoding="utf-8")
            return 0, "seeded auth", ""
        return 0, "proof command did not capture screenshots", ""

    runner = ProofRunner(
        config=_config(
            tmp_path,
            setup_commands=("seed-test-auth",),
            commands=("capture-proof",),
            platform_order=("ios", "android"),
        ),
        run_command=run,
    )

    result = runner.run(
        run=FakeRun(),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Fitness First",
        },
    )

    assert result.passed is False
    assert result.summary == "Proof blocked: proof commands produced no artifacts."
    assert result.artifacts == (
        str(proof_dir / "android-screenshot.png"),
        str(proof_dir / "ios-screenshot.png"),
    )
    assert result.to_dict()["proof_target"] == {
        "deep_link": "elixir-card://marketplace/offer/fitness-first",
        "expected_text": "Fitness First",
    }
    assert result.to_dict()["setup_commands"] == ["seed-test-auth"]
    assert result.to_dict()["commands"] == ["capture-proof"]
    assert calls == ["seed-test-auth", "capture-proof"]


def test_proof_runner_does_not_accept_touched_setup_artifacts_as_proof_evidence(tmp_path):
    calls = []
    proof_dir = tmp_path / "monica-runtime" / "proof" / "run-123"
    artifact_names = (
        "ios-screenshot.png",
        "android-screenshot.png",
        "ios-target.log",
        "android-ui.xml",
    )

    def run(command, _cwd, _timeout, _env):
        calls.append(command)
        artifacts = tuple(proof_dir / name for name in artifact_names)
        if command == "seed-test-auth":
            artifacts[0].write_text("ios image bytes", encoding="utf-8")
            artifacts[1].write_text("android image bytes", encoding="utf-8")
            artifacts[2].write_text("visible target text: Fitness First", encoding="utf-8")
            artifacts[3].write_text("<node text='Fitness First' />", encoding="utf-8")
            return 0, "seeded auth", ""
        for artifact in artifacts:
            future = artifact.stat().st_mtime + 5
            os.utime(artifact, (future, future))
        return 0, "touched stale setup artifacts", ""

    runner = ProofRunner(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev"),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                setup_commands=("seed-test-auth",),
                commands=("capture-proof",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
                platform_order=("ios", "android"),
                artifact_dir="proof",
            ),
            runtime=RuntimeConfig(home_subdir=str(tmp_path / "monica-runtime")),
        ),
        run_command=run,
    )

    result = runner.run(
        run=FakeRun(),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Fitness First",
        },
    )

    assert result.passed is False
    assert result.summary == "Proof blocked: proof commands produced no artifacts."
    assert set(result.artifacts) == {str(proof_dir / name) for name in artifact_names}
    assert calls == ["seed-test-auth", "capture-proof"]


def test_proof_runner_passes_target_deep_link_to_env_and_manifest(tmp_path):
    captured_env = {}

    def run(_command, _cwd, _timeout, env):
        captured_env["MONICA_DEEP_LINK"] = env["MONICA_DEEP_LINK"]
        captured_env["MONICA_PROOF_EXPECTED_TEXT"] = env["MONICA_PROOF_EXPECTED_TEXT"]
        captured_env["MONICA_PROOF_SCREEN"] = env["MONICA_PROOF_SCREEN"]
        proof_dir = tmp_path / "monica-runtime" / "proof" / "run-123"
        proof_path = proof_dir / "ios-screenshot.png"
        proof_path.write_text("image bytes", encoding="utf-8")
        (proof_dir / "ios-ui.xml").write_text("<node text='Fitness First' />", encoding="utf-8")
        return 0, "captured Fitness First", ""

    runner = ProofRunner(config=_config(tmp_path), run_command=run)

    result = runner.run(
        run=FakeRun(),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Fitness First",
            "screen": "/MarketplacePdp",
        },
    )

    assert result.passed is True
    assert captured_env == {
        "MONICA_DEEP_LINK": "elixir-card://marketplace/offer/fitness-first",
        "MONICA_PROOF_EXPECTED_TEXT": "Fitness First",
        "MONICA_PROOF_SCREEN": "/MarketplacePdp",
    }
    manifest_path = tmp_path / "monica-runtime" / "proof" / "run-123" / "monica-proof-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["proof_target"] == {
        "deep_link": "elixir-card://marketplace/offer/fitness-first",
        "expected_text": "Fitness First",
        "screen": "/MarketplacePdp",
    }
    assert result.to_dict()["proof_target"] == {
        "deep_link": "elixir-card://marketplace/offer/fitness-first",
        "expected_text": "Fitness First",
        "screen": "/MarketplacePdp",
    }


def test_proof_runner_blocks_when_expected_text_is_not_observed(tmp_path):
    def run(_command, _cwd, _timeout, _env):
        proof_dir = tmp_path / "monica-runtime" / "proof" / "run-123"
        (proof_dir / "ios-screenshot.png").write_text("image bytes", encoding="utf-8")
        (proof_dir / "ios-ui.xml").write_text("<node text='Other offer' />", encoding="utf-8")
        return 0, "captured target route", ""

    runner = ProofRunner(config=_config(tmp_path), run_command=run)

    result = runner.run(
        run=FakeRun(),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Fitness First",
        },
    )

    assert result.passed is False
    assert result.summary == "Proof blocked: expected target text was not observed: Fitness First"
    assert result.artifacts == (
        str(tmp_path / "monica-runtime" / "proof" / "run-123" / "ios-screenshot.png"),
        str(tmp_path / "monica-runtime" / "proof" / "run-123" / "ios-ui.xml"),
    )


def test_proof_runner_does_not_accept_stdout_as_target_text_proof(tmp_path):
    def run(_command, _cwd, _timeout, _env):
        proof_dir = tmp_path / "monica-runtime" / "proof" / "run-123"
        (proof_dir / "ios-screenshot.png").write_text("image bytes", encoding="utf-8")
        return 0, "captured Fitness First", ""

    runner = ProofRunner(config=_config(tmp_path), run_command=run)

    result = runner.run(
        run=FakeRun(),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Fitness First",
        },
    )

    assert result.passed is False
    assert result.summary == "Proof blocked: expected target text was not observed: Fitness First"
    assert result.artifacts == (
        str(tmp_path / "monica-runtime" / "proof" / "run-123" / "ios-screenshot.png"),
    )


def test_proof_runner_passes_when_expected_text_is_in_text_artifact(tmp_path):
    def run(_command, _cwd, _timeout, _env):
        proof_dir = tmp_path / "monica-runtime" / "proof" / "run-123"
        (proof_dir / "ios-screenshot.png").write_text("image bytes", encoding="utf-8")
        (proof_dir / "ios-ui.xml").write_text("<node text='Fitness First' />", encoding="utf-8")
        return 0, "captured target route", ""

    runner = ProofRunner(config=_config(tmp_path), run_command=run)

    result = runner.run(
        run=FakeRun(),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Fitness First",
        },
    )

    assert result.passed is True


def test_proof_runner_requires_expected_text_for_each_required_platform(tmp_path):
    def run(_command, _cwd, _timeout, _env):
        proof_dir = tmp_path / "monica-runtime" / "proof" / "run-123"
        (proof_dir / "ios-screenshot.png").write_text("ios image bytes", encoding="utf-8")
        (proof_dir / "android-screenshot.png").write_text("android image bytes", encoding="utf-8")
        (proof_dir / "android-ui.xml").write_text("<node text='Fitness First' />", encoding="utf-8")
        return 0, "captured both", ""

    runner = ProofRunner(
        config=_config(tmp_path, platform_order=("ios", "android")),
        run_command=run,
    )

    result = runner.run(
        run=FakeRun(),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Fitness First",
        },
    )

    assert result.passed is False
    assert result.summary == "Proof blocked: expected target text was not observed for: ios"


def test_proof_runner_requires_distinct_expected_text_artifacts_per_platform(tmp_path):
    def run(_command, _cwd, _timeout, _env):
        proof_dir = tmp_path / "monica-runtime" / "proof" / "run-123"
        (proof_dir / "ios-screenshot.png").write_text("ios image bytes", encoding="utf-8")
        (proof_dir / "android-screenshot.png").write_text("android image bytes", encoding="utf-8")
        (proof_dir / "ios-android-ui.xml").write_text(
            "<node text='Fitness First' />",
            encoding="utf-8",
        )
        return 0, "captured both", ""

    runner = ProofRunner(
        config=_config(tmp_path, platform_order=("ios", "android")),
        run_command=run,
    )

    result = runner.run(
        run=FakeRun(),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Fitness First",
        },
    )

    assert result.passed is False
    assert result.summary == "Proof blocked: expected target text was not observed for: android"


def test_proof_runner_passes_when_expected_text_is_seen_for_each_required_platform(tmp_path):
    def run(_command, _cwd, _timeout, _env):
        proof_dir = tmp_path / "monica-runtime" / "proof" / "run-123"
        (proof_dir / "ios-screenshot.png").write_text("ios image bytes", encoding="utf-8")
        (proof_dir / "android-screenshot.png").write_text("android image bytes", encoding="utf-8")
        (proof_dir / "ios-ui.log").write_text("screen text: Fitness First", encoding="utf-8")
        (proof_dir / "android-ui.xml").write_text("<node text='Fitness First' />", encoding="utf-8")
        return 0, "captured both", ""

    runner = ProofRunner(
        config=_config(tmp_path, platform_order=("ios", "android")),
        run_command=run,
    )

    result = runner.run(
        run=FakeRun(),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Fitness First",
        },
    )

    assert result.passed is True


def test_proof_runner_blocks_when_screenshots_are_still_on_splash_screen(tmp_path):
    proof_dir = tmp_path / "monica-runtime" / "proof" / "run-123"

    def run(command, _cwd, _timeout, _env):
        if command == "seed-test-auth":
            return 0, "seeded auth", ""
        (proof_dir / "ios-screenshot.png").write_bytes(_png_bytes())
        (proof_dir / "android-screenshot.png").write_bytes(_png_bytes())
        (proof_dir / "ios-target.log").write_text("visible target text: Fitness First", encoding="utf-8")
        (proof_dir / "android-ui.xml").write_text('<node text="Fitness First" />', encoding="utf-8")
        (proof_dir / "ios-metro.stdout.log").write_text(
            "LOG  [APP-PERF-METRIC] ui.load | screen.load /SplashScreen | 6ms | ok\n",
            encoding="utf-8",
        )
        (proof_dir / "android-metro.stdout.log").write_text(
            "LOG  [APP-PERF-METRIC] ui.load | screen.load /SplashScreen | 19ms | ok\n",
            encoding="utf-8",
        )
        return 0, "captured splash screens", ""

    runner = ProofRunner(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev"),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                setup_commands=("seed-test-auth",),
                commands=("capture-proof",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
                platform_order=("ios", "android"),
                artifact_dir="proof",
            ),
            runtime=RuntimeConfig(home_subdir=str(tmp_path / "monica-runtime")),
        ),
        run_command=run,
    )

    result = runner.run(
        run=FakeRun(),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Fitness First",
        },
    )

    assert result.passed is False
    assert result.summary == "Proof blocked: non-target app screen observed for: ios, android."
    assert result.artifacts == (
        str(proof_dir / "android-metro.stdout.log"),
        str(proof_dir / "android-screenshot.png"),
        str(proof_dir / "android-ui.xml"),
        str(proof_dir / "ios-metro.stdout.log"),
        str(proof_dir / "ios-screenshot.png"),
        str(proof_dir / "ios-target.log"),
    )


def test_proof_runner_blocks_short_splash_route_as_non_target_screen(tmp_path):
    proof_dir = tmp_path / "monica-runtime" / "proof" / "run-123"

    def run(command, _cwd, _timeout, _env):
        if command == "seed-test-auth":
            return 0, "seeded auth", ""
        (proof_dir / "ios-screenshot.png").write_bytes(_png_bytes())
        (proof_dir / "android-screenshot.png").write_bytes(_png_bytes())
        (proof_dir / "ios-target.log").write_text("visible target text: Fitness First", encoding="utf-8")
        (proof_dir / "android-ui.xml").write_text('<node text="Fitness First" />', encoding="utf-8")
        (proof_dir / "ios-metro.stdout.log").write_text(
            "LOG  [APP-PERF-METRIC] ui.load | screen.load /Splash | 6ms | ok\n",
            encoding="utf-8",
        )
        (proof_dir / "android-metro.stdout.log").write_text(
            "LOG  [APP-PERF-METRIC] ui.load | screen.load /Splash | 19ms | ok\n",
            encoding="utf-8",
        )
        return 0, "captured splash screens", ""

    runner = ProofRunner(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev"),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                setup_commands=("seed-test-auth",),
                commands=("capture-proof",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
                platform_order=("ios", "android"),
                artifact_dir="proof",
            ),
            runtime=RuntimeConfig(home_subdir=str(tmp_path / "monica-runtime")),
        ),
        run_command=run,
    )

    result = runner.run(
        run=FakeRun(),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Fitness First",
        },
    )

    assert result.passed is False
    assert result.summary == "Proof blocked: non-target app screen observed for: ios, android."


def test_proof_runner_blocks_json_screen_route_when_screenshots_are_still_on_splash_screen(tmp_path):
    proof_dir = tmp_path / "monica-runtime" / "proof" / "run-123"

    def run(command, _cwd, _timeout, _env):
        if command == "seed-test-auth":
            return 0, "seeded auth", ""
        (proof_dir / "ios-screenshot.png").write_bytes(_png_bytes())
        (proof_dir / "android-screenshot.png").write_bytes(_png_bytes())
        (proof_dir / "ios-target.log").write_text("visible target text: Fitness First", encoding="utf-8")
        (proof_dir / "android-ui.xml").write_text('<node text="Fitness First" />', encoding="utf-8")
        (proof_dir / "ios-metro.stdout.log").write_text(
            'LOG  [APP-PERF-METRIC] ui.load | ok {"screen": "/SplashScreen"}\n',
            encoding="utf-8",
        )
        (proof_dir / "android-metro.stdout.log").write_text(
            'LOG  [APP-PERF-METRIC] ui.load | ok {"screen": "/SplashScreen"}\n',
            encoding="utf-8",
        )
        return 0, "captured splash screens", ""

    runner = ProofRunner(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev"),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                setup_commands=("seed-test-auth",),
                commands=("capture-proof",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
                platform_order=("ios", "android"),
                artifact_dir="proof",
            ),
            runtime=RuntimeConfig(home_subdir=str(tmp_path / "monica-runtime")),
        ),
        run_command=run,
    )

    result = runner.run(
        run=FakeRun(),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Fitness First",
        },
    )

    assert result.passed is False
    assert result.summary == "Proof blocked: non-target app screen observed for: ios, android."


def test_proof_runner_blocks_approved_pr_when_target_route_is_not_observed(tmp_path):
    proof_dir = tmp_path / "monica-runtime" / "proof" / "run-123"

    def run(command, _cwd, _timeout, _env):
        if command == "seed-test-auth":
            return 0, "seeded auth", ""
        (proof_dir / "ios-screenshot.png").write_bytes(_png_bytes())
        (proof_dir / "android-screenshot.png").write_bytes(_png_bytes())
        (proof_dir / "ios-target.log").write_text("visible target text: Fitness First", encoding="utf-8")
        (proof_dir / "android-ui.xml").write_text('<node text="Fitness First" />', encoding="utf-8")
        return 0, "captured screenshots without route evidence", ""

    runner = ProofRunner(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev"),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                setup_commands=("seed-test-auth",),
                commands=("capture-proof",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
                platform_order=("ios", "android"),
                artifact_dir="proof",
            ),
            runtime=RuntimeConfig(home_subdir=str(tmp_path / "monica-runtime")),
        ),
        run_command=run,
    )

    result = runner.run(
        run=FakeRun(),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Fitness First",
        },
    )

    assert result.passed is False
    assert result.summary == "Proof blocked: target screen route was not observed for: ios, android."


def test_proof_runner_blocks_approved_pr_when_route_does_not_match_target(tmp_path):
    proof_dir = tmp_path / "monica-runtime" / "proof" / "run-123"

    def run(command, _cwd, _timeout, _env):
        if command == "seed-test-auth":
            return 0, "seeded auth", ""
        (proof_dir / "ios-screenshot.png").write_bytes(_png_bytes())
        (proof_dir / "android-screenshot.png").write_bytes(_png_bytes())
        (proof_dir / "ios-target.log").write_text("visible target text: Fitness First", encoding="utf-8")
        (proof_dir / "android-ui.xml").write_text('<node text="Fitness First" />', encoding="utf-8")
        (proof_dir / "ios-metro.stdout.log").write_text(
            "LOG  [APP-PERF-METRIC] ui.load | screen.load /Home | 6ms | ok\n",
            encoding="utf-8",
        )
        (proof_dir / "android-metro.stdout.log").write_text(
            "LOG  [APP-PERF-METRIC] ui.load | screen.load /Home | 19ms | ok\n",
            encoding="utf-8",
        )
        return 0, "captured home screens", ""

    runner = ProofRunner(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev"),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                setup_commands=("seed-test-auth",),
                commands=("capture-proof",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
                platform_order=("ios", "android"),
                artifact_dir="proof",
            ),
            runtime=RuntimeConfig(home_subdir=str(tmp_path / "monica-runtime")),
        ),
        run_command=run,
    )

    result = runner.run(
        run=FakeRun(),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Fitness First",
        },
    )

    assert result.passed is False
    assert result.summary == "Proof blocked: target screen route does not match proof target for: ios, android."


def test_proof_runner_blocks_approved_pr_when_route_is_only_generic_marketplace(tmp_path):
    proof_dir = tmp_path / "monica-runtime" / "proof" / "run-123"

    def run(command, _cwd, _timeout, _env):
        if command == "seed-test-auth":
            return 0, "seeded auth", ""
        (proof_dir / "ios-screenshot.png").write_bytes(_png_bytes())
        (proof_dir / "android-screenshot.png").write_bytes(_png_bytes())
        (proof_dir / "ios-target.log").write_text("visible target text: Fitness First", encoding="utf-8")
        (proof_dir / "android-ui.xml").write_text('<node text="Fitness First" />', encoding="utf-8")
        (proof_dir / "ios-metro.stdout.log").write_text(
            "LOG  [APP-PERF-METRIC] ui.load | screen.load /Marketplace | 6ms | ok\n",
            encoding="utf-8",
        )
        (proof_dir / "android-metro.stdout.log").write_text(
            "LOG  [APP-PERF-METRIC] ui.load | screen.load /Marketplace | 19ms | ok\n",
            encoding="utf-8",
        )
        return 0, "captured marketplace tab", ""

    runner = ProofRunner(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev"),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                setup_commands=("seed-test-auth",),
                commands=("capture-proof",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
                platform_order=("ios", "android"),
                artifact_dir="proof",
            ),
            runtime=RuntimeConfig(home_subdir=str(tmp_path / "monica-runtime")),
        ),
        run_command=run,
    )

    result = runner.run(
        run=FakeRun(),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Fitness First",
        },
    )

    assert result.passed is False
    assert result.summary == "Proof blocked: target screen route does not match proof target for: ios, android."


def test_proof_runner_blocks_when_auth_fallback_is_observed_in_artifacts(tmp_path):
    proof_dir = tmp_path / "monica-runtime" / "proof" / "run-123"

    def run(command, _cwd, _timeout, _env):
        if command == "seed-test-auth":
            return 0, "seeded auth", ""
        (proof_dir / "ios-screenshot.png").write_bytes(_png_bytes())
        (proof_dir / "android-screenshot.png").write_bytes(_png_bytes())
        (proof_dir / "ios-target.log").write_text("visible target text: Fitness First", encoding="utf-8")
        (proof_dir / "android-ui.xml").write_text('<node text="Fitness First" />', encoding="utf-8")
        (proof_dir / "ios-metro.stdout.log").write_text(
            "LOG [auth] not logged in -> onboarding\n",
            encoding="utf-8",
        )
        return 0, "captured both", ""

    runner = ProofRunner(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev"),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                setup_commands=("seed-test-auth",),
                commands=("capture-proof",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
                platform_order=("ios", "android"),
                artifact_dir="proof",
            ),
            runtime=RuntimeConfig(home_subdir=str(tmp_path / "monica-runtime")),
        ),
        run_command=run,
    )

    result = runner.run(
        run=FakeRun(),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Fitness First",
        },
    )

    assert result.passed is False
    assert result.summary == "Proof blocked: auth/onboarding proof fallback observed for: ios."
    assert result.artifacts == (
        str(proof_dir / "android-screenshot.png"),
        str(proof_dir / "android-ui.xml"),
        str(proof_dir / "ios-metro.stdout.log"),
        str(proof_dir / "ios-screenshot.png"),
        str(proof_dir / "ios-target.log"),
    )


def test_proof_runner_blocks_unattributed_auth_fallback_artifact(tmp_path):
    proof_dir = tmp_path / "monica-runtime" / "proof" / "run-123"

    def run(command, _cwd, _timeout, _env):
        if command == "seed-test-auth":
            return 0, "seeded auth", ""
        (proof_dir / "ios-screenshot.png").write_bytes(_png_bytes())
        (proof_dir / "android-screenshot.png").write_bytes(_png_bytes())
        (proof_dir / "ios-target.log").write_text("visible target text: Fitness First", encoding="utf-8")
        (proof_dir / "android-ui.xml").write_text('<node text="Fitness First" />', encoding="utf-8")
        (proof_dir / "metro.stdout.log").write_text(
            "LOG [auth] not logged in -> onboarding\n",
            encoding="utf-8",
        )
        return 0, "captured both", ""

    runner = ProofRunner(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev"),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                setup_commands=("seed-test-auth",),
                commands=("capture-proof",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
                platform_order=("ios", "android"),
                artifact_dir="proof",
            ),
            runtime=RuntimeConfig(home_subdir=str(tmp_path / "monica-runtime")),
        ),
        run_command=run,
    )

    result = runner.run(
        run=FakeRun(),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Fitness First",
        },
    )

    assert result.passed is False
    assert (
        result.summary
        == "Proof blocked: auth/onboarding proof fallback observed for: unattributed."
    )
    assert result.artifacts == (
        str(proof_dir / "android-screenshot.png"),
        str(proof_dir / "android-ui.xml"),
        str(proof_dir / "ios-screenshot.png"),
        str(proof_dir / "ios-target.log"),
        str(proof_dir / "metro.stdout.log"),
    )


def test_proof_runner_uses_configured_deep_link_when_worker_has_none(tmp_path):
    captured_env = {}

    def run(_command, _cwd, _timeout, env):
        captured_env["MONICA_DEEP_LINK"] = env["MONICA_DEEP_LINK"]
        proof_path = tmp_path / "monica-runtime" / "proof" / "run-123" / "ios-screenshot.png"
        proof_path.write_text("image bytes", encoding="utf-8")
        return 0, "captured", ""

    runner = ProofRunner(
        config=_config(tmp_path, deep_link="elixir-card://fallback/offer"),
        run_command=run,
    )

    result = runner.run(run=FakeRun(), worktree=_mark_git_worktree(tmp_path / "worktree"))

    assert result.passed is True
    assert captured_env == {"MONICA_DEEP_LINK": "elixir-card://fallback/offer"}


def test_proof_runner_clears_stale_artifacts_before_each_attempt(tmp_path):
    proof_dir = tmp_path / "monica-runtime" / "proof" / "run-123"
    proof_dir.mkdir(parents=True)
    (proof_dir / "android-screenshot.png").write_text("old launcher screenshot", encoding="utf-8")
    (proof_dir / "monica-proof-manifest.json").write_text("old manifest", encoding="utf-8")

    def run(_command, _cwd, _timeout, _env):
        (proof_dir / "ios-screenshot.png").write_text("fresh ios image bytes", encoding="utf-8")
        return 0, "captured ios only", ""

    runner = ProofRunner(
        config=_config(tmp_path, platform_order=("ios", "android")),
        run_command=run,
    )

    result = runner.run(run=FakeRun(), worktree=_mark_git_worktree(tmp_path / "worktree"))

    assert result.passed is False
    assert result.summary == "Proof blocked: missing required platform artifacts: android."
    assert result.artifacts == (str(proof_dir / "ios-screenshot.png"),)
    assert not (proof_dir / "android-screenshot.png").exists()
    assert not (proof_dir / "monica-proof-manifest.json").exists()


def test_proof_runner_clears_stale_artifacts_before_setup_config_blocks(tmp_path):
    proof_dir = tmp_path / "monica-runtime" / "proof" / "run-123"
    proof_dir.mkdir(parents=True)
    (proof_dir / "ios-screenshot.png").write_text("old ios proof", encoding="utf-8")
    (proof_dir / "android-screenshot.png").write_text("old android proof", encoding="utf-8")
    (proof_dir / "monica-proof-manifest.json").write_text("old manifest", encoding="utf-8")

    calls = []
    runner = ProofRunner(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev"),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                commands=("capture-proof",),
                platform_order=("ios", "android"),
                artifact_dir="proof",
            ),
            runtime=RuntimeConfig(home_subdir=str(tmp_path / "monica-runtime")),
        ),
        run_command=lambda *args: calls.append(args) or (0, "captured", ""),
    )

    result = runner.run(run=FakeRun(), worktree=_mark_git_worktree(tmp_path / "worktree"))

    assert result.passed is False
    assert result.summary == "Proof blocked: proof.setup_commands is empty."
    assert result.artifacts == ()
    assert calls == []
    assert list(proof_dir.iterdir()) == []


def test_proof_runner_extends_outer_timeout_for_builtin_multi_platform_simulator_command(tmp_path):
    calls = []

    def run(command, cwd, timeout, env):
        calls.append((command, timeout, env["MONICA_PROOF_PLATFORM_ORDER"]))
        proof_dir = tmp_path / "monica-runtime" / "proof" / "run-123"
        (proof_dir / "ios-screenshot.png").write_text("ios image bytes", encoding="utf-8")
        (proof_dir / "android-screenshot.png").write_text("android image bytes", encoding="utf-8")
        return 0, "captured both", ""

    runner = ProofRunner(
        config=_config(
            tmp_path,
            platform_order=("ios", "android"),
            timeout_minutes=30,
            commands=("uv run python -m plugins.mobile_bug_agent.simulator_proof --timeout-seconds 1800",),
        ),
        run_command=run,
    )

    result = runner.run(run=FakeRun(), worktree=_mark_git_worktree(tmp_path / "worktree"))

    assert result.passed is True
    assert calls == [
        (
            "uv run python -m plugins.mobile_bug_agent.simulator_proof --timeout-seconds 1800",
            4200,
            "ios,android",
        )
    ]


def test_proof_runner_normalizes_platform_aliases_before_commands_and_manifest(tmp_path):
    calls = []

    def run(command, cwd, timeout, env):
        calls.append((command, timeout, env["MONICA_PROOF_PLATFORM_ORDER"]))
        proof_dir = tmp_path / "monica-runtime" / "proof" / "run-123"
        (proof_dir / "ios-screenshot.png").write_text("ios image bytes", encoding="utf-8")
        (proof_dir / "android-screenshot.png").write_text("android image bytes", encoding="utf-8")
        return 0, "captured both", ""

    runner = ProofRunner(
        config=_config(
            tmp_path,
            platform_order=("ios-simulator", "android-emulator"),
            timeout_minutes=30,
            commands=("uv run python -m plugins.mobile_bug_agent.simulator_proof --timeout-seconds 1800",),
        ),
        run_command=run,
    )

    result = runner.run(run=FakeRun(), worktree=_mark_git_worktree(tmp_path / "worktree"))

    manifest_path = tmp_path / "monica-runtime" / "proof" / "run-123" / "monica-proof-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert result.passed is True
    assert result.platforms == ("ios", "android")
    assert manifest["platforms"] == ["ios", "android"]
    assert calls == [
        (
            "uv run python -m plugins.mobile_bug_agent.simulator_proof --timeout-seconds 1800",
            4200,
            "ios,android",
        )
    ]


def test_proof_runner_blocks_when_command_produces_no_artifacts(tmp_path):
    runner = ProofRunner(
        config=_config(tmp_path),
        run_command=lambda _cmd, _cwd, _timeout, _env: (0, "ok", ""),
    )

    result = runner.run(run=FakeRun(), worktree=_mark_git_worktree(tmp_path / "worktree"))

    assert result.passed is False
    assert result.summary == "Proof blocked: proof commands produced no artifacts."
    assert result.artifacts == ()


def test_proof_runner_blocks_when_command_produces_only_logs(tmp_path):
    def run(_command, _cwd, _timeout, _env):
        proof_dir = tmp_path / "monica-runtime" / "proof" / "run-123"
        (proof_dir / "ios-metro.stdout.log").write_text("iOS Bundled 1000ms", encoding="utf-8")
        return 0, "captured logs", ""

    runner = ProofRunner(config=_config(tmp_path), run_command=run)

    result = runner.run(run=FakeRun(), worktree=_mark_git_worktree(tmp_path / "worktree"))

    assert result.passed is False
    assert result.summary == "Proof blocked: proof commands produced no screenshot or recording artifacts."
    assert result.artifacts == (
        str(tmp_path / "monica-runtime" / "proof" / "run-123" / "ios-metro.stdout.log"),
    )


def test_proof_runner_blocks_when_required_platform_artifact_is_missing(tmp_path):
    def run(_command, _cwd, _timeout, _env):
        proof_path = tmp_path / "monica-runtime" / "proof" / "run-123" / "android-screenshot.png"
        proof_path.write_text("android image bytes", encoding="utf-8")
        return 0, "captured android", ""

    runner = ProofRunner(
        config=_config(tmp_path, platform_order=("ios", "android")),
        run_command=run,
    )

    result = runner.run(run=FakeRun(), worktree=_mark_git_worktree(tmp_path / "worktree"))

    assert result.passed is False
    assert result.summary == "Proof blocked: missing required platform artifacts: ios."
    assert result.artifacts == (
        str(tmp_path / "monica-runtime" / "proof" / "run-123" / "android-screenshot.png"),
    )


def test_proof_runner_blocks_when_visual_proof_artifact_is_empty(tmp_path):
    def run(_command, _cwd, _timeout, _env):
        proof_dir = tmp_path / "monica-runtime" / "proof" / "run-123"
        (proof_dir / "ios-screenshot.png").touch()
        return 0, "captured empty ios screenshot", ""

    runner = ProofRunner(config=_config(tmp_path), run_command=run)

    result = runner.run(run=FakeRun(), worktree=_mark_git_worktree(tmp_path / "worktree"))

    assert result.passed is False
    assert result.summary == "Proof blocked: empty proof artifact files: ios."
    assert result.artifacts == (
        str(tmp_path / "monica-runtime" / "proof" / "run-123" / "ios-screenshot.png"),
    )


def test_proof_runner_blocks_approved_pr_when_screenshot_files_are_not_images(tmp_path):
    proof_dir = tmp_path / "monica-runtime" / "proof" / "run-123"

    def run(command, _cwd, _timeout, _env):
        if command == "capture-proof":
            (proof_dir / "ios-screenshot.png").write_text("ios image bytes", encoding="utf-8")
            (proof_dir / "android-screenshot.png").write_text("android image bytes", encoding="utf-8")
            (proof_dir / "ios-target.log").write_text("visible target text: Fitness First", encoding="utf-8")
            (proof_dir / "android-ui.xml").write_text("<node text='Fitness First' />", encoding="utf-8")
        return 0, "captured screenshots", ""

    runner = ProofRunner(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            repo=RepoConfig(default_branch="dev"),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                setup_commands=("seed-test-auth",),
                commands=("capture-proof",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
                platform_order=("ios", "android"),
                artifact_dir="proof",
            ),
            runtime=RuntimeConfig(home_subdir=str(tmp_path / "monica-runtime")),
        ),
        run_command=run,
    )

    result = runner.run(
        run=FakeRun(),
        worktree=_mark_git_worktree(tmp_path / "worktree"),
        proof_target={
            "deep_link": "elixir-card://marketplace/offer/fitness-first",
            "expected_text": "Fitness First",
        },
    )

    assert result.passed is False
    assert result.summary == "Proof blocked: invalid proof artifact files: ios, android."
    assert result.artifacts == (
        str(proof_dir / "android-screenshot.png"),
        str(proof_dir / "android-ui.xml"),
        str(proof_dir / "ios-screenshot.png"),
        str(proof_dir / "ios-target.log"),
    )


def test_proof_runner_does_not_count_platform_logs_as_required_visual_proof(tmp_path):
    def run(_command, _cwd, _timeout, _env):
        proof_dir = tmp_path / "monica-runtime" / "proof" / "run-123"
        (proof_dir / "ios-metro.stdout.log").write_text("iOS Bundled 1000ms", encoding="utf-8")
        (proof_dir / "android-screenshot.png").write_text("android image bytes", encoding="utf-8")
        return 0, "captured android and ios logs", ""

    runner = ProofRunner(
        config=_config(tmp_path, platform_order=("ios", "android")),
        run_command=run,
    )

    result = runner.run(run=FakeRun(), worktree=_mark_git_worktree(tmp_path / "worktree"))

    assert result.passed is False
    assert result.summary == "Proof blocked: missing required platform artifacts: ios."
    assert result.artifacts == (
        str(tmp_path / "monica-runtime" / "proof" / "run-123" / "android-screenshot.png"),
        str(tmp_path / "monica-runtime" / "proof" / "run-123" / "ios-metro.stdout.log"),
    )


def test_proof_runner_requires_distinct_visual_artifacts_per_platform(tmp_path):
    def run(_command, _cwd, _timeout, _env):
        proof_dir = tmp_path / "monica-runtime" / "proof" / "run-123"
        (proof_dir / "ios-android-screenshot.png").write_text("one mixed image", encoding="utf-8")
        return 0, "captured one mixed artifact", ""

    runner = ProofRunner(
        config=_config(tmp_path, platform_order=("ios", "android")),
        run_command=run,
    )

    result = runner.run(run=FakeRun(), worktree=_mark_git_worktree(tmp_path / "worktree"))

    assert result.passed is False
    assert result.summary == "Proof blocked: missing required platform artifacts: android."
    assert result.artifacts == (
        str(tmp_path / "monica-runtime" / "proof" / "run-123" / "ios-android-screenshot.png"),
    )


def test_proof_runner_passes_only_after_all_required_platform_artifacts_exist(tmp_path):
    def run(_command, _cwd, _timeout, _env):
        proof_dir = tmp_path / "monica-runtime" / "proof" / "run-123"
        (proof_dir / "android-screenshot.png").write_text("android image bytes", encoding="utf-8")
        (proof_dir / "ios-screenshot.png").write_text("ios image bytes", encoding="utf-8")
        return 0, "captured both", ""

    runner = ProofRunner(
        config=_config(tmp_path, platform_order=("ios", "android")),
        run_command=run,
    )

    result = runner.run(run=FakeRun(), worktree=_mark_git_worktree(tmp_path / "worktree"))

    proof_dir = tmp_path / "monica-runtime" / "proof" / "run-123"
    assert result.passed is True
    assert result.artifacts == (
        str(proof_dir / "monica-proof-manifest.json"),
        str(proof_dir / "android-screenshot.png"),
        str(proof_dir / "ios-screenshot.png"),
    )


def test_proof_runner_preserves_partial_artifacts_on_command_failure(tmp_path):
    def run(_command, _cwd, _timeout, env):
        proof_path = tmp_path / "monica-runtime" / "proof" / "run-123" / "before-failure.txt"
        proof_path.write_text(env["MONICA_LINEAR_IDENTIFIER"], encoding="utf-8")
        return 1, "", "simulator failed"

    runner = ProofRunner(config=_config(tmp_path), run_command=run)

    result = runner.run(run=FakeRun(), worktree=_mark_git_worktree(tmp_path / "worktree"))

    assert result.passed is False
    assert result.summary == "Proof blocked: capture-proof"
    assert result.artifacts == (str(tmp_path / "monica-runtime" / "proof" / "run-123" / "before-failure.txt"),)
    assert result.to_dict()["setup_commands"] == []
    assert result.to_dict()["commands"] == ["capture-proof"]
    assert "simulator failed" in result.output
