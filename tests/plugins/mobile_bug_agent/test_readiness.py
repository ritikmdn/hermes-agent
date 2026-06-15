from __future__ import annotations

from dataclasses import replace

import pytest

from plugins.mobile_bug_agent.config import (
    LinearConfig,
    MonicaConfig,
    ProofConfig,
    RepoConfig,
    SlackConfig,
    VerificationConfig,
    WorkerConfig,
)
from plugins.mobile_bug_agent.readiness import check_monica_readiness


def _code_rollout_config() -> MonicaConfig:
    return MonicaConfig(
        enabled=True,
        rollout_mode="local_fix_only",
        slack=SlackConfig(
            bot_user_ids=("U_MONICA",),
            allowed_channels=("D123MONICA",),
            approver_user_ids=("U_APPROVER",),
        ),
        linear=LinearConfig(team_id="team-id"),
        repo=RepoConfig(url="git@github.com:acme/mobile.git"),
        verification=VerificationConfig(commands=("npm test",)),
        proof=ProofConfig(
            enabled=True,
            required_for_done=True,
            commands=("npm run monica:proof",),
            platform_order=("ios",),
        ),
    )


def test_readiness_warns_when_simctl_probe_fails_even_if_xcode_tools_exist():
    report = check_monica_readiness(
        config=_code_rollout_config(),
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        command_succeeds=lambda command: command != ("xcrun", "--find", "simctl"),
    )

    assert report.ready is True
    assert any(issue.code == "ios_proof_tooling" for issue in report.warnings)


def test_readiness_accepts_ios_proof_when_simctl_probe_passes():
    report = check_monica_readiness(
        config=_code_rollout_config(),
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        command_succeeds=lambda _command: True,
    )

    warning_codes = {issue.code for issue in report.warnings}
    assert report.ready is True
    assert "ios_proof_tooling" not in warning_codes


def test_readiness_warns_when_no_model_credential_is_configured_for_agentic_triage():
    config = replace(
        _code_rollout_config(),
        worker=WorkerConfig(backend="internal_agent"),
    )

    report = check_monica_readiness(
        config=config,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        command_succeeds=lambda _command: True,
    )

    assert report.ready is True
    assert any(issue.code == "intent_classifier_model" for issue in report.warnings)


def test_readiness_accepts_agentic_triage_with_codex_cli_classifier_path():
    report = check_monica_readiness(
        config=_code_rollout_config(),
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        command_succeeds=lambda _command: True,
    )

    warning_codes = {issue.code for issue in report.warnings}
    assert report.ready is True
    assert "intent_classifier_model" not in warning_codes


def test_readiness_accepts_agentic_triage_when_a_model_credential_is_configured():
    config = replace(
        _code_rollout_config(),
        worker=WorkerConfig(backend="internal_agent"),
    )

    report = check_monica_readiness(
        config=config,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "OPENAI_API_KEY": "openai-key",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        command_succeeds=lambda _command: True,
    )

    warning_codes = {issue.code for issue in report.warnings}
    assert report.ready is True
    assert "intent_classifier_model" not in warning_codes


def test_readiness_blocks_builtin_ios_simulator_proof_without_dev_client_settings():
    config = replace(
        _code_rollout_config(),
        proof=ProofConfig(
            enabled=True,
            required_for_done=True,
            commands=("python -m plugins.mobile_bug_agent.simulator_proof --timeout-seconds 600",),
            platform_order=("ios",),
        ),
    )

    report = check_monica_readiness(
        config=config,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        command_succeeds=lambda _command: True,
    )

    failure_codes = {issue.code for issue in report.failures}
    assert report.ready is False
    assert "proof_ios_dev_client_scheme" in failure_codes
    assert "proof_ios_bundle_id" in failure_codes


def test_readiness_accepts_builtin_ios_simulator_proof_with_configured_dev_client_settings():
    config = replace(
        _code_rollout_config(),
        proof=ProofConfig(
            enabled=True,
            required_for_done=True,
            commands=("python -m plugins.mobile_bug_agent.simulator_proof --timeout-seconds 600",),
            platform_order=("ios",),
            dev_client_scheme="elixir-card",
            ios_bundle_id="com.elixir.card",
        ),
    )

    report = check_monica_readiness(
        config=config,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        command_succeeds=lambda _command: True,
    )

    assert report.ready is True


