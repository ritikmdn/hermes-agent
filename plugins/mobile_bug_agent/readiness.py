from __future__ import annotations

import importlib.util
import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from hermes_constants import get_env_path

from .config import MonicaConfig, runtime_root
from .repo_manager import (
    RepoManagerError,
    safe_branch_prefix,
    safe_default_branch,
    safe_repo_local_name,
)
from .secrets import MONICA_SLACK_APP_TOKEN, MONICA_SLACK_BOT_TOKEN

_SLACK_CHANNEL_ID_RE = re.compile(r"^[CDG][A-Z0-9]{2,}$")
_SLACK_USER_ID_RE = re.compile(r"^[UW][A-Z0-9_]{2,}$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MODEL_CREDENTIAL_KEYS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "OPENROUTER_API_KEY",
    "GROQ_API_KEY",
)
_BUILTIN_PROOF_CONTEXT_ENV_KEYS = {
    "MONICA_ANDROID_AVD",
    "MONICA_ANDROID_PACKAGE",
    "MONICA_ANDROID_SERIAL",
    "MONICA_BASE_COMMIT",
    "MONICA_BASE_REF",
    "MONICA_BRANCH_NAME",
    "MONICA_DEEP_LINK",
    "MONICA_DEV_CLIENT_SCHEME",
    "MONICA_HERMES_AGENT_ROOT",
    "MONICA_IOS_BUNDLE_ID",
    "MONICA_IOS_SIMULATOR_UDID",
    "MONICA_LINEAR_IDENTIFIER",
    "MONICA_LINEAR_URL",
    "MONICA_PROOF_DIR",
    "MONICA_PROOF_EXPECTED_TEXT",
    "MONICA_PROOF_PLATFORM_ORDER",
    "MONICA_PROOF_SCREEN",
    "MONICA_REQUIRED_ENV_KEYS",
    "MONICA_RUN_ID",
    "MONICA_WORKTREE",
    "PYTHONPATH",
}


@dataclass(frozen=True)
class ReadinessIssue:
    code: str
    message: str


@dataclass(frozen=True)
class ReadinessReport:
    rollout_mode: str
    runtime_root_value: str
    warnings: tuple[ReadinessIssue, ...]
    failures: tuple[ReadinessIssue, ...]

    @property
    def ready(self) -> bool:
        return not self.failures

    def first_approval_failure(self) -> str:
        if not self.failures:
            return ""
        by_code = {issue.code: issue.message for issue in self.failures}
        for code in _APPROVAL_FAILURE_PRIORITY:
            message = by_code.get(code)
            if message:
                return message
        return self.failures[0].message