def test_readiness_accepts_builtin_ios_simulator_proof_with_command_settings():
    config = replace(
        _code_rollout_config(),
        proof=ProofConfig(
            enabled=True,
            required_for_done=True,
            commands=(
                "MONICA_IOS_BUNDLE_ID=com.elixir.card "
                "python -m plugins.mobile_bug_agent.simulator_proof "
                "--dev-client-scheme elixir-card --timeout-seconds 600",
            ),
            platform_order=("ios",),
        ),
    )

    report = check_monica_readiness(
        config=config,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        command_succeeds=lambda _command: True,
    )

    assert report.ready is True


def test_readiness_blocks_builtin_android_simulator_proof_without_package():
    config = replace(
        _code_rollout_config(),
        proof=ProofConfig(
            enabled=True,
            required_for_done=True,
            commands=("python -m plugins.mobile_bug_agent.simulator_proof --timeout-seconds 600",),
            platform_order=("android",),
        ),
    )

    report = check_monica_readiness(
        config=config,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        command_succeeds=lambda _command: True,
    )

    failure_codes = {issue.code for issue in report.failures}
    assert report.ready is False
    assert "proof_android_package" in failure_codes


def test_readiness_accepts_builtin_android_simulator_proof_with_command_package():
    config = replace(
        _code_rollout_config(),
        proof=ProofConfig(
            enabled=True,
            required_for_done=True,
            commands=(
                "MONICA_ANDROID_PACKAGE=com.elixir.card "
                "python -m plugins.mobile_bug_agent.simulator_proof "
                "--timeout-seconds 600",
            ),
            platform_order=("android",),
        ),
    )

    report = check_monica_readiness(
        config=config,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        command_succeeds=lambda _command: True,
    )

    assert report.ready is True


def test_readiness_blocks_approved_pr_without_proof_commands():
    config = replace(
        _code_rollout_config(),
        rollout_mode="approved_pr",
        proof=ProofConfig(),
    )

    report = check_monica_readiness(
        config=config,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "GITHUB_TOKEN": "gh-key",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        command_succeeds=lambda _command: True,
    )

    failure_codes = {issue.code for issue in report.failures}
    assert report.ready is False
    assert "proof_commands_empty" in failure_codes


def test_readiness_blocks_approved_pr_with_placeholder_proof_command():
    config = replace(
        _code_rollout_config(),
        rollout_mode="approved_pr",
        proof=ProofConfig(
            enabled=True,
            required_for_done=True,
            commands=("<simulator proof command>",),
            setup_commands=("npm run monica:seed-auth",),
            platform_order=("ios", "android"),
        ),
    )

    report = check_monica_readiness(
        config=config,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "GITHUB_TOKEN": "gh-key",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        command_succeeds=lambda _command: True,
    )

    failure_codes = {issue.code for issue in report.failures}
    assert report.ready is False
    assert "proof_commands_placeholder" in failure_codes


def test_readiness_blocks_approved_pr_with_only_noop_proof_commands():
    config = replace(
        _code_rollout_config(),
        rollout_mode="approved_pr",
        proof=ProofConfig(
            enabled=True,
            required_for_done=True,
            commands=("true", "exit 0"),
            setup_commands=("npm run monica:seed-auth",),
            platform_order=("ios", "android"),
        ),
    )

    report = check_monica_readiness(
        config=config,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "GITHUB_TOKEN": "gh-key",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        command_succeeds=lambda _command: True,
    )

    failure_codes = {issue.code for issue in report.failures}
    assert report.ready is False
    assert "proof_commands_noop" in failure_codes


def test_readiness_blocks_approved_pr_with_inline_secret_proof_command():
    config = replace(
        _code_rollout_config(),
        rollout_mode="approved_pr",
        proof=ProofConfig(
            enabled=True,
            required_for_done=True,
            commands=("MONICA_TEST_LOGIN_OTP=123456 npm run monica:proof",),
            setup_commands=("npm run monica:seed-auth",),
            platform_order=("ios", "android"),
        ),
    )

    report = check_monica_readiness(
        config=config,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "GITHUB_TOKEN": "gh-key",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        command_succeeds=lambda _command: True,
    )

    failure_codes = {issue.code for issue in report.failures}
    assert report.ready is False
    assert "proof_commands_inline_secret" in failure_codes


def test_readiness_blocks_approved_pr_without_proof_setup_commands():
    config = replace(
        _code_rollout_config(),
        rollout_mode="approved_pr",
        proof=ProofConfig(
            enabled=True,
            required_for_done=True,
            commands=("npm run monica:proof",),
            platform_order=("ios", "android"),
        ),
    )

    report = check_monica_readiness(
        config=config,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "GITHUB_TOKEN": "gh-key",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        command_succeeds=lambda _command: True,
    )

    failure_codes = {issue.code for issue in report.failures}
    assert report.ready is False
    assert "proof_setup_commands_empty" in failure_codes


def test_readiness_blocks_approved_pr_without_required_proof_env_keys():
    config = replace(
        _code_rollout_config(),
        rollout_mode="approved_pr",
        proof=ProofConfig(
            enabled=True,
            required_for_done=True,
            commands=("npm run monica:proof",),
            setup_commands=("npm run monica:seed-auth",),
            platform_order=("ios", "android"),
        ),
    )

    report = check_monica_readiness(
        config=config,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "GITHUB_TOKEN": "gh-key",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        command_succeeds=lambda _command: True,
    )

    failure_codes = {issue.code for issue in report.failures}
    assert report.ready is False
    assert "proof_required_env_keys_empty" in failure_codes


def test_readiness_blocks_approved_pr_with_placeholder_proof_setup_command():
    config = replace(
        _code_rollout_config(),
        rollout_mode="approved_pr",
        proof=ProofConfig(
            enabled=True,
            required_for_done=True,
            commands=("npm run monica:proof",),
            setup_commands=("<auth/session seed command>",),
            platform_order=("ios", "android"),
        ),
    )

    report = check_monica_readiness(
        config=config,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "GITHUB_TOKEN": "gh-key",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        command_succeeds=lambda _command: True,
    )

    failure_codes = {issue.code for issue in report.failures}
    assert report.ready is False
    assert "proof_setup_commands_placeholder" in failure_codes


def test_readiness_blocks_approved_pr_with_embedded_placeholder_proof_commands():
    config = replace(
        _code_rollout_config(),
        rollout_mode="approved_pr",
        proof=ProofConfig(
            enabled=True,
            required_for_done=True,
            commands=("sh -c '<simulator proof command>'",),
            setup_commands=("npm run monica:seed -- '<auth/session seed command>'",),
            required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
            platform_order=("ios", "android"),
        ),
    )

    report = check_monica_readiness(
        config=config,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "GITHUB_TOKEN": "gh-key",
            "MONICA_TEST_LOGIN_TOKEN": "test-token",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        command_succeeds=lambda _command: True,
    )

    failure_codes = {issue.code for issue in report.failures}
    assert report.ready is False
    assert "proof_commands_placeholder" in failure_codes
    assert "proof_setup_commands_placeholder" in failure_codes


def test_readiness_blocks_approved_pr_with_only_noop_proof_setup_commands():
    config = replace(
        _code_rollout_config(),
        rollout_mode="approved_pr",
        proof=ProofConfig(
            enabled=True,
            required_for_done=True,
            commands=("npm run monica:proof",),
            setup_commands=("true", "exit 0"),
            platform_order=("ios", "android"),
        ),
    )

    report = check_monica_readiness(
        config=config,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "GITHUB_TOKEN": "gh-key",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        command_succeeds=lambda _command: True,
    )

    failure_codes = {issue.code for issue in report.failures}
    assert report.ready is False
    assert "proof_setup_commands_noop" in failure_codes