def check_monica_readiness(
    *,
    config: MonicaConfig,
    environ: dict[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
    module_available: Callable[[str], bool] | None = None,
    command_succeeds: Callable[[tuple[str, ...]], bool] | None = None,
    command_output: Callable[[tuple[str, ...]], str] | None = None,
) -> ReadinessReport:
    if environ is None:
        env = dict(os.environ)
        env.update(_profile_env_values())
    else:
        env = environ
    resolve = which or shutil.which
    has_module = module_available or _module_available
    command_ok = command_succeeds or _command_succeeds
    command_stdout = command_output or _command_output
    verify_commands = command_succeeds is not None or which is None
    failures: list[ReadinessIssue] = []
    warnings: list[ReadinessIssue] = []
    runtime_root_value = ""

    def require(code: str, condition: bool, message: str) -> None:
        if not condition:
            failures.append(ReadinessIssue(code, message))

    def warn(code: str, condition: bool, message: str) -> None:
        if not condition:
            warnings.append(ReadinessIssue(code, message))

    require("config_enabled", config.enabled, "mobile_bug_agent.enabled is false")
    try:
        runtime_root_value = str(runtime_root(config))
    except ValueError as exc:
        failures.append(ReadinessIssue("runtime_root", str(exc)))
    require(
        "runtime_chandler",
        not _runtime_path_mentions_chandler(config.runtime.home_subdir),
        "mobile_bug_agent.runtime.home_subdir must not point at a Chandler runtime path",
    )
    require(
        "slack_bot_token",
        _has_secret(env, MONICA_SLACK_BOT_TOKEN),
        f"{MONICA_SLACK_BOT_TOKEN} is missing",
    )
    require(
        "slack_sdk",
        bool(has_module("slack_sdk")),
        "slack-sdk Python package is not installed; install `hermes-agent[slack]` or `hermes-agent[messaging]`",
    )
    warn(
        "slack_app_token",
        _has_secret(env, MONICA_SLACK_APP_TOKEN),
        f"{MONICA_SLACK_APP_TOKEN} is missing; Monica Slack Socket Mode may not start",
    )
    warn(
        "slack_bot_user_ids_empty_warning",
        bool(config.slack.bot_user_ids),
        "slack.bot_user_ids is empty; Monica will rely only on app_mention events",
    )
    bot_id_values = [
        user_id for user_id in config.slack.bot_user_ids if user_id.upper().startswith("B")
    ]
    require(
        "slack_bot_id_value",
        not bot_id_values,
        (
            "slack.bot_user_ids must contain Slack mention user IDs like U012ABCDEF, "
            f"not bot_id values like {bot_id_values[0]}"
        ) if bot_id_values else "",
    )
    handle_style_bot_ids = [
        user_id for user_id in config.slack.bot_user_ids if str(user_id).strip().startswith("@")
    ]
    require(
        "slack_bot_handle_value",
        not handle_style_bot_ids,
        (
            "slack.bot_user_ids must contain Slack mention user IDs like U012ABCDEF, "
            f"not handles like {handle_style_bot_ids[0]}"
        ) if handle_style_bot_ids else "",
    )
    invalid_bot_user_ids = [
        user_id
        for user_id in config.slack.bot_user_ids
        if not str(user_id).strip().startswith("@")
        and not str(user_id).upper().startswith("B")
        and not _is_slack_user_id(user_id)
    ]
    require(
        "slack_bot_user_id_invalid",
        not invalid_bot_user_ids,
        (
            "slack.bot_user_ids must contain Slack mention user IDs like U012ABCDEF, "
            f"not invalid values like {invalid_bot_user_ids[0]}"
        ) if invalid_bot_user_ids else "",
    )
    invalid_channels = [
        channel for channel in config.slack.allowed_channels if not _is_slack_channel_id(channel)
    ]
    require(
        "slack_allowed_channel_invalid",
        not invalid_channels,
        (
            "slack.allowed_channels must contain Slack channel IDs like C123 or G123, "
            f"not names like {invalid_channels[0]}"
        ) if invalid_channels else "",
    )

    side_effect_rollout = config.rollout_mode in {"linear_only", "local_fix_only", "approved_pr"}
    if side_effect_rollout:
        codex_cli_classifier_available = (
            config.worker.backend == "codex_cli"
            and resolve(config.worker.codex_command) is not None
        )
        warn(
            "intent_classifier_model",
            _has_any_secret(env, _MODEL_CREDENTIAL_KEYS) or codex_cli_classifier_available,
            (
                "No common model API credential is configured; Monica's intent classifier "
                "may fall back to deterministic keyword triage"
            ),
        )
        require(
            "slack_bot_user_ids_empty",
            bool(config.slack.bot_user_ids),
            "slack.bot_user_ids is empty; configure Monica's Slack mention user ID",
        )
        require(
            "slack_allowed_channels_empty",
            bool(config.slack.allowed_channels),
            "slack.allowed_channels is empty; configure the Slack channels Monica may act in",
        )
    else:
        warn(
            "slack_allowed_channels_empty_warning",
            bool(config.slack.allowed_channels),
            "slack.allowed_channels is empty; Monica can run in any channel where the Slack app is present",
        )

    scopes = _scope_set(env.get("SLACK_BOT_SCOPES") or env.get("SLACK_SCOPES"))
    if scopes:
        require(
            "slack_scope_app_mentions",
            "app_mentions:read" in scopes,
            "SLACK_BOT_SCOPES is missing required scope: app_mentions:read",
        )
        require(
            "slack_scope_chat_write",
            "chat:write" in scopes,
            "SLACK_BOT_SCOPES is missing required scope: chat:write",
        )
        require(
            "slack_scope_history",
            bool(scopes & {"channels:history", "groups:history", "im:history", "mpim:history"}),
            "SLACK_BOT_SCOPES needs at least one thread history scope: channels:history, groups:history, im:history, or mpim:history",
        )
        if config.slack.download_attachments:
            require(
                "slack_scope_files_read",
                "files:read" in scopes,
                "SLACK_BOT_SCOPES is missing required scope: files:read",
            )
        if config.rollout_mode == "approved_pr":
            require(
                "slack_scope_files_write",
                "files:write" in scopes,
                "SLACK_BOT_SCOPES is missing required scope: files:write",
            )

    if config.rollout_mode not in {"dry_run", "linear_only", "local_fix_only", "approved_pr"}:
        failures.append(ReadinessIssue("rollout_mode", f"unknown rollout_mode: {config.rollout_mode}"))

    if config.rollout_mode in {"linear_only", "local_fix_only", "approved_pr"}:
        require(
            "loop_create_linear",
            config.loop.create_linear,
            f"loop.create_linear must be true in {config.rollout_mode} mode",
        )

    if config.rollout_mode in {"linear_only", "local_fix_only", "approved_pr"} and config.loop.create_linear:
        require("linear_api_key", _has_secret(env, "LINEAR_API_KEY"), "LINEAR_API_KEY is missing")
        require("linear_team_id", bool(config.linear.team_id), "linear.team_id is missing")

    if config.rollout_mode in {"local_fix_only", "approved_pr"}:
        require(
            "loop_require_fix_approval",
            config.loop.require_fix_approval,
            f"loop.require_fix_approval must be true in {config.rollout_mode} mode",
        )
        require(
            "worker_session_prefix",
            "monica" in config.runtime.worker_session_prefix.lower(),
            "runtime.worker_session_prefix must include `monica` to keep worker sessions segregated",
        )
        require("repo_url", bool(config.repo.url), "repo.url is missing")
        try:
            safe_repo_local_name(config.repo.local_name)
        except RepoManagerError as exc:
            failures.append(ReadinessIssue("repo_local_name", str(exc)))
        try:
            safe_branch_prefix(config.repo.branch_prefix)
        except RepoManagerError as exc:
            failures.append(ReadinessIssue("repo_branch_prefix", str(exc)))
        try:
            safe_default_branch(config.repo.default_branch)
        except RepoManagerError as exc:
            failures.append(ReadinessIssue("repo_default_branch", str(exc)))
        require(
            "verification_commands",
            _has_non_blank_values(config.verification.commands),
            "verification.commands is empty",
        )
        require("git_executable", resolve("git") is not None, "git executable was not found")
        cached_repo_issue = _cached_repo_status_issue(
            config=config,
            runtime_root_value=runtime_root_value,
            command_output=command_stdout,
        )
        if cached_repo_issue is not None:
            failures.append(cached_repo_issue)
        if config.rollout_mode == "approved_pr":
            require("gh_executable", resolve("gh") is not None, "gh executable was not found")
        if config.worker.backend == "codex_cli":
            require(
                "codex_executable",
                resolve(config.worker.codex_command) is not None,
                f"{config.worker.codex_command} executable was not found",
            )
            require(
                "codex_approval_policy",
                config.worker.codex_approval_policy == "never",
                f"worker.codex_approval_policy must be `never` for {config.rollout_mode} codex_cli runs",
            )
            require(
                "codex_sandbox",
                config.worker.codex_sandbox == "workspace-write",
                f"worker.codex_sandbox must be `workspace-write` for {config.rollout_mode} codex_cli runs",
            )
        elif config.worker.backend != "internal_agent":
            failures.append(ReadinessIssue("worker_backend", f"unknown worker.backend: {config.worker.backend}"))
        if config.rollout_mode == "approved_pr":
            warn(
                "github_token",
                _has_secret(env, "GITHUB_TOKEN"),
                "GITHUB_TOKEN is missing; this is okay only if gh is already authenticated",
            )
            require(
                "github_auth",
                _has_secret(env, "GITHUB_TOKEN")
                or not verify_commands
                or command_ok(("gh", "auth", "status")),
                "GITHUB_TOKEN is missing and `gh auth status` failed; Monica cannot open draft PRs",
            )
        proof_required = config.rollout_mode == "approved_pr" or config.proof.enabled or config.proof.required_for_done
        if proof_required:
            has_proof_commands = _has_non_blank_values(config.proof.commands)
            has_proof_setup_commands = _has_non_blank_values(config.proof.setup_commands)
            if config.rollout_mode == "approved_pr":
                require(
                    "proof_commands_empty",
                    has_proof_commands,
                    "proof.commands is empty; approved_pr cannot open a PR without simulator proof",
                )
                placeholder_proof_commands = _placeholder_proof_commands(config.proof.commands)
                require(
                    "proof_commands_placeholder",
                    not placeholder_proof_commands,
                    (
                        "proof.commands contains a placeholder; replace it with the "
                        f"real simulator proof command: {placeholder_proof_commands[0]}"
                    ) if placeholder_proof_commands else "",
                )
                secret_proof_assignments = _inline_secret_env_assignments(
                    config.proof.commands
                )
                require(
                    "proof_commands_inline_secret",
                    not secret_proof_assignments,
                    (
                        "proof.commands must not inline secret env assignment(s); "
                        "put credentials in the Monica profile .env instead: "
                        f"{', '.join(secret_proof_assignments)}"
                    ) if secret_proof_assignments else "",
                )
                literal_proof_secret_keys = _required_env_value_keys_in_commands(
                    config.proof.commands,
                    env=env,
                    required_env_keys=config.proof.required_env_keys,
                )
                require(
                    "proof_commands_literal_secret",
                    not literal_proof_secret_keys,
                    (
                        "proof.commands must not include literal values from "
                        "proof.required_env_keys; reference the environment key(s) "
                        f"instead: {', '.join(literal_proof_secret_keys)}"
                    ) if literal_proof_secret_keys else "",
                )
                noop_proof_commands = _noop_only_proof_commands(config.proof.commands)
                require(
                    "proof_commands_noop",
                    not noop_proof_commands,
                    (
                        "proof.commands contains only no-op commands; replace them with "
                        "the real simulator proof command: "
                        f"{', '.join(noop_proof_commands)}"
                    ) if noop_proof_commands else "",
                )
            else:
                warn(
                    "proof_commands_empty",
                    has_proof_commands,
                    "proof.commands is empty; Monica will block at proof after verification",
                )
            if config.rollout_mode == "approved_pr":
                require(
                    "proof_setup_commands_empty",
                    has_proof_setup_commands,
                    (
                        "proof.setup_commands is empty; protected marketplace/PDP proof needs "
                        "a test-auth or session seed command before simulator capture"
                    ),
                )
                placeholder_setup_commands = _placeholder_proof_setup_commands(
                    config.proof.setup_commands
                )
                require(
                    "proof_setup_commands_placeholder",
                    not placeholder_setup_commands,
                    (
                        "proof.setup_commands contains a placeholder; replace it with the "
                        f"real test-auth/session seed command: {placeholder_setup_commands[0]}"
                    ) if placeholder_setup_commands else "",
                )
                secret_setup_assignments = _inline_secret_env_assignments(
                    config.proof.setup_commands
                )
                require(
                    "proof_setup_commands_inline_secret",
                    not secret_setup_assignments,
                    (
                        "proof.setup_commands must not inline secret env assignment(s); "
                        "put credentials in the Monica profile .env instead: "
                        f"{', '.join(secret_setup_assignments)}"
                    ) if secret_setup_assignments else "",
                )
                literal_setup_secret_keys = _required_env_value_keys_in_commands(
                    config.proof.setup_commands,
                    env=env,
                    required_env_keys=config.proof.required_env_keys,
                )
                require(
                    "proof_setup_commands_literal_secret",
                    not literal_setup_secret_keys,
                    (
                        "proof.setup_commands must not include literal values from "
                        "proof.required_env_keys; reference the environment key(s) "
                        f"instead: {', '.join(literal_setup_secret_keys)}"
                    ) if literal_setup_secret_keys else "",
                )
                noop_setup_commands = _noop_only_proof_setup_commands(
                    config.proof.setup_commands
                )
                require(
                    "proof_setup_commands_noop",
                    not noop_setup_commands,
                    (
                        "proof.setup_commands contains only no-op commands; replace them with "
                        "the real test-auth/session seed command: "
                        f"{', '.join(noop_setup_commands)}"
                    ) if noop_setup_commands else "",
                )
                require(
                    "proof_required_env_keys_empty",
                    _has_non_blank_values(config.proof.required_env_keys),
                    (
                        "proof.required_env_keys is empty; protected marketplace/PDP proof "
                        "must declare the Monica profile .env key names used by setup/auth"
                    ),
                )
                invalid_required_env_keys = _invalid_required_env_keys(
                    config.proof.required_env_keys
                )
                require(
                    "proof_required_env_keys_invalid",
                    not invalid_required_env_keys,
                    (
                        "proof.required_env_keys must contain environment key names, "
                        "not KEY=value or invalid values: "
                        f"{', '.join(invalid_required_env_keys)}"
                    ) if invalid_required_env_keys else "",
                )
                missing_required_env_keys = (
                    []
                    if invalid_required_env_keys
                    else _missing_required_env_keys(env, config.proof.required_env_keys)
                )
                require(
                    "proof_required_env_keys",
                    not missing_required_env_keys,
                    (
                        "proof.required_env_keys are missing from the Monica profile environment: "
                        f"{', '.join(missing_required_env_keys)}"
                    ) if missing_required_env_keys else "",
                )
            proof_platforms = _normalized_proof_platforms(config.proof.platform_order)
            if config.rollout_mode == "approved_pr":
                missing_required_platforms = [
                    platform
                    for platform in ("ios", "android")
                    if platform not in proof_platforms
                ]
                require(
                    "proof_platform_order",
                    not missing_required_platforms,
                    (
                        "proof.platform_order must include both ios and android in approved_pr mode; "
                        f"missing {', '.join(missing_required_platforms)}"
                    ),
                )
            uses_builtin_simulator_proof = _uses_builtin_simulator_proof(config.proof.commands)
            if "ios" in proof_platforms:
                if uses_builtin_simulator_proof:
                    require(
                        "proof_ios_dev_client_scheme",
                        _proof_setting_configured(
                            config.proof.dev_client_scheme,
                            config.proof.commands,
                            cli_flag="--dev-client-scheme",
                            env_var="MONICA_DEV_CLIENT_SCHEME",
                        ),
                        (
                            "iOS simulator proof uses Monica's built-in Expo dev-client harness; "
                            "set proof.dev_client_scheme or pass --dev-client-scheme/"
                            "MONICA_DEV_CLIENT_SCHEME in proof.commands"
                        ),
                    )
                    require(
                        "proof_ios_bundle_id",
                        _proof_setting_configured(
                            config.proof.ios_bundle_id,
                            config.proof.commands,
                            cli_flag="--ios-bundle-id",
                            env_var="MONICA_IOS_BUNDLE_ID",
                        ),
                        (
                            "iOS simulator proof uses Monica's built-in Expo dev-client harness; "
                            "set proof.ios_bundle_id or pass --ios-bundle-id/"
                            "MONICA_IOS_BUNDLE_ID in proof.commands"
                        ),
                    )
                ios_tools_present = resolve("xcrun") is not None and resolve("xcodebuild") is not None
                ios_ready = ios_tools_present and (
                    not verify_commands
                    or (
                        command_ok(("xcrun", "--find", "simctl"))
                        and command_ok(("xcodebuild", "-version"))
                    )
                )
                warn(
                    "ios_proof_tooling",
                    ios_ready,
                    (
                        "iOS proof is configured but xcrun/simctl or xcodebuild was not found; "
                        "Monica will block at proof until a simulator is available"
                    ),
                )
            if "android" in proof_platforms:
                if uses_builtin_simulator_proof:
                    require(
                        "proof_android_package",
                        _proof_setting_configured(
                            config.proof.android_package,
                            config.proof.commands,
                            cli_flag="--android-package",
                            env_var="MONICA_ANDROID_PACKAGE",
                        ),
                        (
                            "Android simulator proof uses Monica's built-in Expo dev-client harness; "
                            "set proof.android_package or pass --android-package/"
                            "MONICA_ANDROID_PACKAGE in proof.commands"
                        ),
                    )
                warn(
                    "android_proof_tooling",
                    resolve("adb") is not None and resolve("emulator") is not None,
                    (
                        "Android proof is configured but adb or emulator was not found; "
                        "Monica will block at proof until an emulator is available"
                    ),
                )
        require(
            "slack_approvers_empty",
            bool(config.slack.approver_user_ids),
            "slack.approver_user_ids is empty; configure at least one Monica code approver",
        )
        handle_style_approvers = [
            user_id
            for user_id in config.slack.approver_user_ids
            if str(user_id).strip().startswith("@")
        ]
        require(
            "slack_approver_handle_value",
            not handle_style_approvers,
            (
                "slack.approver_user_ids must contain Slack user IDs like U123, "
                f"not handles like {handle_style_approvers[0]}"
            ) if handle_style_approvers else "",
        )
        invalid_approvers = [
            user_id
            for user_id in config.slack.approver_user_ids
            if not str(user_id).strip().startswith("@")
            and not _is_slack_user_id(user_id)
        ]
        require(
            "slack_approver_invalid",
            not invalid_approvers,
            (
                "slack.approver_user_ids must contain Slack user IDs like U123, "
                f"not invalid values like {invalid_approvers[0]}"
            ) if invalid_approvers else "",
        )

    return ReadinessReport(
        rollout_mode=config.rollout_mode,
        runtime_root_value=runtime_root_value,
        warnings=tuple(warnings),
        failures=tuple(failures),
    )


_APPROVAL_FAILURE_PRIORITY = (
    "config_enabled",
    "rollout_mode",
    "loop_create_linear",
    "loop_require_fix_approval",
    "worker_session_prefix",
    "runtime_root",
    "runtime_chandler",
    "linear_api_key",
    "linear_team_id",
    "repo_url",
    "repo_local_name",
    "repo_branch_prefix",
    "repo_default_branch",
    "repo_cached_path",
    "repo_cached_status",
    "repo_cached_dirty",
    "verification_commands",
    "slack_approvers_empty",
    "slack_approver_handle_value",
    "slack_approver_invalid",
    "git_executable",
    "gh_executable",
    "github_auth",
    "worker_backend",
    "codex_executable",
    "codex_approval_policy",
    "codex_sandbox",
    "slack_bot_token",
    "slack_sdk",
    "slack_bot_user_ids_empty",
    "slack_bot_id_value",
    "slack_bot_handle_value",
    "slack_bot_user_id_invalid",
    "slack_allowed_channels_empty",
    "slack_allowed_channel_invalid",
    "slack_scope_app_mentions",
    "slack_scope_chat_write",
    "slack_scope_history",
    "slack_scope_files_read",
    "proof_platform_order",
    "proof_commands_empty",
    "proof_commands_placeholder",
    "proof_commands_inline_secret",
    "proof_commands_literal_secret",
    "proof_commands_noop",
    "proof_setup_commands_empty",
    "proof_setup_commands_placeholder",
    "proof_setup_commands_inline_secret",
    "proof_setup_commands_literal_secret",
    "proof_setup_commands_noop",
    "proof_required_env_keys_empty",
    "proof_required_env_keys_invalid",
    "proof_required_env_keys",
    "proof_ios_dev_client_scheme",
    "proof_ios_bundle_id",
    "proof_android_package",
)


def _has_secret(env: dict[str, str], key: str) -> bool:
    return bool(str(env.get(key) or "").strip())


def _profile_env_values() -> dict[str, str]:
    path = get_env_path()
    if not path.is_file():
        return {}
    try:
        values = dotenv_values(path)
    except Exception:
        return {}
    return {
        str(key): str(value)
        for key, value in values.items()
        if key and value is not None
    }


def _has_any_secret(env: dict[str, str], keys: tuple[str, ...]) -> bool:
    return any(_has_secret(env, key) for key in keys)


def _missing_required_env_keys(env: dict[str, str], keys: tuple[str, ...]) -> list[str]:
    return [
        key
        for key in dict.fromkeys(key.strip() for key in keys if key.strip())
        if not _has_secret(env, key)
    ]


def _required_env_value_keys_in_commands(
    commands: tuple[str, ...],
    *,
    env: dict[str, str],
    required_env_keys: tuple[str, ...],
) -> list[str]:
    matches: list[str] = []
    required = tuple(dict.fromkeys(str(key or "").strip() for key in required_env_keys if str(key or "").strip()))
    if not required:
        return []
    for command in commands:
        try:
            parts = shlex.split(str(command or ""))
        except ValueError:
            parts = str(command or "").split()
        for key in required:
            secret = str(env.get(key) or "").strip()
            if not secret:
                continue
            if any(_command_part_contains_required_env_value(part, secret) for part in parts):
                matches.append(key)
    return sorted(dict.fromkeys(matches))


def _command_part_contains_required_env_value(part: str, secret: str) -> bool:
    value = str(part or "")
    if not value:
        return False
    if value == secret:
        return True
    if value.endswith(f"={secret}") or value.endswith(f":{secret}"):
        return True
    return len(secret) >= 4 and secret in value


def _invalid_required_env_keys(keys: tuple[str, ...]) -> list[str]:
    invalid: list[str] = []
    for key in dict.fromkeys(str(key or "").strip() for key in keys if str(key or "").strip()):
        if not _ENV_KEY_RE.fullmatch(key):
            invalid.append(_display_required_env_key_for_error(key))
            continue
        if key in _BUILTIN_PROOF_CONTEXT_ENV_KEYS:
            invalid.append(f"{key} (built-in Monica proof context)")
    return invalid


def _display_required_env_key_for_error(value: str) -> str:
    raw = str(value or "").strip()
    if "=" in raw:
        return raw.split("=", 1)[0].strip() or "<empty>"
    return raw or "<empty>"


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _command_succeeds(command: tuple[str, ...]) -> bool:
    try:
        proc = subprocess.run(
            command,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _command_output(command: tuple[str, ...]) -> str:
    try:
        proc = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(str(exc)) from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(detail or f"command failed ({proc.returncode})")
    return proc.stdout


def _cached_repo_status_issue(
    *,
    config: MonicaConfig,
    runtime_root_value: str,
    command_output: Callable[[tuple[str, ...]], str],
) -> ReadinessIssue | None:
    if not runtime_root_value or not config.repo.url:
        return None
    try:
        local_name = safe_repo_local_name(config.repo.local_name)
    except RepoManagerError:
        return None
    repo_path = Path(runtime_root_value) / "workspace" / "repos" / local_name
    if not repo_path.exists():
        return None
    if not repo_path.is_dir():
        return ReadinessIssue(
            "repo_cached_path",
            f"cached mobile repo path exists but is not a directory: {repo_path}",
        )
    try:
        status = command_output(("git", "-C", str(repo_path), "status", "--porcelain")).strip()
    except Exception as exc:
        return ReadinessIssue(
            "repo_cached_status",
            f"cached mobile repo status could not be checked: {repo_path}: {exc}",
        )
    if not status:
        return None
    lines = [line.strip() for line in status.splitlines() if line.strip()]
    summary = ", ".join(lines[:5])
    if len(lines) > 5:
        summary += f", +{len(lines) - 5} more"
    return ReadinessIssue(
        "repo_cached_dirty",
        (
            "cached mobile repo has uncommitted changes; fresh Monica runs will refuse "
            f"stale app code. Clean or archive the Monica cached repo before approving fixes: "
            f"{repo_path} ({summary})"
        ),
    )


def _uses_builtin_simulator_proof(commands: tuple[str, ...]) -> bool:
    return any("plugins.mobile_bug_agent.simulator_proof" in command for command in commands)


def _has_non_blank_values(values: tuple[str, ...]) -> bool:
    return any(str(value or "").strip() for value in values)


def _placeholder_proof_setup_commands(values: tuple[str, ...]) -> list[str]:
    return [
        str(value or "").strip()
        for value in values
        if _is_placeholder_proof_command(value, placeholders=_PLACEHOLDER_PROOF_SETUP_COMMANDS)
    ]


def _placeholder_proof_commands(values: tuple[str, ...]) -> list[str]:
    return [
        str(value or "").strip()
        for value in values
        if _is_placeholder_proof_command(value, placeholders=_PLACEHOLDER_PROOF_COMMANDS)
    ]


def _is_placeholder_proof_command(value: str, *, placeholders: set[str]) -> bool:
    command = " ".join(str(value or "").split()).strip("'\"`")
    normalized = command.casefold()
    if normalized in placeholders:
        return True
    return any(placeholder in normalized for placeholder in placeholders)


def _noop_only_proof_setup_commands(values: tuple[str, ...]) -> list[str]:
    return _noop_only_commands(values)


def _noop_only_proof_commands(values: tuple[str, ...]) -> list[str]:
    return _noop_only_commands(values)


def _noop_only_commands(values: tuple[str, ...]) -> list[str]:
    commands = [str(value or "").strip() for value in values if str(value or "").strip()]
    if not commands:
        return []
    noop_commands = [command for command in commands if _is_noop_shell_command(command)]
    if len(noop_commands) != len(commands):
        return []
    return noop_commands


def _is_noop_shell_command(value: str) -> bool:
    command = " ".join(str(value or "").split()).strip()
    if not command:
        return True
    if command.startswith("#"):
        return True
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    return _shell_parts_are_noop(parts)


def _shell_parts_are_noop(parts: list[str]) -> bool:
    parts = _strip_env_prefix(parts)
    if not parts:
        return True
    executable = os.path.basename(parts[0])
    if executable in {"sh", "bash", "zsh", "dash"}:
        for index, part in enumerate(parts[1:], start=1):
            if "c" in part.lstrip("-") and index + 1 < len(parts):
                return _is_noop_shell_command(parts[index + 1])
    if executable in {"true", ":"}:
        return True
    if executable == "exit":
        return len(parts) == 1 or parts[1] == "0"
    if executable == "sleep" and len(parts) == 2 and parts[1] in {"0", "0s"}:
        return True
    return False


def _strip_env_prefix(parts: list[str]) -> list[str]:
    remaining = list(parts)
    if remaining and os.path.basename(remaining[0]) == "env":
        remaining = remaining[1:]
    while remaining and _looks_like_shell_assignment(remaining[0]):
        remaining = remaining[1:]
    return remaining


def _looks_like_shell_assignment(value: str) -> bool:
    name, separator, _rest = value.partition("=")
    return bool(separator and name and _looks_like_env_name(name))


_SECRET_ENV_NAME_MARKERS = (
    "API_KEY",
    "APP_TOKEN",
    "BOT_TOKEN",
    "OTP",
    "PASSWORD",
    "SECRET",
    "SESSION",
    "TOKEN",
)


def _inline_secret_env_assignments(commands: tuple[str, ...]) -> list[str]:
    assignments: list[str] = []
    for command in commands:
        try:
            parts = shlex.split(command)
        except ValueError:
            parts = str(command or "").split()
        for part in parts:
            if "=" not in part or part.startswith("-"):
                continue
            name, value = part.split("=", 1)
            if not value or not _looks_like_env_name(name):
                continue
            normalized = name.upper()
            if any(marker in normalized for marker in _SECRET_ENV_NAME_MARKERS):
                assignments.append(name)
    return sorted(dict.fromkeys(assignments))


def _looks_like_env_name(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(value or "")))


_PLACEHOLDER_PROOF_SETUP_COMMANDS = {
    "<auth/session seed command>",
    "auth/session seed command",
    "<test-auth/session seed command>",
    "test-auth/session seed command",
    "none",
    "n/a",
    "na",
    "unknown",
    "unavailable",
}

_PLACEHOLDER_PROOF_COMMANDS = {
    "<simulator proof command>",
    "simulator proof command",
    "<proof command>",
    "proof command",
    "<capture proof command>",
    "capture proof command",
    "none",
    "n/a",
    "na",
    "unknown",
    "unavailable",
}


def _normalized_proof_platforms(values: tuple[str, ...]) -> set[str]:
    normalized: set[str] = set()
    for value in values:
        platform = str(value or "").strip().lower()
        if platform in {"iphone", "ipad", "ios-simulator"}:
            platform = "ios"
        elif platform == "android-emulator":
            platform = "android"
        if platform:
            normalized.add(platform)
    return normalized


def _proof_setting_configured(
    config_value: str | None,
    commands: tuple[str, ...],
    *,
    cli_flag: str,
    env_var: str,
) -> bool:
    if str(config_value or "").strip():
        return True
    assignment_re = re.compile(rf"(?:^|[\s;])(?:export\s+)?{re.escape(env_var)}\s*=")
    return any(cli_flag in command or assignment_re.search(command) for command in commands)


def _scope_set(value: object) -> set[str]:
    return {
        part.strip()
        for part in str(value or "").replace("\n", ",").replace(" ", ",").split(",")
        if part.strip()
    }


def _is_slack_channel_id(value: str) -> bool:
    return bool(_SLACK_CHANNEL_ID_RE.fullmatch(str(value or "").strip()))


def _is_slack_user_id(value: str) -> bool:
    return bool(_SLACK_USER_ID_RE.fullmatch(str(value or "").strip()))


def _runtime_path_mentions_chandler(value: str) -> bool:
    return any(
        "chandler" in part.lower()
        for part in re.split(r"[\\/]+", str(value or ""))
        if part
    )