def test_readiness_blocks_approved_pr_with_inline_secret_proof_setup_command():
    config = replace(
        _code_rollout_config(),
        rollout_mode="approved_pr",
        proof=ProofConfig(
            enabled=True,
            required_for_done=True,
            commands=("npm run monica:proof",),
            setup_commands=("MONICA_TEST_LOGIN_TOKEN=secret npm run monica:seed-auth",),
            platform_order=("ios", "android"),
        ),
    )

    report = check_monica_readiness(
        config=config,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "GITHUB_TOKEN": "gh-key",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        command_succeeds=lambda _command: True,
    )

    failure_codes = {issue.code for issue in report.failures}
    assert report.ready is False
    assert "proof_setup_commands_inline_secret" in failure_codes


@pytest.mark.parametrize(
    ("proof_kwargs", "expected_code"),
    (
        (
            {
                "setup_commands": ("npm run monica:seed-auth -- --token profile-secret",),
                "commands": ("npm run monica:proof",),
            },
            "proof_setup_commands_literal_secret",
        ),
        (
            {
                "setup_commands": ("npm run monica:seed-auth",),
                "commands": ("npm run monica:proof -- --token=profile-secret",),
            },
            "proof_commands_literal_secret",
        ),
    ),
)
def test_readiness_blocks_approved_pr_when_proof_commands_embed_required_env_values(
    proof_kwargs,
    expected_code,
):
    config = replace(
        _code_rollout_config(),
        rollout_mode="approved_pr",
        proof=ProofConfig(
            enabled=True,
            required_for_done=True,
            required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
            platform_order=("ios", "android"),
            **proof_kwargs,
        ),
    )

    report = check_monica_readiness(
        config=config,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "GITHUB_TOKEN": "gh-key",
            "MONICA_TEST_LOGIN_TOKEN": "profile-secret",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        command_succeeds=lambda _command: True,
    )

    failure_codes = {issue.code for issue in report.failures}
    messages = "\n".join(issue.message for issue in report.failures)
    assert report.ready is False
    assert expected_code in failure_codes
    assert "MONICA_TEST_LOGIN_TOKEN" in messages
    assert "profile-secret" not in messages


def test_readiness_blocks_approved_pr_with_blank_proof_commands():
    config = replace(
        _code_rollout_config(),
        rollout_mode="approved_pr",
        proof=ProofConfig(
            enabled=True,
            required_for_done=True,
            commands=("   ",),
            setup_commands=("\t",),
            platform_order=("ios", "android"),
        ),
    )

    report = check_monica_readiness(
        config=config,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "GITHUB_TOKEN": "gh-key",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        command_succeeds=lambda _command: True,
    )

    failure_codes = {issue.code for issue in report.failures}
    assert report.ready is False
    assert "proof_commands_empty" in failure_codes
    assert "proof_setup_commands_empty" in failure_codes


def test_readiness_blocks_approved_pr_with_blank_verification_commands():
    config = replace(
        _code_rollout_config(),
        rollout_mode="approved_pr",
        verification=VerificationConfig(commands=("   ",)),
        proof=ProofConfig(
            enabled=True,
            required_for_done=True,
            commands=("npm run monica:proof",),
            setup_commands=("npm run monica:seed-auth",),
            platform_order=("ios", "android"),
        ),
    )

    report = check_monica_readiness(
        config=config,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "GITHUB_TOKEN": "gh-key",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        command_succeeds=lambda _command: True,
    )

    failure_codes = {issue.code for issue in report.failures}
    assert report.ready is False
    assert "verification_commands" in failure_codes


def test_readiness_blocks_approved_pr_when_github_auth_is_unavailable():
    config = replace(_code_rollout_config(), rollout_mode="approved_pr")

    report = check_monica_readiness(
        config=config,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        command_succeeds=lambda command: command != ("gh", "auth", "status"),
    )

    failure_codes = {issue.code for issue in report.failures}
    assert report.ready is False
    assert "github_auth" in failure_codes


def test_readiness_accepts_approved_pr_with_github_token_when_gh_auth_fails():
    config = replace(
        _code_rollout_config(),
        rollout_mode="approved_pr",
        proof=ProofConfig(
            commands=("npm run monica:proof",),
            setup_commands=("npm run monica:seed-auth",),
            required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
            platform_order=("ios", "android"),
        ),
    )

    report = check_monica_readiness(
        config=config,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "GITHUB_TOKEN": "gh-token",
            "MONICA_TEST_LOGIN_TOKEN": "token",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        command_succeeds=lambda command: command != ("gh", "auth", "status"),
    )

    assert report.ready is True


def test_readiness_blocks_approved_pr_when_required_proof_env_keys_are_missing():
    config = replace(
        _code_rollout_config(),
        rollout_mode="approved_pr",
        proof=ProofConfig(
            commands=("npm run monica:proof",),
            setup_commands=("npm run monica:seed-auth",),
            required_env_keys=("MONICA_TEST_LOGIN_TOKEN", "MONICA_TEST_LOGIN_OTP"),
            platform_order=("ios", "android"),
        ),
    )

    report = check_monica_readiness(
        config=config,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "GITHUB_TOKEN": "gh-token",
            "MONICA_TEST_LOGIN_TOKEN": "token",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        command_succeeds=lambda _command: True,
    )

    failure_codes = {issue.code for issue in report.failures}
    assert report.ready is False
    assert "proof_required_env_keys" in failure_codes
    assert any(
        issue.message == "proof.required_env_keys are missing from the Monica profile environment: MONICA_TEST_LOGIN_OTP"
        for issue in report.failures
    )


def test_readiness_blocks_approved_pr_with_invalid_required_proof_env_key_without_echoing_value():
    config = replace(
        _code_rollout_config(),
        rollout_mode="approved_pr",
        proof=ProofConfig(
            commands=("npm run monica:proof",),
            setup_commands=("npm run monica:seed-auth",),
            required_env_keys=("MONICA_TEST_LOGIN_TOKEN=secret",),
            platform_order=("ios", "android"),
        ),
    )

    report = check_monica_readiness(
        config=config,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "GITHUB_TOKEN": "gh-token",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        command_succeeds=lambda _command: True,
    )

    failure_codes = {issue.code for issue in report.failures}
    messages = "\n".join(issue.message for issue in report.failures)
    assert report.ready is False
    assert "proof_required_env_keys_invalid" in failure_codes
    assert "MONICA_TEST_LOGIN_TOKEN" in messages
    assert "secret" not in messages


@pytest.mark.parametrize("required_env_key", ("MONICA_WORKTREE", "MONICA_PROOF_SCREEN"))
def test_readiness_blocks_approved_pr_when_required_proof_env_key_is_builtin_context(required_env_key):
    config = replace(
        _code_rollout_config(),
        rollout_mode="approved_pr",
        proof=ProofConfig(
            commands=("npm run monica:proof",),
            setup_commands=("npm run monica:seed-auth",),
            required_env_keys=(required_env_key,),
            platform_order=("ios", "android"),
        ),
    )

    report = check_monica_readiness(
        config=config,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "GITHUB_TOKEN": "gh-token",
            required_env_key: "context-value",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        command_succeeds=lambda _command: True,
    )

    failure_codes = {issue.code for issue in report.failures}
    messages = "\n".join(issue.message for issue in report.failures)
    assert report.ready is False
    assert "proof_required_env_keys_invalid" in failure_codes
    assert required_env_key in messages
    assert "built-in Monica proof context" in messages


def test_readiness_accepts_approved_pr_when_required_proof_env_keys_are_present():
    config = replace(
        _code_rollout_config(),
        rollout_mode="approved_pr",
        proof=ProofConfig(
            commands=("npm run monica:proof",),
            setup_commands=("npm run monica:seed-auth",),
            required_env_keys=("MONICA_TEST_LOGIN_TOKEN", "MONICA_TEST_LOGIN_OTP"),
            platform_order=("ios", "android"),
        ),
    )

    report = check_monica_readiness(
        config=config,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "GITHUB_TOKEN": "gh-token",
            "MONICA_TEST_LOGIN_TOKEN": "token",
            "MONICA_TEST_LOGIN_OTP": "123456",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        command_succeeds=lambda _command: True,
    )

    assert report.ready is True


def test_readiness_blocks_approved_pr_when_cached_repo_is_dirty(tmp_path):
    runtime = tmp_path / "monica-runtime"
    repo_path = runtime / "workspace" / "repos" / "mobile-app"
    repo_path.mkdir(parents=True)
    config = replace(
        _code_rollout_config(),
        rollout_mode="approved_pr",
        runtime=replace(_code_rollout_config().runtime, home_subdir=str(runtime)),
        proof=ProofConfig(
            commands=("npm run monica:proof",),
            setup_commands=("npm run monica:seed-auth",),
            required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
            platform_order=("ios", "android"),
        ),
    )

    def command_output(command: tuple[str, ...]) -> str:
        assert command == ("git", "-C", str(repo_path), "status", "--porcelain")
        return " M apps/elixir-card/fingerprint.config.js\n"

    report = check_monica_readiness(
        config=config,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "GITHUB_TOKEN": "gh-token",
            "MONICA_TEST_LOGIN_TOKEN": "token",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        command_succeeds=lambda _command: True,
        command_output=command_output,
    )

    assert report.ready is False
    assert any(issue.code == "repo_cached_dirty" for issue in report.failures)
    assert any("fingerprint.config.js" in issue.message for issue in report.failures)


def test_readiness_accepts_required_proof_env_keys_from_profile_env_file(tmp_path, monkeypatch):
    hermes_home = tmp_path / "profile-home"
    hermes_home.mkdir()
    (hermes_home / ".env").write_text(
        "MONICA_TEST_LOGIN_TOKEN=profile-token\n"
        "MONICA_TEST_LOGIN_OTP=123456\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("MONICA_SLACK_BOT_TOKEN", "xoxb-token")
    monkeypatch.setenv("MONICA_SLACK_APP_TOKEN", "xapp-token")
    monkeypatch.setenv("LINEAR_API_KEY", "lin-key")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-token")
    monkeypatch.delenv("MONICA_TEST_LOGIN_TOKEN", raising=False)
    monkeypatch.delenv("MONICA_TEST_LOGIN_OTP", raising=False)
    config = replace(
        _code_rollout_config(),
        rollout_mode="approved_pr",
        proof=ProofConfig(
            commands=("npm run monica:proof",),
            setup_commands=("npm run monica:seed-auth",),
            required_env_keys=("MONICA_TEST_LOGIN_TOKEN", "MONICA_TEST_LOGIN_OTP"),
            platform_order=("ios", "android"),
        ),
    )

    report = check_monica_readiness(
        config=config,
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        command_succeeds=lambda _command: True,
    )

    assert report.ready is True


def test_readiness_blocks_approved_pr_without_both_required_proof_platforms():
    config = replace(
        _code_rollout_config(),
        rollout_mode="approved_pr",
        proof=ProofConfig(
            commands=("npm run monica:proof",),
            setup_commands=("npm run monica:seed-auth",),
            platform_order=("ios",),
        ),
    )

    report = check_monica_readiness(
        config=config,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "GITHUB_TOKEN": "gh-token",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        command_succeeds=lambda _command: True,
    )

    failure_codes = {issue.code for issue in report.failures}
    assert report.ready is False
    assert "proof_platform_order" in failure_codes


def test_readiness_requires_slack_files_write_for_approved_pr_when_scopes_are_declared():
    config = replace(_code_rollout_config(), rollout_mode="approved_pr")

    report = check_monica_readiness(
        config=config,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "GITHUB_TOKEN": "gh-token",
            "SLACK_BOT_SCOPES": "app_mentions:read,chat:write,channels:history,files:read",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        command_succeeds=lambda _command: True,
    )

    failure_codes = {issue.code for issue in report.failures}
    assert report.ready is False
    assert "slack_scope_files_write" in failure_codes
