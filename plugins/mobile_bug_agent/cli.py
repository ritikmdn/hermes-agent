from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import MonicaConfig, load_monica_config, runtime_root
from .linear_client import LinearClient, LinearClientError
from .loop import MonicaLoop
from .readiness import (
    check_monica_readiness,
    _invalid_required_env_keys,
    _inline_secret_env_assignments,
    _is_slack_channel_id,
    _is_slack_user_id,
    _noop_only_proof_commands,
    _noop_only_proof_setup_commands,
    _placeholder_proof_commands,
    _placeholder_proof_setup_commands,
    _proof_setting_configured,
    _uses_builtin_simulator_proof,
)
from .repo_manager import (
    RepoManagerError,
    safe_branch_prefix,
    safe_default_branch,
    safe_repo_local_name,
)
from .secrets import MONICA_SLACK_BOT_TOKEN, monica_slack_bot_token
from .skills import DefaultMonicaSkills
from .slack_client import SlackClientError, SlackThreadClient
from .slack_flow import is_approval_text
from .state import MonicaState, RUNTIME_SYNC_BLOCKING_STATUSES

RETRYABLE_STATUSES = {"blocked", "failed", "needs_clarification", "proof_blocked", "proofing"}
_SIMULATION_THREAD_SEQUENCE = 0
_MONICA_PLUGIN_NAME = "mobile-bug-agent"
_GATEWAY_STATUS_STALE_AFTER_SECONDS = 300
_SLACK_MENTION_RE = re.compile(r"<@([A-Z0-9_]+)(?:\|[^>]+)?>")


def register_cli(subparser: argparse.ArgumentParser) -> None:
    subs = subparser.add_subparsers(dest="mobile_bug_agent_action")

    status_p = subs.add_parser("status", help="List recent Monica runs")
    status_p.add_argument("--limit", type=int, default=20)
    status_p.add_argument("--json", action="store_true", help="Print recent runs as JSON")

    doctor_p = subs.add_parser("doctor", help="Check Monica rollout readiness")
    doctor_p.add_argument(
        "--rollout-mode",
        choices=("dry_run", "linear_only", "local_fix_only", "approved_pr"),
        default="",
        help="Preflight a rollout mode without changing config",
    )
    doctor_p.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable readiness JSON",
    )

    setup_plan_p = subs.add_parser(
        "setup-plan",
        help="Print ordered Monica rollout setup steps",
    )
    setup_plan_p.set_defaults(mobile_bug_agent_action="setup_plan")
    setup_plan_p.add_argument(
        "--rollout-mode",
        choices=("dry_run", "linear_only", "local_fix_only", "approved_pr"),
        default="",
        help="Plan setup for a rollout mode without changing config",
    )
    setup_plan_p.add_argument("--json", action="store_true", help="Print setup plan as JSON")

    manifest_p = subs.add_parser(
        "slack-manifest",
        help="Print a Slack App Manifest for Monica setup",
    )
    manifest_p.set_defaults(mobile_bug_agent_action="slack_manifest")
    manifest_p.add_argument("--app-name", default="Monica")
    manifest_p.add_argument("--bot-display-name", default="monica")

    slack_metadata_p = subs.add_parser(
        "slack-metadata",
        help="List Slack bot identity and channel IDs for Monica setup",
    )
    slack_metadata_p.set_defaults(mobile_bug_agent_action="slack_metadata")
    slack_metadata_p.add_argument(
        "--json",
        action="store_true",
        help="Print Slack metadata as JSON",
    )

    linear_metadata_p = subs.add_parser(
        "linear-metadata",
        help="List Linear team/project/label IDs for Monica setup",
    )
    linear_metadata_p.set_defaults(mobile_bug_agent_action="linear_metadata")
    linear_metadata_p.add_argument(
        "--json",
        action="store_true",
        help="Print Linear metadata as JSON",
    )

    configure_dry_run_p = subs.add_parser(
        "configure-dry-run",
        help="Persist non-secret Monica settings for dry-run Slack rollout",
    )
    configure_dry_run_p.set_defaults(mobile_bug_agent_action="configure_dry_run")
    configure_dry_run_p.add_argument(
        "--bot-user-id",
        dest="bot_user_ids",
        action="append",
        default=[],
        help="Optional Monica Slack mention user ID, e.g. U012ABCDEF",
    )
    configure_dry_run_p.add_argument(
        "--channel-id",
        dest="channel_ids",
        action="append",
        default=[],
        help="Optional private dry-run Slack channel ID, e.g. C012ABCDEF or G012ABCDEF",
    )

    configure_linear_p = subs.add_parser(
        "configure-linear-only",
        help="Persist non-secret Monica settings for Linear-only rollout",
    )
    configure_linear_p.set_defaults(mobile_bug_agent_action="configure_linear_only")
    configure_linear_p.add_argument(
        "--bot-user-id",
        dest="bot_user_ids",
        action="append",
        required=True,
        help="Monica Slack mention user ID, e.g. U012ABCDEF",
    )
    configure_linear_p.add_argument(
        "--channel-id",
        dest="channel_ids",
        action="append",
        required=True,
        help="Allowed Slack channel ID, e.g. C012ABCDEF or G012ABCDEF",
    )
    configure_linear_p.add_argument(
        "--linear-team-id",
        required=True,
        help="Linear team ID for mobile app issues",
    )
    configure_linear_p.add_argument(
        "--linear-project-id",
        default="",
        help="Optional Linear project ID",
    )
    configure_linear_p.add_argument(
        "--linear-label-id",
        dest="linear_label_ids",
        action="append",
        default=[],
        help="Optional Linear label ID; repeat for multiple labels",
    )

    configure_pr_p = subs.add_parser(
        "configure-approved-pr",
        help="Persist non-secret Monica settings for approved draft PR rollout",
    )
    configure_pr_p.set_defaults(mobile_bug_agent_action="configure_approved_pr")
    configure_pr_p.add_argument(
        "--approver-user-id",
        dest="approver_user_ids",
        action="append",
        required=True,
        help="Slack user ID allowed to approve Monica code work, e.g. U012ABCDEF",
    )
    configure_pr_p.add_argument(
        "--repo-url",
        required=True,
        help="React Native app repository URL",
    )
    configure_pr_p.add_argument(
        "--verification-command",
        dest="verification_commands",
        action="append",
        required=True,
        help="Command Monica must run before opening a draft PR; repeat for multiple commands",
    )
    configure_pr_p.add_argument(
        "--proof-setup-command",
        dest="proof_setup_commands",
        action="append",
        default=[],
        help="Command Monica runs before simulator proof to seed auth/session state; repeat for multiple commands",
    )
    configure_pr_p.add_argument(
        "--proof-command",
        dest="proof_commands",
        action="append",
        default=[],
        help="Command Monica runs to capture iOS and Android simulator proof; repeat for multiple commands",
    )
    configure_pr_p.add_argument(
        "--proof-required-env-key",
        dest="proof_required_env_keys",
        action="append",
        default=[],
        help="Non-secret environment key name required by simulator proof; repeat for multiple keys",
    )
    configure_pr_p.add_argument(
        "--proof-dev-client-scheme",
        default="",
        help="Expo dev-client URL scheme used by Monica's built-in simulator proof harness",
    )
    configure_pr_p.add_argument(
        "--proof-ios-bundle-id",
        default="",
        help="iOS bundle identifier for simulator launch/proof setup",
    )
    configure_pr_p.add_argument(
        "--proof-ios-simulator-udid",
        default="",
        help="Optional iOS simulator UDID for proof setup/capture",
    )
    configure_pr_p.add_argument(
        "--proof-android-serial",
        default="",
        help="Optional adb serial for Android proof setup/capture",
    )
    configure_pr_p.add_argument(
        "--proof-android-avd",
        default="",
        help="Optional Android AVD name for proof setup/capture",
    )
    configure_pr_p.add_argument(
        "--proof-android-package",
        default="",
        help="Android package name for emulator launch/proof setup",
    )
    configure_pr_p.add_argument(
        "--proof-timeout-minutes",
        type=int,
        default=0,
        help="Optional proof command timeout in minutes",
    )
    configure_pr_p.add_argument(
        "--proof-artifact-dir",
        default="",
        help="Optional proof artifact directory relative to Monica runtime root",
    )
    configure_pr_p.add_argument("--repo-local-name", default="mobile-app")
    configure_pr_p.add_argument("--default-branch", default="main")
    configure_pr_p.add_argument("--branch-prefix", default="monica")

    configure_proof_setup_p = subs.add_parser(
        "configure-proof-setup",
        help="Persist Monica proof setup/auth commands without changing rollout settings",
    )
    configure_proof_setup_p.set_defaults(mobile_bug_agent_action="configure_proof_setup")
    configure_proof_setup_p.add_argument(
        "--proof-setup-command",
        dest="proof_setup_commands",
        action="append",
        required=True,
        help="Command Monica runs before simulator proof to seed auth/session state; repeat for multiple commands",
    )
    configure_proof_setup_p.add_argument(
        "--proof-required-env-key",
        dest="proof_required_env_keys",
        action="append",
        default=[],
        help="Non-secret environment key name required by simulator proof; repeat for multiple keys",
    )

    configure_local_fix_p = subs.add_parser(
        "configure-local-fix-only",
        help="Persist non-secret Monica settings for local fix rollout without push or PR",
    )
    configure_local_fix_p.set_defaults(mobile_bug_agent_action="configure_local_fix_only")
    configure_local_fix_p.add_argument(
        "--approver-user-id",
        dest="approver_user_ids",
        action="append",
        required=True,
        help="Slack user ID allowed to approve Monica code work, e.g. U012ABCDEF",
    )
    configure_local_fix_p.add_argument(
        "--repo-url",
        required=True,
        help="React Native app repository URL",
    )
    configure_local_fix_p.add_argument(
        "--verification-command",
        dest="verification_commands",
        action="append",
        required=True,
        help="Command Monica must run before marking a local fix ready; repeat for multiple commands",
    )
    configure_local_fix_p.add_argument("--repo-local-name", default="mobile-app")
    configure_local_fix_p.add_argument("--default-branch", default="main")
    configure_local_fix_p.add_argument("--branch-prefix", default="monica")

    show_p = subs.add_parser("show", help="Show one Monica run")
    show_p.add_argument("run_id")
    show_p.add_argument("--json", action="store_true", help="Print the run as JSON")

    retry_p = subs.add_parser("retry", help="Retry a Monica run from its last safe gate")
    retry_p.add_argument("run_id")
    retry_p.add_argument("--json", action="store_true", help="Print retry result as JSON")

    approve_p = subs.add_parser("approve", help="Approve a Monica run locally and continue the fix loop")
    approve_p.add_argument("run_id")
    approve_p.add_argument("--user-id", default="local-operator")
    approve_p.add_argument("--json", action="store_true", help="Print approval result as JSON")

    sync_approvals_p = subs.add_parser(
        "sync-approvals",
        help="Recover Slack approvals that arrived while the Monica gateway was offline",
    )
    sync_approvals_p.set_defaults(mobile_bug_agent_action="sync_approvals")
    sync_approvals_p.add_argument("--run-id", default="", help="Only sync one awaiting run")
    sync_approvals_p.add_argument("--limit", type=int, default=20)
    sync_approvals_p.add_argument("--json", action="store_true", help="Print sync result as JSON")

    simulate_p = subs.add_parser("simulate", help="Run a local Monica message simulation")
    simulate_p.add_argument("text", nargs="+")
    simulate_p.add_argument("--channel-id", default="LOCAL_MONICA_SIM")
    simulate_p.add_argument("--user-id", default="local-operator")
    simulate_p.add_argument("--thread-ts", default="")
    simulate_p.add_argument(
        "--allow-side-effects",
        action="store_true",
        help="Allow simulate to run real Linear/PR side effects when rollout_mode is not dry_run",
    )
    simulate_p.add_argument("--json", action="store_true", help="Print simulation result as JSON")

    subparser.set_defaults(func=mobile_bug_agent_command)


def mobile_bug_agent_command(args: argparse.Namespace) -> int:
    action = getattr(args, "mobile_bug_agent_action", None)
    config = load_monica_config()
    if action == "doctor":
        return run_doctor_command(
            config=config,
            target_rollout_mode=str(getattr(args, "rollout_mode", "") or ""),
            json_output=bool(getattr(args, "json", False)),
            plugin_config=_load_hermes_config_mapping(),
        )
    if action == "setup_plan":
        return run_setup_plan_command(
            config=config,
            target_rollout_mode=str(getattr(args, "rollout_mode", "") or ""),
            json_output=bool(getattr(args, "json", False)),
            plugin_config=_load_hermes_config_mapping(),
        )
    if action == "slack_manifest":
        return run_slack_manifest_command(
            config=config,
            app_name=str(getattr(args, "app_name", "") or ""),
            bot_display_name=str(getattr(args, "bot_display_name", "") or ""),
        )
    if action == "slack_metadata":
        return run_slack_metadata_command(json_output=bool(getattr(args, "json", False)))
    if action == "linear_metadata":
        return run_linear_metadata_command(json_output=bool(getattr(args, "json", False)))
    if action == "configure_dry_run":
        return run_configure_dry_run_command(
            bot_user_ids=tuple(getattr(args, "bot_user_ids", ()) or ()),
            channel_ids=tuple(getattr(args, "channel_ids", ()) or ()),
        )
    if action == "configure_linear_only":
        return run_configure_linear_only_command(
            bot_user_ids=tuple(getattr(args, "bot_user_ids", ()) or ()),
            channel_ids=tuple(getattr(args, "channel_ids", ()) or ()),
            linear_team_id=str(getattr(args, "linear_team_id", "") or ""),
            linear_project_id=str(getattr(args, "linear_project_id", "") or ""),
            linear_label_ids=tuple(getattr(args, "linear_label_ids", ()) or ()),
        )
    if action == "configure_approved_pr":
        return run_configure_approved_pr_command(
            approver_user_ids=tuple(getattr(args, "approver_user_ids", ()) or ()),
            repo_url=str(getattr(args, "repo_url", "") or ""),
            verification_commands=tuple(getattr(args, "verification_commands", ()) or ()),
            proof_setup_commands=tuple(getattr(args, "proof_setup_commands", ()) or ()),
            proof_commands=tuple(getattr(args, "proof_commands", ()) or ()),
            proof_required_env_keys=tuple(getattr(args, "proof_required_env_keys", ()) or ()),
            proof_dev_client_scheme=str(getattr(args, "proof_dev_client_scheme", "") or ""),
            proof_ios_bundle_id=str(getattr(args, "proof_ios_bundle_id", "") or ""),
            proof_ios_simulator_udid=str(getattr(args, "proof_ios_simulator_udid", "") or ""),
            proof_android_serial=str(getattr(args, "proof_android_serial", "") or ""),
            proof_android_avd=str(getattr(args, "proof_android_avd", "") or ""),
            proof_android_package=str(getattr(args, "proof_android_package", "") or ""),
            proof_timeout_minutes=int(getattr(args, "proof_timeout_minutes", 0) or 0),
            proof_artifact_dir=str(getattr(args, "proof_artifact_dir", "") or ""),
            repo_local_name=str(getattr(args, "repo_local_name", "") or ""),
            default_branch=str(getattr(args, "default_branch", "") or ""),
            branch_prefix=str(getattr(args, "branch_prefix", "") or ""),
        )
    if action == "configure_proof_setup":
        return run_configure_proof_setup_command(
            proof_setup_commands=tuple(getattr(args, "proof_setup_commands", ()) or ()),
            proof_required_env_keys=tuple(getattr(args, "proof_required_env_keys", ()) or ()),
        )
    if action == "configure_local_fix_only":
        return run_configure_local_fix_only_command(
            approver_user_ids=tuple(getattr(args, "approver_user_ids", ()) or ()),
            repo_url=str(getattr(args, "repo_url", "") or ""),
            verification_commands=tuple(getattr(args, "verification_commands", ()) or ()),
            repo_local_name=str(getattr(args, "repo_local_name", "") or ""),
            default_branch=str(getattr(args, "default_branch", "") or ""),
            branch_prefix=str(getattr(args, "branch_prefix", "") or ""),
        )

    try:
        state = _open_state(config)
    except ValueError as exc:
        print(f"Monica runtime is not configured correctly: {exc}")
        return 1
    if action == "status":
        return run_status_command(
            state=state,
            limit=int(getattr(args, "limit", 20)),
            json_output=bool(getattr(args, "json", False)),
        )
    if action == "show":
        return run_show_command(
            state=state,
            run_id=str(args.run_id),
            json_output=bool(getattr(args, "json", False)),
        )
    if action == "retry":
        return run_retry_command(
            state=state,
            run_id=str(args.run_id),
            config=config,
            readiness_checker=_cli_readiness_checker(config),
            json_output=bool(getattr(args, "json", False)),
        )
    if action == "approve":
        return run_approve_command(
            state=state,
            run_id=str(args.run_id),
            user_id=str(getattr(args, "user_id", "") or "local-operator"),
            config=config,
            readiness_checker=_cli_readiness_checker(config),
            json_output=bool(getattr(args, "json", False)),
        )
    if action == "sync_approvals":
        return run_sync_approvals_command(
            config=config,
            state=state,
            run_id=str(getattr(args, "run_id", "") or ""),
            limit=int(getattr(args, "limit", 20)),
            readiness_checker=_cli_readiness_checker(config),
            json_output=bool(getattr(args, "json", False)),
        )
    if action == "simulate":
        return run_simulate_command(
            config=config,
            state=state,
            text=" ".join(getattr(args, "text", []) or []),
            channel_id=str(getattr(args, "channel_id", "") or "LOCAL_MONICA_SIM"),
            user_id=str(getattr(args, "user_id", "") or "local-operator"),
            thread_ts=str(getattr(args, "thread_ts", "") or ""),
            allow_side_effects=bool(getattr(args, "allow_side_effects", False)),
            json_output=bool(getattr(args, "json", False)),
            readiness_checker=_cli_readiness_checker(config),
        )
    print(
        "Usage: hermes mobile-bug-agent "
        "{status|doctor|setup-plan|configure-dry-run|configure-linear-only|configure-local-fix-only|"
        "configure-approved-pr|configure-proof-setup|"
        "show|retry|approve|sync-approvals|simulate}"
    )
    return 2


def run_configure_linear_only_command(
    *,
    bot_user_ids: tuple[str, ...],
    channel_ids: tuple[str, ...],
    linear_team_id: str,
    linear_project_id: str = "",
    linear_label_ids: tuple[str, ...] = (),
    load_config_fn: Any | None = None,
    save_config_fn: Any | None = None,
) -> int:
    bot_ids = _clean_sequence(bot_user_ids)
    channels = _clean_sequence(channel_ids)
    team_id = str(linear_team_id or "").strip()
    project_id = str(linear_project_id or "").strip()
    label_ids = _clean_sequence(linear_label_ids)

    errors = _linear_only_config_errors(
        bot_user_ids=bot_ids,
        channel_ids=channels,
        linear_team_id=team_id,
    )
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        print("Monica linear_only configuration was not saved.")
        return 1

    if load_config_fn is None or save_config_fn is None:
        from hermes_cli.config import load_config, save_config

        load = load_config if load_config_fn is None else load_config_fn
        save = save_config if save_config_fn is None else save_config_fn
    else:
        load = load_config_fn
        save = save_config_fn

    config = load()
    if not isinstance(config, dict):
        config = {}
    _enable_plugin(config)

    monica_cfg = _ensure_dict(config, "mobile_bug_agent")
    monica_cfg["enabled"] = True
    monica_cfg["rollout_mode"] = "linear_only"
    monica_cfg["dry_run"] = False

    slack_cfg = _ensure_dict(monica_cfg, "slack")
    slack_cfg["bot_user_ids"] = list(bot_ids)
    slack_cfg["allowed_channels"] = list(channels)

    loop_cfg = _ensure_dict(monica_cfg, "loop")
    loop_cfg["create_linear"] = True

    linear_cfg = _ensure_dict(monica_cfg, "linear")
    linear_cfg["team_id"] = team_id
    linear_cfg["project_id"] = project_id
    linear_cfg["label_ids"] = list(label_ids)

    save(config)
    print("Configured Monica for linear_only rollout.")
    print(
        "Secrets were not written; keep MONICA_SLACK_BOT_TOKEN and LINEAR_API_KEY in the Monica profile .env."
    )
    print("Run `hermes mobile-bug-agent doctor --rollout-mode linear_only` before inviting Monica.")
    return 0


def run_configure_dry_run_command(
    *,
    bot_user_ids: tuple[str, ...] = (),
    channel_ids: tuple[str, ...] = (),
    load_config_fn: Any | None = None,
    save_config_fn: Any | None = None,
) -> int:
    bot_ids = _clean_sequence(bot_user_ids)
    channels = _clean_sequence(channel_ids)

    errors = _slack_scope_config_errors(
        bot_user_ids=bot_ids,
        channel_ids=channels,
    )
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        print("Monica dry_run configuration was not saved.")
        return 1

    if load_config_fn is None or save_config_fn is None:
        from hermes_cli.config import load_config, save_config

        load = load_config if load_config_fn is None else load_config_fn
        save = save_config if save_config_fn is None else save_config_fn
    else:
        load = load_config_fn
        save = save_config_fn

    config = load()
    if not isinstance(config, dict):
        config = {}
    _enable_plugin(config)

    monica_cfg = _ensure_dict(config, "mobile_bug_agent")
    monica_cfg["enabled"] = True
    monica_cfg["rollout_mode"] = "dry_run"
    monica_cfg["dry_run"] = True

    if bot_ids or channels:
        slack_cfg = _ensure_dict(monica_cfg, "slack")
        if bot_ids:
            slack_cfg["bot_user_ids"] = list(bot_ids)
        if channels:
            slack_cfg["allowed_channels"] = list(channels)

    loop_cfg = _ensure_dict(monica_cfg, "loop")
    loop_cfg["create_linear"] = True
    loop_cfg["require_fix_approval"] = True

    runtime_cfg = _ensure_dict(monica_cfg, "runtime")
    runtime_cfg["home_subdir"] = "agents/monica"
    runtime_cfg["worker_session_prefix"] = "monica"
    runtime_cfg["skip_memory"] = True

    save(config)
    print("Configured Monica for dry_run rollout.")
    print("Secrets were not written; keep MONICA_SLACK_BOT_TOKEN in the Monica profile .env.")
    print("Run `hermes mobile-bug-agent doctor --rollout-mode dry_run` before inviting Monica.")
    return 0


def run_slack_manifest_command(
    *,
    config: MonicaConfig,
    app_name: str = "Monica",
    bot_display_name: str = "monica",
) -> int:
    manifest = _slack_app_manifest(
        config=config,
        app_name=app_name,
        bot_display_name=bot_display_name,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


def run_linear_metadata_command(
    *,
    api_key: str | None = None,
    json_output: bool = False,
    client_factory: Any = LinearClient,
) -> int:
    key = str(api_key if api_key is not None else os.environ.get("LINEAR_API_KEY", "")).strip()
    if not key:
        message = "LINEAR_API_KEY is missing; keep it in ~/.hermes/.env before listing Linear IDs."
        if json_output:
            _print_linear_metadata_error_json("linear_api_key_missing", message)
            return 1
        print(message)
        return 1
    try:
        metadata = client_factory(api_key=key).list_workspace_metadata()
    except LinearClientError as exc:
        message = str(exc)
        if json_output:
            _print_linear_metadata_error_json("linear_request_failed", message)
            return 1
        print(f"Could not list Linear metadata: {message}")
        return 1
    if json_output:
        print(json.dumps(_linear_metadata_payload(metadata), sort_keys=True))
        return 0
    _print_linear_metadata_text(metadata)
    return 0


def _linear_metadata_payload(metadata: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "teams": [
            {
                "id": str(getattr(team, "id", "") or ""),
                "key": str(getattr(team, "key", "") or ""),
                "name": str(getattr(team, "name", "") or ""),
            }
            for team in getattr(metadata, "teams", ()) or ()
        ],
        "projects": [
            {
                "id": str(getattr(project, "id", "") or ""),
                "name": str(getattr(project, "name", "") or ""),
                "state": str(getattr(project, "state", "") or ""),
            }
            for project in getattr(metadata, "projects", ()) or ()
        ],
        "labels": [
            {
                "id": str(getattr(label, "id", "") or ""),
                "name": str(getattr(label, "name", "") or ""),
                "color": str(getattr(label, "color", "") or ""),
            }
            for label in getattr(metadata, "labels", ()) or ()
        ],
    }


def _print_linear_metadata_text(metadata: Any) -> None:
    print("Linear metadata for Monica setup")
    print("")
    print("Teams:")
    teams = getattr(metadata, "teams", ()) or ()
    if teams:
        for team in teams:
            key = str(getattr(team, "key", "") or "")
            key_suffix = f" [{key}]" if key else ""
            print(f"- {getattr(team, 'name', '')}{key_suffix}: {getattr(team, 'id', '')}")
    else:
        print("- none returned")
    print("")
    print("Projects:")
    projects = getattr(metadata, "projects", ()) or ()
    if projects:
        for project in projects:
            state = str(getattr(project, "state", "") or "")
            state_suffix = f" ({state})" if state else ""
            print(f"- {getattr(project, 'name', '')}{state_suffix}: {getattr(project, 'id', '')}")
    else:
        print("- none returned")
    print("")
    print("Labels:")
    labels = getattr(metadata, "labels", ()) or ()
    if labels:
        for label in labels:
            print(f"- {getattr(label, 'name', '')}: {getattr(label, 'id', '')}")
    else:
        print("- none returned")
    print("")
    print(
        "Use the chosen team ID with `hermes mobile-bug-agent configure-linear-only "
        "--linear-team-id <id>`."
    )


def _print_linear_metadata_error_json(code: str, message: str) -> None:
    print(
        json.dumps(
            {
                "ok": False,
                "error": {
                    "code": code,
                    "message": message,
                },
            },
            sort_keys=True,
        )
    )


def run_slack_metadata_command(
    *,
    token: str | None = None,
    json_output: bool = False,
    client_factory: Any = SlackThreadClient.from_token,
) -> int:
    bot_token = str(token if token is not None else monica_slack_bot_token()).strip()
    if not bot_token:
        message = (
            f"{MONICA_SLACK_BOT_TOKEN} is missing; keep it in the Monica profile .env "
            "before listing Slack IDs."
        )
        if json_output:
            _print_slack_metadata_error_json("slack_bot_token_missing", message)
            return 1
        print(message)
        return 1
    try:
        metadata = client_factory(token=bot_token).list_workspace_metadata()
    except SlackClientError as exc:
        message = str(exc)
        if json_output:
            _print_slack_metadata_error_json("slack_request_failed", message)
            return 1
        print(f"Could not list Slack metadata: {message}")
        return 1
    if json_output:
        print(json.dumps(_slack_metadata_payload(metadata), sort_keys=True))
        return 0
    _print_slack_metadata_text(metadata)
    return 0


def _slack_metadata_payload(metadata: Any) -> dict[str, Any]:
    auth = getattr(metadata, "auth", None)
    return {
        "ok": True,
        "auth": {
            "bot_user_id": str(getattr(auth, "bot_user_id", "") or ""),
            "bot_id": str(getattr(auth, "bot_id", "") or ""),
            "team_id": str(getattr(auth, "team_id", "") or ""),
            "team_name": str(getattr(auth, "team_name", "") or ""),
            "team_url": str(getattr(auth, "team_url", "") or ""),
        },
        "channels": [
            {
                "id": str(getattr(channel, "id", "") or ""),
                "name": str(getattr(channel, "name", "") or ""),
                "is_private": bool(getattr(channel, "is_private", False)),
                "is_member": bool(getattr(channel, "is_member", False)),
                "is_archived": bool(getattr(channel, "is_archived", False)),
            }
            for channel in getattr(metadata, "channels", ()) or ()
        ],
    }


def _print_slack_metadata_text(metadata: Any) -> None:
    auth = getattr(metadata, "auth", None)
    print("Slack metadata for Monica setup")
    print("")
    print(f"Workspace: {getattr(auth, 'team_name', '') or '(unknown)'}")
    print(f"Team ID: {getattr(auth, 'team_id', '') or '(unknown)'}")
    print(f"Bot user ID: {getattr(auth, 'bot_user_id', '') or '(unknown)'}")
    bot_id = str(getattr(auth, "bot_id", "") or "")
    if bot_id:
        print(f"Bot ID: {bot_id} (do not use this for slack.bot_user_ids)")
    print("")
    print("Channels visible to Monica:")
    channels = getattr(metadata, "channels", ()) or ()
    if channels:
        for channel in channels:
            privacy = "private" if getattr(channel, "is_private", False) else "public"
            membership = "joined" if getattr(channel, "is_member", False) else "not joined"
            print(
                f"- #{getattr(channel, 'name', '')} ({privacy}, {membership}): "
                f"{getattr(channel, 'id', '')}"
            )
    else:
        print("- none returned")
    print("")
    print(
        "Use the bot user ID and selected channel IDs with "
        "`hermes mobile-bug-agent configure-linear-only --bot-user-id <U...> --channel-id <C...>`."
    )


def _print_slack_metadata_error_json(code: str, message: str) -> None:
    print(
        json.dumps(
            {
                "ok": False,
                "error": {
                    "code": code,
                    "message": message,
                },
            },
            sort_keys=True,
        )
    )


def _slack_app_manifest(
    *,
    config: MonicaConfig,
    app_name: str,
    bot_display_name: str,
) -> dict[str, Any]:
    scopes = [
        "app_mentions:read",
        "channels:history",
        "groups:history",
        "im:history",
        "im:read",
        "chat:write",
    ]
    if config.slack.download_attachments:
        scopes.append("files:read")
    scopes.append("files:write")
    return {
        "display_information": {
            "name": str(app_name or "").strip() or "Monica",
        },
        "features": {
            "app_home": {
                "home_tab_enabled": False,
                "messages_tab_enabled": True,
                "messages_tab_read_only_enabled": False,
            },
            "bot_user": {
                "display_name": str(bot_display_name or "").strip() or "monica",
                "always_online": True,
            },
        },
        "oauth_config": {
            "scopes": {
                "bot": scopes,
            },
        },
        "settings": {
            "event_subscriptions": {
                "bot_events": ["app_mention", "message.im"],
            },
            "interactivity": {
                "is_enabled": False,
            },
            "org_deploy_enabled": False,
            "socket_mode_enabled": True,
            "token_rotation_enabled": False,
        },
    }


def run_configure_approved_pr_command(
    *,
    approver_user_ids: tuple[str, ...],
    repo_url: str,
    verification_commands: tuple[str, ...],
    proof_setup_commands: tuple[str, ...] = (),
    proof_commands: tuple[str, ...] = (),
    proof_required_env_keys: tuple[str, ...] = (),
    proof_dev_client_scheme: str = "",
    proof_ios_bundle_id: str = "",
    proof_ios_simulator_udid: str = "",
    proof_android_serial: str = "",
    proof_android_avd: str = "",
    proof_android_package: str = "",
    proof_timeout_minutes: int = 0,
    proof_artifact_dir: str = "",
    repo_local_name: str = "mobile-app",
    default_branch: str = "main",
    branch_prefix: str = "monica",
    load_config_fn: Any | None = None,
    save_config_fn: Any | None = None,
) -> int:
    approvers = _clean_sequence(approver_user_ids)
    commands = _clean_sequence(verification_commands)
    setup_commands = _clean_sequence(proof_setup_commands)
    proof_command_list = _clean_sequence(proof_commands)
    required_env_keys = _clean_required_env_key_sequence(proof_required_env_keys)
    proof_environment = {
        "dev_client_scheme": str(proof_dev_client_scheme or "").strip(),
        "ios_bundle_id": str(proof_ios_bundle_id or "").strip(),
        "ios_simulator_udid": str(proof_ios_simulator_udid or "").strip(),
        "android_serial": str(proof_android_serial or "").strip(),
        "android_avd": str(proof_android_avd or "").strip(),
        "android_package": str(proof_android_package or "").strip(),
        "artifact_dir": str(proof_artifact_dir or "").strip(),
    }
    repo_url_value = str(repo_url or "").strip()
    local_name = str(repo_local_name or "mobile-app").strip() or "mobile-app"
    branch = str(default_branch or "main").strip() or "main"
    prefix = str(branch_prefix or "monica").strip() or "monica"

    errors = _approved_pr_config_errors(
        approver_user_ids=approvers,
        repo_url=repo_url_value,
        verification_commands=commands,
        proof_setup_commands=setup_commands,
        proof_commands=proof_command_list,
        proof_required_env_keys=required_env_keys,
        proof_dev_client_scheme=proof_environment["dev_client_scheme"],
        proof_ios_bundle_id=proof_environment["ios_bundle_id"],
        proof_android_package=proof_environment["android_package"],
        require_proof_setup_command=True,
        require_proof_command=True,
        repo_local_name=local_name,
        default_branch=branch,
        branch_prefix=prefix,
    )
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        print("Monica approved_pr configuration was not saved.")
        return 1

    if load_config_fn is None or save_config_fn is None:
        from hermes_cli.config import load_config, save_config

        load = load_config if load_config_fn is None else load_config_fn
        save = save_config if save_config_fn is None else save_config_fn
    else:
        load = load_config_fn
        save = save_config_fn

    config = load()
    if not isinstance(config, dict):
        config = {}
    _enable_plugin(config)
    monica_cfg = _ensure_dict(config, "mobile_bug_agent")
    monica_cfg["enabled"] = True
    monica_cfg["rollout_mode"] = "approved_pr"
    monica_cfg["dry_run"] = False

    slack_cfg = _ensure_dict(monica_cfg, "slack")
    slack_cfg["approver_user_ids"] = list(approvers)

    loop_cfg = _ensure_dict(monica_cfg, "loop")
    loop_cfg["create_linear"] = True
    loop_cfg["require_fix_approval"] = True

    repo_cfg = _ensure_dict(monica_cfg, "repo")
    repo_cfg["url"] = repo_url_value
    repo_cfg["local_name"] = local_name
    repo_cfg["default_branch"] = branch
    repo_cfg["branch_prefix"] = prefix

    verification_cfg = _ensure_dict(monica_cfg, "verification")
    verification_cfg["commands"] = list(commands)

    if (
        setup_commands
        or proof_command_list
        or required_env_keys
        or any(proof_environment.values())
        or proof_timeout_minutes > 0
    ):
        proof_cfg = _ensure_dict(monica_cfg, "proof")
        proof_cfg["enabled"] = True
        proof_cfg["required_for_done"] = True
        proof_cfg["platform_order"] = ["ios", "android"]
        if setup_commands:
            proof_cfg["setup_commands"] = list(setup_commands)
        if proof_command_list:
            proof_cfg["commands"] = list(proof_command_list)
        if required_env_keys:
            proof_cfg["required_env_keys"] = list(required_env_keys)
        for key, value in proof_environment.items():
            if value:
                proof_cfg[key] = value
        if proof_timeout_minutes > 0:
            proof_cfg["timeout_minutes"] = int(proof_timeout_minutes)

    runtime_cfg = _ensure_dict(monica_cfg, "runtime")
    runtime_cfg["home_subdir"] = "agents/monica"
    runtime_cfg["worker_session_prefix"] = "monica"
    runtime_cfg["skip_memory"] = True

    worker_cfg = _ensure_dict(monica_cfg, "worker")
    worker_cfg["backend"] = "codex_cli"
    worker_cfg["codex_command"] = str(worker_cfg.get("codex_command") or "codex").strip() or "codex"
    worker_cfg["codex_sandbox"] = "workspace-write"
    worker_cfg["codex_approval_policy"] = "never"
    if not worker_cfg.get("timeout_minutes"):
        worker_cfg["timeout_minutes"] = 45

    save(config)
    print("Configured Monica for approved_pr rollout.")
    print(
        "Secrets were not written; keep MONICA_SLACK_BOT_TOKEN, LINEAR_API_KEY, and GITHUB_TOKEN/gh auth outside config."
    )
    print("Run `hermes mobile-bug-agent doctor --rollout-mode approved_pr` before approving fixes.")
    return 0


def run_configure_proof_setup_command(
    *,
    proof_setup_commands: tuple[str, ...],
    proof_required_env_keys: tuple[str, ...] = (),
    load_config_fn: Any | None = None,
    save_config_fn: Any | None = None,
) -> int:
    setup_commands = _clean_sequence(proof_setup_commands)
    provided_required_env_keys = _clean_required_env_key_sequence(proof_required_env_keys)

    if load_config_fn is None or save_config_fn is None:
        from hermes_cli.config import load_config, save_config

        load = load_config if load_config_fn is None else load_config_fn
        save = save_config if save_config_fn is None else save_config_fn
    else:
        load = load_config_fn
        save = save_config_fn

    config = load()
    if not isinstance(config, dict):
        config = {}
    required_env_keys = provided_required_env_keys or _existing_proof_required_env_keys(config)
    errors = _proof_setup_command_config_errors(
        setup_commands,
        proof_required_env_keys=required_env_keys,
    )
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        print("Monica proof setup configuration was not saved.")
        return 1

    _enable_plugin(config)
    monica_cfg = _ensure_dict(config, "mobile_bug_agent")
    proof_cfg = _ensure_dict(monica_cfg, "proof")
    proof_cfg["enabled"] = True
    proof_cfg["required_for_done"] = True
    if not _clean_sequence(proof_cfg.get("platform_order", ())):
        proof_cfg["platform_order"] = ["ios", "android"]
    proof_cfg["setup_commands"] = list(setup_commands)
    if required_env_keys:
        proof_cfg["required_env_keys"] = list(required_env_keys)

    save(config)
    print("Configured Monica proof setup commands.")
    print("Secrets were not written; keep credentials in the Monica profile .env.")
    print("Run `hermes mobile-bug-agent doctor --rollout-mode approved_pr` before approving fixes.")
    return 0


def _existing_proof_required_env_keys(config: dict[str, Any]) -> tuple[str, ...]:
    monica_cfg = config.get("mobile_bug_agent")
    if not isinstance(monica_cfg, dict):
        return ()
    proof_cfg = monica_cfg.get("proof")
    if not isinstance(proof_cfg, dict):
        return ()
    return _clean_required_env_key_sequence(proof_cfg.get("required_env_keys", ()))


def run_configure_local_fix_only_command(
    *,
    approver_user_ids: tuple[str, ...],
    repo_url: str,
    verification_commands: tuple[str, ...],
    repo_local_name: str = "mobile-app",
    default_branch: str = "main",
    branch_prefix: str = "monica",
    load_config_fn: Any | None = None,
    save_config_fn: Any | None = None,
) -> int:
    return _run_configure_code_rollout_command(
        rollout_mode="local_fix_only",
        saved_label="local_fix_only",
        doctor_mode="local_fix_only",
        approver_user_ids=approver_user_ids,
        repo_url=repo_url,
        verification_commands=verification_commands,
        repo_local_name=repo_local_name,
        default_branch=default_branch,
        branch_prefix=branch_prefix,
        load_config_fn=load_config_fn,
        save_config_fn=save_config_fn,
    )


def _run_configure_code_rollout_command(
    *,
    rollout_mode: str,
    saved_label: str,
    doctor_mode: str,
    approver_user_ids: tuple[str, ...],
    repo_url: str,
    verification_commands: tuple[str, ...],
    repo_local_name: str = "mobile-app",
    default_branch: str = "main",
    branch_prefix: str = "monica",
    load_config_fn: Any | None = None,
    save_config_fn: Any | None = None,
) -> int:
    approvers = _clean_sequence(approver_user_ids)
    commands = _clean_sequence(verification_commands)
    repo_url_value = str(repo_url or "").strip()
    local_name = str(repo_local_name or "mobile-app").strip() or "mobile-app"
    branch = str(default_branch or "main").strip() or "main"
    prefix = str(branch_prefix or "monica").strip() or "monica"

    errors = _approved_pr_config_errors(
        approver_user_ids=approvers,
        repo_url=repo_url_value,
        verification_commands=commands,
        repo_local_name=local_name,
        default_branch=branch,
        branch_prefix=prefix,
    )
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        print(f"Monica {saved_label} configuration was not saved.")
        return 1

    if load_config_fn is None or save_config_fn is None:
        from hermes_cli.config import load_config, save_config

        load = load_config if load_config_fn is None else load_config_fn
        save = save_config if save_config_fn is None else save_config_fn
    else:
        load = load_config_fn
        save = save_config_fn

    config = load()
    if not isinstance(config, dict):
        config = {}
    _enable_plugin(config)
    monica_cfg = _ensure_dict(config, "mobile_bug_agent")
    monica_cfg["enabled"] = True
    monica_cfg["rollout_mode"] = rollout_mode
    monica_cfg["dry_run"] = False

    slack_cfg = _ensure_dict(monica_cfg, "slack")
    slack_cfg["approver_user_ids"] = list(approvers)

    loop_cfg = _ensure_dict(monica_cfg, "loop")
    loop_cfg["create_linear"] = True
    loop_cfg["require_fix_approval"] = True

    repo_cfg = _ensure_dict(monica_cfg, "repo")
    repo_cfg["url"] = repo_url_value
    repo_cfg["local_name"] = local_name
    repo_cfg["default_branch"] = branch
    repo_cfg["branch_prefix"] = prefix

    verification_cfg = _ensure_dict(monica_cfg, "verification")
    verification_cfg["commands"] = list(commands)

    runtime_cfg = _ensure_dict(monica_cfg, "runtime")
    runtime_cfg["home_subdir"] = "agents/monica"
    runtime_cfg["worker_session_prefix"] = "monica"
    runtime_cfg["skip_memory"] = True

    worker_cfg = _ensure_dict(monica_cfg, "worker")
    worker_cfg["backend"] = "codex_cli"
    worker_cfg["codex_command"] = str(worker_cfg.get("codex_command") or "codex").strip() or "codex"
    worker_cfg["codex_sandbox"] = "workspace-write"
    worker_cfg["codex_approval_policy"] = "never"
    if not worker_cfg.get("timeout_minutes"):
        worker_cfg["timeout_minutes"] = 45

    save(config)
    print(f"Configured Monica for {saved_label} rollout.")
    print(
        "Secrets were not written; keep MONICA_SLACK_BOT_TOKEN and LINEAR_API_KEY outside config."
    )
    print(f"Run `hermes mobile-bug-agent doctor --rollout-mode {doctor_mode}` before approving fixes.")
    return 0


def _linear_only_config_errors(
    *,
    bot_user_ids: tuple[str, ...],
    channel_ids: tuple[str, ...],
    linear_team_id: str,
) -> list[str]:
    errors: list[str] = []
    if not bot_user_ids:
        errors.append("at least one --bot-user-id is required")
    if not channel_ids:
        errors.append("at least one --channel-id is required")
    if not str(linear_team_id or "").strip():
        errors.append("--linear-team-id is required")
    errors.extend(_slack_scope_config_errors(bot_user_ids=bot_user_ids, channel_ids=channel_ids))
    return errors


def _slack_scope_config_errors(
    *,
    bot_user_ids: tuple[str, ...],
    channel_ids: tuple[str, ...],
) -> list[str]:
    errors: list[str] = []
    for user_id in bot_user_ids:
        if str(user_id).startswith("@"):
            errors.append(
                f"slack.bot_user_ids must contain Slack mention user IDs like U012ABCDEF, not handles like {user_id}"
            )
        elif str(user_id).upper().startswith("B"):
            errors.append(
                f"slack.bot_user_ids must contain Slack mention user IDs like U012ABCDEF, not bot_id values like {user_id}"
            )
        elif not _is_slack_user_id(user_id):
            errors.append(
                f"slack.bot_user_ids must contain Slack mention user IDs like U012ABCDEF, not invalid values like {user_id}"
            )
    for channel_id in channel_ids:
        if str(channel_id).startswith("#"):
            errors.append(
                f"slack.allowed_channels must contain Slack channel IDs like C123 or G123, not names like {channel_id}"
            )
        elif not _is_slack_channel_id(channel_id):
            errors.append(
                f"slack.allowed_channels must contain Slack channel IDs like C123 or G123, not invalid values like {channel_id}"
            )
    return errors


def _approved_pr_config_errors(
    *,
    approver_user_ids: tuple[str, ...],
    repo_url: str,
    verification_commands: tuple[str, ...],
    repo_local_name: str,
    default_branch: str,
    branch_prefix: str,
    proof_setup_commands: tuple[str, ...] = (),
    proof_commands: tuple[str, ...] = (),
    proof_required_env_keys: tuple[str, ...] = (),
    proof_dev_client_scheme: str = "",
    proof_ios_bundle_id: str = "",
    proof_android_package: str = "",
    require_proof_setup_command: bool = False,
    require_proof_command: bool = False,
) -> list[str]:
    errors: list[str] = []
    if not approver_user_ids:
        errors.append("at least one --approver-user-id is required")
    if not str(repo_url or "").strip():
        errors.append("--repo-url is required")
    if not verification_commands:
        errors.append("at least one --verification-command is required")
    if require_proof_setup_command and not proof_setup_commands:
        errors.append("at least one --proof-setup-command is required")
    if require_proof_setup_command and proof_setup_commands and not proof_required_env_keys:
        errors.append("at least one --proof-required-env-key is required")
    placeholder_setup_commands = _placeholder_proof_setup_commands(proof_setup_commands)
    if placeholder_setup_commands:
        errors.append(
            "--proof-setup-command must be the real auth/session seed command, "
            f"not placeholder {placeholder_setup_commands[0]}"
        )
    secret_setup_assignments = _inline_secret_env_assignments(proof_setup_commands)
    if secret_setup_assignments:
        errors.append(
            "--proof-setup-command must not inline secret env assignment(s); "
            "put credentials in the Monica profile .env instead: "
            f"{', '.join(secret_setup_assignments)}"
        )
    noop_setup_commands = _noop_only_proof_setup_commands(proof_setup_commands)
    if noop_setup_commands:
        errors.append(
            "--proof-setup-command must seed test auth/session state, "
            f"not only no-op command(s): {', '.join(noop_setup_commands)}"
        )
    errors.extend(_proof_required_env_key_config_errors(proof_required_env_keys))
    if require_proof_command and not proof_commands:
        errors.append("at least one --proof-command is required")
    placeholder_proof_commands = _placeholder_proof_commands(proof_commands)
    if placeholder_proof_commands:
        errors.append(
            "--proof-command must be the real simulator proof command, "
            f"not placeholder {placeholder_proof_commands[0]}"
        )
    secret_proof_assignments = _inline_secret_env_assignments(proof_commands)
    if secret_proof_assignments:
        errors.append(
            "--proof-command must not inline secret env assignment(s); "
            "put credentials in the Monica profile .env instead: "
            f"{', '.join(secret_proof_assignments)}"
        )
    noop_proof_commands = _noop_only_proof_commands(proof_commands)
    if noop_proof_commands:
        errors.append(
            "--proof-command must capture simulator proof artifacts, "
            f"not only no-op command(s): {', '.join(noop_proof_commands)}"
        )
    if proof_commands and _uses_builtin_simulator_proof(proof_commands):
        if not _proof_setting_configured(
            proof_dev_client_scheme,
            proof_commands,
            cli_flag="--dev-client-scheme",
            env_var="MONICA_DEV_CLIENT_SCHEME",
        ):
            errors.append(
                "proof.dev_client_scheme is required for built-in simulator proof "
                "(pass --proof-dev-client-scheme or set --dev-client-scheme/MONICA_DEV_CLIENT_SCHEME in --proof-command)"
            )
        if not _proof_setting_configured(
            proof_ios_bundle_id,
            proof_commands,
            cli_flag="--ios-bundle-id",
            env_var="MONICA_IOS_BUNDLE_ID",
        ):
            errors.append(
                "proof.ios_bundle_id is required for built-in simulator proof "
                "(pass --proof-ios-bundle-id or set --ios-bundle-id/MONICA_IOS_BUNDLE_ID in --proof-command)"
            )
        if not _proof_setting_configured(
            proof_android_package,
            proof_commands,
            cli_flag="--android-package",
            env_var="MONICA_ANDROID_PACKAGE",
        ):
            errors.append(
                "proof.android_package is required for built-in simulator proof "
                "(pass --proof-android-package or set --android-package/MONICA_ANDROID_PACKAGE in --proof-command)"
            )
    for user_id in approver_user_ids:
        if str(user_id).startswith("@"):
            errors.append(
                f"slack.approver_user_ids must contain Slack user IDs like U123, not handles like {user_id}"
            )
        elif not _is_slack_user_id(user_id):
            errors.append(
                f"slack.approver_user_ids must contain Slack user IDs like U123, not invalid values like {user_id}"
            )
    for validator, value in (
        (safe_repo_local_name, repo_local_name),
        (safe_default_branch, default_branch),
        (safe_branch_prefix, branch_prefix),
    ):
        try:
            validator(value)
        except RepoManagerError as exc:
            errors.append(str(exc))
    return errors


def _proof_setup_command_config_errors(
    proof_setup_commands: tuple[str, ...],
    *,
    proof_required_env_keys: tuple[str, ...] = (),
) -> list[str]:
    errors: list[str] = []
    if not proof_setup_commands:
        errors.append("at least one --proof-setup-command is required")
    if proof_setup_commands and not proof_required_env_keys:
        errors.append("at least one --proof-required-env-key is required")
    placeholder_setup_commands = _placeholder_proof_setup_commands(proof_setup_commands)
    if placeholder_setup_commands:
        errors.append(
            "--proof-setup-command must be the real auth/session seed command, "
            f"not placeholder {placeholder_setup_commands[0]}"
        )
    secret_setup_assignments = _inline_secret_env_assignments(proof_setup_commands)
    if secret_setup_assignments:
        errors.append(
            "--proof-setup-command must not inline secret env assignment(s); "
            "put credentials in the Monica profile .env instead: "
            f"{', '.join(secret_setup_assignments)}"
        )
    noop_setup_commands = _noop_only_proof_setup_commands(proof_setup_commands)
    if noop_setup_commands:
        errors.append(
            "--proof-setup-command must seed test auth/session state, "
            f"not only no-op command(s): {', '.join(noop_setup_commands)}"
        )
    errors.extend(_proof_required_env_key_config_errors(proof_required_env_keys))
    return errors


def _proof_required_env_key_config_errors(proof_required_env_keys: tuple[str, ...]) -> list[str]:
    invalid_keys = _invalid_required_env_keys(proof_required_env_keys)
    if not invalid_keys:
        return []
    return [
        "--proof-required-env-key must name an environment key like MONICA_TEST_LOGIN_TOKEN, "
        f"not KEY=value or another invalid value: {', '.join(invalid_keys)}"
    ]


def _clean_sequence(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raw_values = value.split(",")
    else:
        try:
            raw_values = list(value)
        except TypeError:
            raw_values = [value]
    return tuple(str(item).strip() for item in raw_values if str(item).strip())


def _clean_required_env_key_sequence(value: Any) -> tuple[str, ...]:
    keys: list[str] = []
    seen: set[str] = set()
    for item in _clean_sequence(value):
        if item in seen:
            continue
        seen.add(item)
        keys.append(item)
    return tuple(keys)


def _enable_plugin(config: dict[str, Any]) -> None:
    plugins_cfg = _ensure_dict(config, "plugins")
    enabled = set(_clean_sequence(plugins_cfg.get("enabled", ())))
    disabled = set(_clean_sequence(plugins_cfg.get("disabled", ())))
    enabled.add(_MONICA_PLUGIN_NAME)
    disabled.discard(_MONICA_PLUGIN_NAME)
    plugins_cfg["enabled"] = sorted(enabled)
    if disabled:
        plugins_cfg["disabled"] = sorted(disabled)
    elif "disabled" in plugins_cfg:
        plugins_cfg["disabled"] = []


def _ensure_dict(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        value = {}
        parent[key] = value
    return value


def _load_hermes_config_mapping() -> dict[str, Any] | None:
    try:
        from hermes_cli.config import load_config

        config = load_config()
    except Exception:
        return None
    return config if isinstance(config, dict) else None


def _cli_readiness_checker(config: MonicaConfig) -> Any:
    return lambda: run_doctor_command(
        config=config,
        plugin_config=_load_hermes_config_mapping(),
    )


def _monica_plugin_health_payload(
    plugin_config: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if plugin_config is None:
        return None
    plugins_cfg = plugin_config.get("plugins")
    if not isinstance(plugins_cfg, dict):
        plugins_cfg = {}
    enabled = set(_clean_sequence(plugins_cfg.get("enabled", ())))
    disabled = set(_clean_sequence(plugins_cfg.get("disabled", ())))
    explicitly_enabled = _MONICA_PLUGIN_NAME in enabled
    explicitly_disabled = _MONICA_PLUGIN_NAME in disabled
    return {
        "configured": "enabled" in plugins_cfg,
        "enabled": explicitly_enabled and not explicitly_disabled,
        "disabled": explicitly_disabled,
        "name": _MONICA_PLUGIN_NAME,
    }


def run_status_command(
    *,
    state: MonicaState,
    limit: int = 20,
    json_output: bool = False,
    gateway_status: dict[str, Any] | None = None,
    current_commit: str | None = None,
    now: float | None = None,
) -> int:
    runs = state.list_runs(limit=limit)
    runtime_sync_blocking_runs = state.list_runtime_sync_blocking_runs()
    runtime_sync_metadata = state.runtime_sync_health()
    runtime_sync_lease = _runtime_sync_lease_payload(runtime_sync_metadata.get("lease"))
    gateway_health = _gateway_health_payload(
        gateway_status if gateway_status is not None else _read_gateway_status(),
        now=time.time() if now is None else now,
    )
    commit = current_commit if current_commit is not None else _current_hermes_commit()
    runtime_sync_stale = _runtime_sync_stale(
        current_commit=commit,
        last_synced_commit=runtime_sync_metadata["last_synced_commit"],
    )
    if json_output:
        print(
            json.dumps(
                {
                    "count": len(runs),
                    "health": {
                        "hermes_commit": commit,
                        "gateway": gateway_health,
                    },
                    "runs": [_run_payload(run, include_raw_event=False) for run in runs],
                    "runtime_sync": {
                        "idle": not runtime_sync_blocking_runs,
                        "ready_for_monica_work": (
                            not runtime_sync_blocking_runs
                            and not runtime_sync_lease["active"]
                        ),
                        "current_commit": commit,
                        "stale": runtime_sync_stale,
                        "last_synced_commit": runtime_sync_metadata["last_synced_commit"],
                        "last_synced_at": runtime_sync_metadata["last_synced_at"],
                        "last_sync_status": runtime_sync_metadata["last_sync_status"],
                        "last_sync_failure_reason": runtime_sync_metadata["last_sync_failure_reason"],
                        "lease": runtime_sync_lease,
                        "blocking_statuses": list(RUNTIME_SYNC_BLOCKING_STATUSES),
                        "active_run_count": len(runtime_sync_blocking_runs),
                        "active_runs": [
                            _run_payload(run, include_raw_event=False)
                            for run in runtime_sync_blocking_runs
                        ],
                    },
                },
                sort_keys=True,
            )
        )
        return 0
    print(f"Hermes commit: {commit or 'unavailable'}")
    print(_format_gateway_health(gateway_health))
    print(
        _format_runtime_sync_status(
            active_run_count=len(runtime_sync_blocking_runs),
            last_synced_commit=runtime_sync_metadata["last_synced_commit"],
            last_synced_at=runtime_sync_metadata["last_synced_at"],
            active_runs=runtime_sync_blocking_runs,
            stale=runtime_sync_stale,
            lease=runtime_sync_lease,
        )
    )
    if not runs:
        print("No Monica runs found.")
        return 0
    for run in runs:
        parts = [
            run.id,
            run.status,
            f"slack:{run.channel_id}/{run.thread_ts}",
            f"linear:{run.linear_identifier or '-'}",
            f"branch:{run.branch_name or '-'}",
            f"url:{run.pr_url or run.linear_url or '-'}",
            _one_line(run.request_text),
        ]
        print(" | ".join(parts))
    return 0


def _format_gateway_health(gateway_health: dict[str, Any]) -> str:
    state = str(gateway_health.get("state") or "unknown")
    pid = _status_text_value(gateway_health.get("pid"))
    active_agents = _status_text_value(gateway_health.get("active_agents"))
    uptime = gateway_health.get("uptime_seconds")
    uptime_text = f"{int(uptime)}s" if isinstance(uptime, int) else "-"
    line = f"Gateway: {state} pid:{pid} uptime:{uptime_text} active_agents:{active_agents}"
    if gateway_health.get("stale"):
        age = gateway_health.get("updated_age_seconds")
        age_text = f"{int(age)}s" if isinstance(age, int) else "-"
        line += f" stale:true heartbeat_age:{age_text}"
    return line


def _format_gateway_service_health(gateway_service_health: dict[str, Any]) -> str:
    if not gateway_service_health.get("available"):
        return "Gateway service: unavailable"
    manager = _status_text_value(gateway_service_health.get("manager"))
    installed = "true" if gateway_service_health.get("service_installed") else "false"
    running = "true" if gateway_service_health.get("service_running") else "false"
    supervised = "true" if gateway_service_health.get("supervised") else "false"
    pids = gateway_service_health.get("gateway_pids") or []
    pid_text = ",".join(str(pid) for pid in pids) if pids else "-"
    return (
        f"Gateway service: {manager} installed:{installed} "
        f"running:{running} supervised:{supervised} pids:{pid_text}"
    )


def _format_runtime_sync_status(
    *,
    active_run_count: int,
    last_synced_commit: Any,
    last_synced_at: Any,
    active_runs: list[Any] | tuple[Any, ...] = (),
    stale: bool = False,
    lease: dict[str, Any] | None = None,
) -> str:
    lease_payload = lease or {"active": False}
    state = "updating" if lease_payload.get("active") else ("idle" if active_run_count == 0 else "blocked")
    commit = _status_text_value(last_synced_commit)
    synced_at = _status_text_value(last_synced_at)
    line = (
        f"Runtime sync: {state} active_runs:{active_run_count} "
        f"last_commit:{commit} last_synced:{synced_at}"
    )
    if lease_payload.get("active"):
        line += f" lease:{_status_text_value(lease_payload.get('id'))}"
    if stale:
        line += " stale:true"
    if active_run_count > 0:
        labels = [_runtime_sync_run_label(run) for run in active_runs[:5]]
        labels = [label for label in labels if label]
        if labels:
            suffix = "" if len(active_runs) <= 5 else f", +{len(active_runs) - 5} more"
            line += f" blocked_by:{', '.join(labels)}{suffix}"
    return line


def _format_runtime_sync_health(payload: dict[str, Any]) -> str:
    if not payload.get("available"):
        commit = _status_text_value(payload.get("current_commit"))
        return f"Runtime sync: unavailable current_commit:{commit}"
    return _format_runtime_sync_status(
        active_run_count=int(payload.get("active_run_count") or 0),
        last_synced_commit=payload.get("last_synced_commit"),
        last_synced_at=payload.get("last_synced_at"),
        active_runs=payload.get("active_runs") or (),
        stale=bool(payload.get("stale")),
        lease=_runtime_sync_lease_payload(payload.get("lease")),
    )


def _format_plugin_health(payload: dict[str, Any]) -> str:
    state = "enabled" if payload.get("enabled") else "disabled"
    configured = "true" if payload.get("configured") else "false"
    disabled = "true" if payload.get("disabled") else "false"
    name = str(payload.get("name") or _MONICA_PLUGIN_NAME)
    return f"Plugin: {name} {state} configured:{configured} disabled:{disabled}"


def _runtime_sync_run_label(run: Any) -> str:
    if isinstance(run, dict):
        linear = run.get("linear")
        linear_identifier = ""
        if isinstance(linear, dict):
            linear_identifier = str(linear.get("identifier") or "").strip()
        identifier = str(linear_identifier or run.get("id") or "").strip()
        status = str(run.get("status") or "").strip()
    else:
        identifier = str(getattr(run, "linear_identifier", "") or getattr(run, "id", "") or "").strip()
        status = str(getattr(run, "status", "") or "").strip()
    if identifier and status:
        return f"{identifier}/{status}"
    return identifier or status


def _runtime_sync_stale(*, current_commit: Any, last_synced_commit: Any) -> bool:
    current = str(current_commit or "").strip()
    synced = str(last_synced_commit or "").strip()
    if not current or not synced:
        return False
    if current == synced:
        return False
    if len(current) >= 7 and len(synced) >= 7:
        return not (current.startswith(synced) or synced.startswith(current))
    return True


def _runtime_sync_health_payload(
    *,
    state: MonicaState | None,
    config: MonicaConfig,
    current_commit: Any,
) -> dict[str, Any]:
    resolved_state = state or _try_open_state(config)
    commit = str(current_commit or "").strip()
    if resolved_state is None:
        return {
            "available": False,
            "idle": None,
            "ready_for_monica_work": False,
            "current_commit": commit,
            "stale": False,
            "last_synced_commit": "",
            "last_synced_at": "",
            "last_sync_status": "",
            "last_sync_failure_reason": "",
            "lease": _runtime_sync_lease_payload(None),
            "blocking_statuses": list(RUNTIME_SYNC_BLOCKING_STATUSES),
            "active_run_count": None,
            "active_runs": [],
        }
    active_runs = resolved_state.list_runtime_sync_blocking_runs()
    metadata = resolved_state.runtime_sync_health()
    lease = _runtime_sync_lease_payload(metadata.get("lease"))
    return {
        "available": True,
        "idle": not active_runs,
        "ready_for_monica_work": not active_runs and not lease["active"],
        "current_commit": commit,
        "stale": _runtime_sync_stale(
            current_commit=commit,
            last_synced_commit=metadata["last_synced_commit"],
        ),
        "last_synced_commit": metadata["last_synced_commit"],
        "last_synced_at": metadata["last_synced_at"],
        "last_sync_status": metadata["last_sync_status"],
        "last_sync_failure_reason": metadata["last_sync_failure_reason"],
        "lease": lease,
        "blocking_statuses": list(RUNTIME_SYNC_BLOCKING_STATUSES),
        "active_run_count": len(active_runs),
        "active_runs": [
            _run_payload(run, include_raw_event=False)
            for run in active_runs
        ],
    }


def _runtime_sync_lease_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return _inactive_runtime_sync_lease_payload()
    if "active" in value and not value.get("active"):
        return _inactive_runtime_sync_lease_payload()
    return {
        "active": True,
        "id": str(value.get("lease_id") or value.get("id") or ""),
        "started_at": str(value.get("started_at") or ""),
        "expires_at": str(value.get("expires_at") or ""),
        "pre_update_commit": str(value.get("pre_update_commit") or ""),
        "project_root": str(value.get("project_root") or ""),
        "owner_id": str(value.get("owner_id") or ""),
        "owner_pid": int(value.get("owner_pid") or 0),
        "owner_host": str(value.get("owner_host") or ""),
    }


def _inactive_runtime_sync_lease_payload() -> dict[str, Any]:
    return {
        "active": False,
        "id": "",
        "started_at": "",
        "expires_at": "",
        "pre_update_commit": "",
        "project_root": "",
        "owner_id": "",
        "owner_pid": 0,
        "owner_host": "",
    }


def _try_open_state(config: MonicaConfig) -> MonicaState | None:
    try:
        return _open_state(config)
    except Exception:
        return None


def _status_text_value(value: Any) -> str:
    return str(value) if value not in (None, "") else "-"


def _read_gateway_status() -> dict[str, Any]:
    try:
        from gateway.status import read_runtime_status

        payload = read_runtime_status()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_gateway_service_status() -> dict[str, Any]:
    try:
        from hermes_cli.gateway import get_gateway_runtime_snapshot

        snapshot = get_gateway_runtime_snapshot()
    except Exception:
        return {"available": False}
    gateway_pids = tuple(int(pid) for pid in (getattr(snapshot, "gateway_pids", ()) or ()))
    manager = str(getattr(snapshot, "manager", "") or "")
    service_running = bool(getattr(snapshot, "service_running", False))
    return {
        "available": True,
        "manager": manager,
        "service_installed": bool(getattr(snapshot, "service_installed", False)),
        "service_running": service_running,
        "gateway_pids": list(gateway_pids),
        "service_scope": getattr(snapshot, "service_scope", None),
        "supervised": _gateway_service_snapshot_is_supervised(
            manager=manager,
            service_running=service_running,
            gateway_pids=gateway_pids,
        ),
    }


def _gateway_service_snapshot_is_supervised(
    *,
    manager: str,
    service_running: bool,
    gateway_pids: tuple[int, ...],
) -> bool:
    if service_running:
        return True
    manager_text = manager.strip().lower()
    return bool(gateway_pids) and "s6" in manager_text and "supervisor" in manager_text


def _gateway_health_payload(gateway_status: dict[str, Any], *, now: float) -> dict[str, Any]:
    start_time = _float_or_none(gateway_status.get("start_time"))
    uptime_seconds = int(max(0, now - start_time)) if start_time is not None else None
    updated_age_seconds = _gateway_status_age_seconds(gateway_status.get("updated_at"), now=now)
    return {
        "state": str(gateway_status.get("gateway_state") or ""),
        "pid": gateway_status.get("pid"),
        "active_agents": _int_or_zero(gateway_status.get("active_agents")),
        "start_time": start_time,
        "uptime_seconds": uptime_seconds,
        "updated_at": str(gateway_status.get("updated_at") or ""),
        "updated_age_seconds": updated_age_seconds,
        "stale": (
            updated_age_seconds is not None
            and updated_age_seconds > _GATEWAY_STATUS_STALE_AFTER_SECONDS
        ),
    }


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _gateway_status_age_seconds(value: Any, *, now: float) -> int | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    timestamp = _float_or_none(text)
    if timestamp is None:
        normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            updated_at = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        timestamp = updated_at.timestamp()
    return int(max(0, now - timestamp))


def _current_hermes_commit() -> str:
    try:
        from hermes_cli.build_info import get_build_sha

        build_sha = get_build_sha()
    except Exception:
        build_sha = None
    if build_sha:
        return str(build_sha)
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short=8", "HEAD"],
            cwd=Path(__file__).resolve().parents[2],
            text=True,
            capture_output=True,
            check=False,
            timeout=2,
        )
    except Exception:
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def run_show_command(*, state: MonicaState, run_id: str, json_output: bool = False) -> int:
    run = state.get_run(run_id)
    if run is None:
        print(f"Monica run not found: {run_id}")
        return 1
    if json_output:
        print(json.dumps(_run_payload(run, include_raw_event=True), sort_keys=True))
        return 0
    for key, value in run.__dict__.items():
        print(f"{key}: {value}")
    return 0


def _run_payload(run: Any, *, include_raw_event: bool) -> dict[str, Any]:
    payload = {
        "id": run.id,
        "platform": run.platform,
        "status": run.status,
        "intent": run.intent,
        "request_text": run.request_text,
        "slack": {
            "channel_id": run.channel_id,
            "thread_ts": run.thread_ts,
            "message_ts": run.message_ts,
            "user_id": run.user_id,
        },
        "linear": {
            "identifier": run.linear_identifier,
            "issue_id": run.linear_issue_id,
            "url": run.linear_url,
        },
        "branch_name": run.branch_name,
        "base": {
            "ref": run.base_branch,
            "commit": run.base_commit,
        },
        "proof_target": {
            "deep_link": run.proof_deep_link,
            "expected_text": run.proof_expected_text,
        },
        "pr_url": run.pr_url,
        "failure_reason": run.failure_reason,
        "approved_by_user_id": run.approved_by_user_id,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
    }
    if include_raw_event:
        payload["raw_event"] = run.raw_event or {}
    return payload


def run_doctor_command(
    *,
    config: MonicaConfig,
    state: MonicaState | None = None,
    target_rollout_mode: str = "",
    json_output: bool = False,
    plugin_config: dict[str, Any] | None = None,
    environ: dict[str, str] | None = None,
    which: Any | None = None,
    module_available: Any | None = None,
    gateway_status: dict[str, Any] | None = None,
    gateway_service_status: dict[str, Any] | None = None,
    current_commit: str | None = None,
    now: float | None = None,
) -> int:
    effective_config = _config_for_doctor_target(
        config=config,
        target_rollout_mode=target_rollout_mode,
    )
    report = check_monica_readiness(
        config=effective_config,
        environ=environ,
        which=which,
        module_available=module_available,
    )
    gateway_status_provided = gateway_status is not None
    gateway_health = _gateway_health_payload(
        gateway_status if gateway_status_provided else _read_gateway_status(),
        now=time.time() if now is None else now,
    )
    gateway_service_health = (
        gateway_service_status
        if gateway_service_status is not None
        else ({"available": False} if gateway_status_provided else _read_gateway_service_status())
    )
    commit = current_commit if current_commit is not None else _current_hermes_commit()
    runtime_sync_health = _runtime_sync_health_payload(
        state=state,
        config=effective_config,
        current_commit=commit,
    )
    runtime_sync_warnings = _doctor_runtime_sync_warnings(
        runtime_sync_health,
        rollout_mode=report.rollout_mode,
    )
    runtime_sync_failures = _doctor_runtime_sync_failures(
        runtime_sync_health,
        rollout_mode=report.rollout_mode,
    )
    gateway_failures = _doctor_gateway_failures(
        gateway_health,
        gateway_service_health,
        rollout_mode=report.rollout_mode,
    )
    gateway_warnings = _doctor_gateway_warnings(
        gateway_health,
        rollout_mode=report.rollout_mode,
    )
    plugin_health = _monica_plugin_health_payload(plugin_config)
    plugin_failures = _doctor_plugin_failures(plugin_health)

    if json_output:
        payload = _readiness_report_payload(report)
        payload["warnings"].extend(gateway_warnings)
        payload["warnings"].extend(runtime_sync_warnings)
        payload["failures"].extend(runtime_sync_failures)
        payload["failures"].extend(gateway_failures)
        payload["failures"].extend(plugin_failures)
        if runtime_sync_failures or gateway_failures or plugin_failures:
            payload["ready"] = False
        health = {
            "hermes_commit": commit,
            "gateway": gateway_health,
            "gateway_service": gateway_service_health,
            "runtime_sync": runtime_sync_health,
        }
        if plugin_health is not None:
            health["plugin"] = plugin_health
        payload["health"] = health
        print(json.dumps(payload, sort_keys=True))
        return (
            0
            if report.ready
            and not runtime_sync_failures
            and not gateway_failures
            and not plugin_failures
            else 1
        )

    print(f"Hermes commit: {commit or 'unavailable'}")
    print(_format_gateway_health(gateway_health))
    print(_format_gateway_service_health(gateway_service_health))
    print(_format_runtime_sync_health(runtime_sync_health))
    if plugin_health is not None:
        print(_format_plugin_health(plugin_health))
    print(f"Monica rollout mode: {report.rollout_mode}")
    if report.runtime_root_value:
        print(f"Monica runtime root: {report.runtime_root_value}")
    for warning in report.warnings:
        print(f"WARN: {warning.message}")
    for warning in gateway_warnings:
        print(f"WARN: {warning['message']}")
    for warning in runtime_sync_warnings:
        print(f"WARN: {warning['message']}")
    for failure in runtime_sync_failures:
        print(f"FAIL: {failure['message']}")
    for failure in gateway_failures:
        print(f"FAIL: {failure['message']}")
    for failure in plugin_failures:
        print(f"FAIL: {failure['message']}")
    for failure in report.failures:
        print(f"FAIL: {failure.message}")
    if report.failures or runtime_sync_failures or gateway_failures or plugin_failures:
        print("Monica doctor: not ready")
        return 1
    print("Monica doctor: ready")
    return 0


def _doctor_gateway_failures(
    gateway_health: dict[str, Any],
    gateway_service_health: dict[str, Any],
    *,
    rollout_mode: str,
) -> list[dict[str, str]]:
    if rollout_mode != "approved_pr":
        return []
    issue = _gateway_not_running_issue(gateway_health, require_status=True)
    if issue:
        return [issue]
    heartbeat_issue = _gateway_heartbeat_missing_issue(gateway_health)
    if heartbeat_issue:
        return [heartbeat_issue]
    stale_issue = _gateway_status_stale_issue(gateway_health)
    if stale_issue:
        return [stale_issue]
    metadata_issue = _gateway_runtime_metadata_missing_issue(gateway_health)
    if metadata_issue:
        return [metadata_issue]
    supervision_issue = _gateway_not_supervised_issue(gateway_health, gateway_service_health)
    return [supervision_issue] if supervision_issue else []


def _doctor_runtime_sync_warnings(
    runtime_sync_health: dict[str, Any],
    *,
    rollout_mode: str = "",
) -> list[dict[str, str]]:
    if not runtime_sync_health.get("available"):
        return []
    current_commit = _status_text_value(runtime_sync_health.get("current_commit"))
    last_commit = _status_text_value(runtime_sync_health.get("last_synced_commit"))
    active_count = runtime_sync_health.get("active_run_count")
    if (
        rollout_mode in {"local_fix_only", "approved_pr"}
        and runtime_sync_health.get("current_commit")
        and not runtime_sync_health.get("last_synced_commit")
    ):
        if isinstance(active_count, int) and active_count > 0:
            message = (
                "Monica runtime sync has not recorded a synced Hermes commit yet "
                f"(current {current_commit}) while Monica has {active_count} active run(s); "
                "wait until Monica is idle before running `hermes update`."
            )
        else:
            message = (
                "Monica runtime sync has not recorded a synced Hermes commit yet "
                f"(current {current_commit}); run `hermes update` while Monica is idle "
                "to record the current runtime."
            )
        return [{"code": "runtime_sync_unrecorded", "message": message}]
    if not runtime_sync_health.get("stale"):
        return []
    if isinstance(active_count, int) and active_count > 0:
        message = (
            "Monica runtime sync is stale "
            f"(current {current_commit}, last synced {last_commit}) while Monica has "
            f"{active_count} active run(s); wait until Monica is idle before running `hermes update`."
        )
    else:
        message = (
            "Monica runtime sync is stale "
            f"(current {current_commit}, last synced {last_commit}); run `hermes update` "
            "while Monica is idle to record the current runtime."
        )
    return [{"code": "runtime_sync_stale", "message": message}]


def _doctor_runtime_sync_failures(
    runtime_sync_health: dict[str, Any],
    *,
    rollout_mode: str = "",
) -> list[dict[str, str]]:
    if not runtime_sync_health.get("available"):
        return []
    lease = _runtime_sync_lease_payload(runtime_sync_health.get("lease"))
    if not lease.get("active"):
        return []
    return [
        {
            "code": "runtime_sync_in_progress",
            "message": (
                "Monica is temporarily paused while Hermes is updating "
                f"(lease {lease.get('id') or '-'}). Wait for the update to finish "
                "before starting or approving Monica work."
            ),
        }
    ]


def _doctor_gateway_warnings(
    gateway_health: dict[str, Any],
    *,
    rollout_mode: str = "",
) -> list[dict[str, str]]:
    if rollout_mode == "approved_pr":
        return []
    issue = _gateway_not_running_issue(gateway_health)
    return [issue] if issue else []


def _gateway_not_running_issue(
    gateway_health: dict[str, Any],
    *,
    require_status: bool = False,
) -> dict[str, str] | None:
    state = str(gateway_health.get("state") or "").strip().lower()
    if state == "running":
        return None
    if not state:
        if not require_status:
            return None
        return {
            "code": "gateway_not_running",
            "message": (
                "Monica gateway status is unavailable; live Slack tag/DM intake "
                "and approvals will not be received until the gateway is running."
            ),
        }
    return {
        "code": "gateway_not_running",
        "message": (
            "Monica gateway is stopped; live Slack tag/DM intake and approvals "
            "will not be received until the gateway is running."
        ),
    }


def _gateway_status_stale_issue(gateway_health: dict[str, Any]) -> dict[str, str] | None:
    if not gateway_health.get("stale"):
        return None
    return {
        "code": "gateway_status_stale",
        "message": (
            "Monica gateway status is stale; live Slack tag/DM intake and approvals "
            "may not be received until the gateway heartbeat is fresh."
        ),
    }


def _gateway_heartbeat_missing_issue(gateway_health: dict[str, Any]) -> dict[str, str] | None:
    state = str(gateway_health.get("state") or "").strip().lower()
    updated_at = str(gateway_health.get("updated_at") or "").strip()
    if state != "running" or updated_at:
        return None
    return {
        "code": "gateway_heartbeat_missing",
        "message": (
            "Monica gateway heartbeat is missing; restart the gateway so live Slack "
            "tag/DM intake and approvals run on the current Monica runtime."
        ),
    }


def _gateway_runtime_metadata_missing_issue(gateway_health: dict[str, Any]) -> dict[str, str] | None:
    state = str(gateway_health.get("state") or "").strip().lower()
    if state != "running" or gateway_health.get("start_time") is not None:
        return None
    return {
        "code": "gateway_runtime_metadata_missing",
        "message": (
            "Monica gateway runtime metadata is incomplete; restart the gateway "
            "so doctor can verify uptime before approved_pr work runs."
        ),
    }


def _gateway_not_supervised_issue(
    gateway_health: dict[str, Any],
    gateway_service_health: dict[str, Any],
) -> dict[str, str] | None:
    state = str(gateway_health.get("state") or "").strip().lower()
    if state != "running":
        return None
    if not gateway_service_health.get("available"):
        return None
    if gateway_service_health.get("supervised"):
        return None
    return {
        "code": "gateway_not_supervised",
        "message": (
            "Monica gateway is running manually; approved_pr work needs the "
            "profile-aware gateway service so Slack approvals survive restarts "
            "and terminal exits."
        ),
    }


def _doctor_plugin_failures(
    plugin_health: dict[str, Any] | None,
) -> list[dict[str, str]]:
    if plugin_health is None or bool(plugin_health.get("enabled")):
        return []
    return [
        {
            "code": "plugin_not_enabled",
            "message": (
                "plugins.enabled must include mobile-bug-agent and plugins.disabled must not; "
                "otherwise Monica's Slack gateway hooks will not load."
            ),
        }
    ]


def run_setup_plan_command(
    *,
    config: MonicaConfig,
    state: MonicaState | None = None,
    target_rollout_mode: str = "",
    json_output: bool = False,
    plugin_config: dict[str, Any] | None = None,
    environ: dict[str, str] | None = None,
    which: Any | None = None,
    module_available: Any | None = None,
    gateway_status: dict[str, Any] | None = None,
    gateway_service_status: dict[str, Any] | None = None,
    current_commit: str | None = None,
    now: float | None = None,
) -> int:
    effective_config = _config_for_doctor_target(
        config=config,
        target_rollout_mode=target_rollout_mode,
    )
    report = check_monica_readiness(
        config=effective_config,
        environ=environ,
        which=which,
        module_available=module_available,
    )
    gateway_status_provided = gateway_status is not None
    gateway_health = _gateway_health_payload(
        gateway_status if gateway_status_provided else _read_gateway_status(),
        now=time.time() if now is None else now,
    )
    gateway_service_health = (
        gateway_service_status
        if gateway_service_status is not None
        else ({"available": False} if gateway_status_provided else _read_gateway_service_status())
    )
    commit = current_commit if current_commit is not None else _current_hermes_commit()
    runtime_sync_health = _runtime_sync_health_payload(
        state=state,
        config=effective_config,
        current_commit=commit,
    )
    runtime_sync_warnings = _doctor_runtime_sync_warnings(
        runtime_sync_health,
        rollout_mode=report.rollout_mode,
    )
    gateway_failures = _doctor_gateway_failures(
        gateway_health,
        gateway_service_health,
        rollout_mode=report.rollout_mode,
    )
    gateway_warnings = _doctor_gateway_warnings(
        gateway_health,
        rollout_mode=report.rollout_mode,
    )
    plugin_health = _monica_plugin_health_payload(plugin_config)
    plugin_failures = _doctor_plugin_failures(plugin_health)
    steps = _setup_plan_steps(
        report,
        config=effective_config,
        extra_warnings=[*gateway_warnings, *runtime_sync_warnings],
        extra_failures=[*gateway_failures, *plugin_failures],
        runtime_sync_health=runtime_sync_health,
    )
    if json_output:
        payload = _readiness_report_payload(report)
        payload["warnings"].extend(gateway_warnings)
        payload["warnings"].extend(runtime_sync_warnings)
        payload["failures"].extend(gateway_failures)
        payload["failures"].extend(plugin_failures)
        if gateway_failures or plugin_failures:
            payload["ready"] = False
        health = {
            "hermes_commit": commit,
            "gateway": gateway_health,
            "gateway_service": gateway_service_health,
            "runtime_sync": runtime_sync_health,
        }
        if plugin_health is not None:
            health["plugin"] = plugin_health
        payload["health"] = health
        payload["steps"] = steps
        print(json.dumps(payload, sort_keys=True))
        return 0 if report.ready and not gateway_failures and not plugin_failures else 1

    print(f"Monica setup plan: {report.rollout_mode}")
    if report.ready and not gateway_failures and not plugin_failures:
        print("Monica is ready for this rollout mode.")
        if steps:
            print("")
            print("Recommended proof setup steps:")
            for index, step in enumerate(steps, start=1):
                print(f"{index}. {step['title']}")
                print(f"   {step['command']}")
                print(f"   {step['why']}")
        return 0
    print("")
    print("Blocking readiness failures:")
    for failure in report.failures:
        print(f"- {failure.message}")
    for failure in gateway_failures:
        print(f"- {failure['message']}")
    for failure in plugin_failures:
        print(f"- {failure['message']}")
    print("")
    print("Next steps:")
    for index, step in enumerate(steps, start=1):
        print(f"{index}. {step['title']}")
        print(f"   {step['command']}")
        print(f"   {step['why']}")
    return 1


def _setup_plan_steps(
    report: Any,
    *,
    config: MonicaConfig | None = None,
    extra_warnings: list[dict[str, str]] | None = None,
    extra_failures: list[dict[str, str]] | None = None,
    runtime_sync_health: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    failure_codes = {str(issue.code) for issue in getattr(report, "failures", ())}
    failure_codes.update(str(item.get("code") or "") for item in extra_failures or [])
    warning_codes = {str(issue.code) for issue in getattr(report, "warnings", ())}
    warning_codes.update(str(item.get("code") or "") for item in extra_warnings or [])
    rollout_mode = str(getattr(report, "rollout_mode", "") or "")
    steps: list[dict[str, str]] = []
    setup_warning_codes = {
        "gateway_not_running",
        "gateway_not_supervised",
        "gateway_status_stale",
        "gateway_heartbeat_missing",
        "gateway_runtime_metadata_missing",
        "runtime_sync_stale",
        "runtime_sync_unrecorded",
        "proof_commands_empty",
        "proof_setup_commands_empty",
        "proof_setup_commands_noop",
        "ios_proof_tooling",
        "android_proof_tooling",
    }
    if not failure_codes and not (warning_codes & setup_warning_codes):
        return []
    if "plugin_not_enabled" in failure_codes:
        _append_setup_step(
            steps,
            step_id="enable_mobile_bug_agent_plugin",
            title="Enable the Monica plugin",
            command=_profiled_hermes_command("plugins", "enable", _MONICA_PLUGIN_NAME),
            why=(
                "The gateway only loads Monica's Slack intake, approval, and runtime-sync hooks "
                "when the bundled plugin is enabled."
            ),
        )
    if not failure_codes and warning_codes & {"runtime_sync_stale", "runtime_sync_unrecorded"}:
        runtime_sync_health = runtime_sync_health or {}
        active_count = int(runtime_sync_health.get("active_run_count") or 0)
        active_runs = runtime_sync_health.get("active_runs") or []
        if active_count:
            labels = ", ".join(_runtime_sync_run_label(run) for run in active_runs[:5])
            if active_count > 5:
                labels += f", +{active_count - 5} more"
            detail = f" Active run(s): {labels}." if labels else ""
            _append_setup_step(
                steps,
                step_id="wait_for_monica_idle_runtime_sync",
                title="Wait for Monica to be idle before Hermes sync",
                command=_profiled_hermes_command(
                    "mobile-bug-agent",
                    "status",
                    "--json",
                ),
                why=(
                    "Hermes update is blocked while Monica has active approved-PR work. "
                    "Re-run status until runtime_sync.idle is true, then run `hermes update`."
                    f"{detail}"
                ),
            )
        else:
            _append_setup_step(
                steps,
                step_id="sync_hermes_runtime",
                title="Sync Hermes runtime while Monica is idle",
                command=_profiled_hermes_command("update"),
                why=(
                    "Monica is idle, so `hermes update` can run the idle-only sync hook "
                    "and record the current Hermes commit/time."
                ),
            )
    if (
        "gateway_not_running" in warning_codes
        or "gateway_not_running" in failure_codes
        or "gateway_not_supervised" in warning_codes
        or "gateway_not_supervised" in failure_codes
    ):
        if rollout_mode == "approved_pr":
            _append_setup_step(
                steps,
                step_id="install_gateway_service",
                title="Install Monica's supervised Slack gateway",
                command=_profiled_hermes_command("gateway", "install"),
                why=(
                    "Approved PR work depends on live Slack tag/DM intake and approvals. "
                    "Install the profile-aware gateway service so Monica stays running "
                    "under the platform supervisor."
                ),
            )
        else:
            _append_setup_step(
                steps,
                step_id="start_gateway",
                title="Start Monica's Slack gateway",
                command=_profiled_hermes_command("gateway", "start"),
                why=(
                    "The gateway must be running for live Slack tag/DM intake and approvals; "
                    "doctor will keep warning until it is running."
                ),
            )
    if (
        "gateway_heartbeat_missing" in warning_codes
        or "gateway_heartbeat_missing" in failure_codes
        or "gateway_runtime_metadata_missing" in warning_codes
        or "gateway_runtime_metadata_missing" in failure_codes
    ):
        _append_setup_step(
            steps,
            step_id="restart_gateway",
            title="Restart Monica's Slack gateway",
            command=_profiled_hermes_command("gateway", "restart"),
            why=(
                "The gateway heartbeat or runtime metadata is incomplete, so restart it "
                "before relying on live Slack tag/DM intake or approvals."
            ),
        )
    if "gateway_status_stale" in warning_codes or "gateway_status_stale" in failure_codes:
        _append_setup_step(
            steps,
            step_id="restart_gateway",
            title="Restart Monica's Slack gateway",
            command=_profiled_hermes_command("gateway", "restart"),
            why=(
                "The gateway heartbeat is stale, so restart it before relying on "
                "live Slack tag/DM intake or approvals."
            ),
        )
    if rollout_mode == "dry_run" and "config_enabled" in failure_codes:
        _append_setup_step(
            steps,
            step_id="configure_dry_run",
            title="Persist Monica's dry-run rollout settings",
            command=_profiled_hermes_command("mobile-bug-agent", "configure-dry-run"),
            why="This enables the Monica plugin and keeps her in no-side-effect dry-run mode.",
        )
    if failure_codes & {
        "slack_bot_token",
        "slack_sdk",
        "slack_scope_app_mentions",
        "slack_scope_chat_write",
        "slack_scope_history",
        "slack_scope_files_read",
        "slack_scope_files_write",
    }:
        _append_setup_step(
            steps,
            step_id="create_slack_app",
            title="Create or update the Monica Slack app",
            command=_profiled_hermes_command("mobile-bug-agent", "slack-manifest"),
            why="Import the manifest in Slack, install the app, and invite Monica to the target channels.",
        )
    if "slack_bot_token" in failure_codes:
        _append_setup_step(
            steps,
            step_id="set_slack_bot_token",
            title="Store Monica's Slack bot token",
            command="Add MONICA_SLACK_BOT_TOKEN=xoxb-... to the Monica profile .env",
            why="Monica needs the bot token to read tagged threads and reply in Slack.",
        )
    if failure_codes & {
        "slack_bot_user_ids_empty",
        "slack_bot_id_value",
        "slack_bot_handle_value",
        "slack_bot_user_id_invalid",
        "slack_allowed_channels_empty",
        "slack_allowed_channel_invalid",
    }:
        _append_setup_step(
            steps,
            step_id="discover_slack_ids",
            title="Discover Monica's Slack user ID and channel IDs",
            command=_profiled_hermes_command("mobile-bug-agent", "slack-metadata", "--json"),
            why="Use auth.bot_user_id for --bot-user-id and selected C.../G... channel IDs for --channel-id.",
        )
    if "linear_api_key" in failure_codes:
        _append_setup_step(
            steps,
            step_id="set_linear_api_key",
            title="Store the Linear API key",
            command="Add LINEAR_API_KEY=lin_api_... to ~/.hermes/.env",
            why="Monica needs Linear access to create or update mobile bug tickets.",
        )
    if failure_codes & {"linear_api_key", "linear_team_id"}:
        _append_setup_step(
            steps,
            step_id="discover_linear_ids",
            title="Discover Linear team, project, and label IDs",
            command=_profiled_hermes_command("mobile-bug-agent", "linear-metadata", "--json"),
            why="Use the selected team ID, and optionally project/label IDs, in Monica's rollout config.",
        )
    if rollout_mode in {"linear_only", "local_fix_only", "approved_pr"} and failure_codes & {
        "config_enabled",
        "slack_bot_user_ids_empty",
        "slack_bot_id_value",
        "slack_bot_handle_value",
        "slack_bot_user_id_invalid",
        "slack_allowed_channels_empty",
        "slack_allowed_channel_invalid",
        "linear_team_id",
        "loop_create_linear",
    }:
        _append_setup_step(
            steps,
            step_id="configure_linear_only",
            title="Persist Monica's Linear-only rollout settings",
            command=_profiled_hermes_command(
                "mobile-bug-agent",
                "configure-linear-only",
                "--bot-user-id",
                "<U...>",
                "--channel-id",
                "<C...>",
                "--linear-team-id",
                "<team-id>",
            ),
            why="This enables Monica only in explicitly allowed Slack channels and lets her file Linear issues.",
        )
    if rollout_mode == "local_fix_only" and failure_codes & {
        "repo_url",
        "verification_commands",
        "slack_approvers_empty",
        "slack_approver_handle_value",
        "slack_approver_invalid",
        "repo_local_name",
        "repo_branch_prefix",
        "repo_default_branch",
        "loop_require_fix_approval",
        "worker_session_prefix",
    }:
        _append_setup_step(
            steps,
            step_id="configure_local_fix_only",
            title="Persist local code-fix rollout settings",
            command=_profiled_hermes_command(
                "mobile-bug-agent",
                "configure-local-fix-only",
                "--approver-user-id",
                "<U...>",
                "--repo-url",
                "<repo-url>",
                "--verification-command",
                "<command>",
            ),
            why="This configures the React Native repo, approver allowlist, and verification gate without enabling push or PR creation.",
        )
    if rollout_mode == "approved_pr" and failure_codes & {
        "repo_url",
        "verification_commands",
        "slack_approvers_empty",
        "slack_approver_handle_value",
        "slack_approver_invalid",
        "repo_local_name",
        "repo_branch_prefix",
        "repo_default_branch",
        "loop_require_fix_approval",
        "worker_session_prefix",
    }:
        _append_setup_step(
            steps,
            step_id="configure_approved_pr",
            title="Persist approved draft-PR rollout settings",
            command=_profiled_hermes_command(
                "mobile-bug-agent",
                "configure-approved-pr",
                "--approver-user-id",
                "<U...>",
                "--repo-url",
                "<repo-url>",
                "--verification-command",
                "<command>",
                "--proof-setup-command",
                "<auth/session seed command>",
                "--proof-required-env-key",
                "<test-auth env key>",
                "--proof-command",
                "<simulator proof command>",
            ),
            why=(
                "This configures the React Native repo, approver allowlist, verification gate, "
                "and non-secret proof setup/proof commands."
            ),
        )
    if failure_codes & {"repo_cached_path", "repo_cached_status", "repo_cached_dirty"}:
        _append_setup_step(
            steps,
            step_id="clean_cached_mobile_repo",
            title="Inspect and clean Monica's cached mobile repo",
            command=_cached_mobile_repo_status_command(config),
            why=(
                "Monica creates fresh run worktrees from the cached mobile repo, so cached "
                "uncommitted changes can poison future approved-PR fixes. Review the status, "
                "then intentionally clean, stash, or archive the cached repo before approving "
                "new code work."
            ),
        )
    if rollout_mode in {"local_fix_only", "approved_pr"} and failure_codes & {
        "git_executable",
        "codex_executable",
        "codex_approval_policy",
        "codex_sandbox",
    }:
        _append_setup_step(
            steps,
            step_id="prepare_code_tools",
            title="Prepare local code-fix tooling",
            command="git --version && codex --version",
            why="Code rollout modes need Git and a non-interactive Codex CLI worker.",
        )
    if rollout_mode == "approved_pr" and failure_codes & {
        "gh_executable",
        "github_token",
        "github_auth",
    }:
        _append_setup_step(
            steps,
            step_id="prepare_pr_tools",
            title="Prepare draft-PR tooling",
            command="gh auth status || gh auth login -h github.com",
            why="Approved-PR mode needs GitHub CLI auth for pushing branches and opening draft PRs.",
        )
    proof_command_failure_codes = {
        "proof_commands_empty",
        "proof_commands_noop",
        "proof_commands_placeholder",
        "proof_commands_inline_secret",
        "proof_commands_literal_secret",
        "proof_ios_dev_client_scheme",
        "proof_ios_bundle_id",
        "proof_android_package",
    }
    if (
        warning_codes & {"proof_commands_empty"}
        or failure_codes & proof_command_failure_codes
    ):
        _append_setup_step(
            steps,
            step_id="configure_simulator_proof",
            title="Configure Monica simulator proof command",
            command=(
                "Set mobile_bug_agent.proof.commands to "
                "`uv run --project \"$MONICA_HERMES_AGENT_ROOT\" python -m "
                "plugins.mobile_bug_agent.simulator_proof --timeout-seconds 600`, "
                "then set proof.dev_client_scheme, proof.ios_bundle_id, and "
                "proof.android_package for real-app simulator proof."
            ),
            why="Stage D/E/F cannot complete until proof commands write screenshots or recordings to MONICA_PROOF_DIR.",
        )
    if rollout_mode == "approved_pr" and "proof_platform_order" in failure_codes:
        _append_setup_step(
            steps,
            step_id="configure_proof_platform_order",
            title="Require iOS and Android simulator proof",
            command="Set mobile_bug_agent.proof.platform_order to `['ios', 'android']`.",
            why=(
                "approved_pr cannot open a PR unless Monica captures proof artifacts "
                "for both iOS and Android."
            ),
        )
    setup_command_failure_codes = {
        "proof_setup_commands_empty",
        "proof_setup_commands_noop",
        "proof_setup_commands_placeholder",
        "proof_setup_commands_inline_secret",
        "proof_setup_commands_literal_secret",
    }
    if (
        warning_codes & {"proof_setup_commands_empty"}
        or failure_codes & setup_command_failure_codes
    ):
        _append_setup_step(
            steps,
            step_id="configure_proof_setup_commands",
            title="Configure Monica proof setup/auth command",
            command=_approved_pr_setup_command_configure_step(config),
            why=(
                "Monica runs setup commands before proof with MONICA_WORKTREE, MONICA_PROOF_DIR, "
                "MONICA_LINEAR_URL, MONICA_BRANCH_NAME, MONICA_BASE_REF, MONICA_BASE_COMMIT, "
                "MONICA_DEEP_LINK, MONICA_PROOF_EXPECTED_TEXT, MONICA_PROOF_SCREEN, simulator "
                "IDs/packages, and credentials loaded from the Monica profile .env. The proof "
                "capture must also emit target route evidence, such as a screen.load marker for "
                "the fixed screen."
            ),
        )
    if (
        rollout_mode == "approved_pr"
        and not (failure_codes & setup_command_failure_codes)
        and failure_codes & {
            "proof_required_env_keys_empty",
            "proof_required_env_keys",
            "proof_required_env_keys_invalid",
        }
    ):
        _append_setup_step(
            steps,
            step_id="configure_proof_required_env_keys",
            title="Configure Monica proof environment secrets",
            command=_proof_required_env_keys_setup_step(config, failure_codes),
            why=(
                "Proof setup and simulator capture receive credentials from the Monica "
                "profile .env; proof.required_env_keys records only the non-secret key names."
            ),
        )
    if warning_codes & {"ios_proof_tooling"}:
        _append_setup_step(
            steps,
            step_id="prepare_ios_simulator",
            title="Prepare iOS simulator proof tooling",
            command="xcrun --find simctl && xcodebuild -version",
            why=(
                "Install full Xcode, open it once, accept the license, select "
                "/Applications/Xcode.app/Contents/Developer, and install an iOS simulator runtime."
            ),
        )
    if warning_codes & {"android_proof_tooling"}:
        _append_setup_step(
            steps,
            step_id="prepare_android_emulator",
            title="Prepare Android emulator proof tooling",
            command="adb version && emulator -list-avds",
            why=(
                "Install Android SDK platform-tools and emulator, create an AVD, "
                "and make adb/emulator available on PATH."
            ),
        )
    _append_setup_step(
        steps,
        step_id="rerun_doctor",
        title="Re-run the rollout readiness check",
        command=_profiled_hermes_command(
            "mobile-bug-agent",
            "doctor",
            "--rollout-mode",
            rollout_mode or "linear_only",
            "--json",
        ),
        why="Only continue to a live Slack test once Monica reports ready.",
    )
    return steps


def _append_setup_step(
    steps: list[dict[str, str]],
    *,
    step_id: str,
    title: str,
    command: str,
    why: str,
) -> None:
    if any(step["id"] == step_id for step in steps):
        return
    steps.append(
        {
            "id": step_id,
            "title": title,
            "command": command,
            "why": why,
        }
    )


def _approved_pr_setup_command_configure_step(config: MonicaConfig | None) -> str:
    if config is None:
        return (
            "Ask the mobile team for the simulator test-auth/session seed command, "
            "then run `hermes mobile-bug-agent configure-proof-setup "
            "--proof-setup-command '<auth/session seed command>' "
            "--proof-required-env-key '<test-auth env key>'`."
        )

    parts = _profiled_hermes_command_parts("mobile-bug-agent", "configure-proof-setup")
    parts.extend(["--proof-setup-command", "<auth/session seed command>"])
    configured_required_env_keys = _clean_required_env_key_sequence(config.proof.required_env_keys)
    if not configured_required_env_keys or _invalid_required_env_keys(configured_required_env_keys):
        parts.extend(["--proof-required-env-key", "<test-auth env key>"])
    return " ".join(shlex.quote(str(part)) for part in parts)


def _cached_mobile_repo_status_command(config: MonicaConfig | None) -> str:
    if config is None:
        return "Inspect Monica's cached mobile repo under mobile_bug_agent.runtime.home_subdir/workspace/repos."
    try:
        local_name = safe_repo_local_name(config.repo.local_name)
        repo_path = runtime_root(config) / "workspace" / "repos" / local_name
    except (RepoManagerError, ValueError):
        return "Inspect Monica's cached mobile repo under mobile_bug_agent.runtime.home_subdir/workspace/repos."
    return f"git -C {shlex.quote(str(repo_path))} status --short --branch"


def _proof_required_env_keys_setup_step(
    config: MonicaConfig | None,
    failure_codes: set[str],
) -> str:
    if config is None:
        return "Add the configured proof.required_env_keys values to the Monica profile .env."

    if "proof_required_env_keys_invalid" in failure_codes:
        keys = _suggested_required_env_keys_for_invalid_config(
            config.proof.required_env_keys
        )
        parts = _profiled_hermes_command_parts("mobile-bug-agent", "configure-proof-setup")
        _append_configured_proof_setup_command_args(parts, config)
        for key in keys:
            parts.extend(["--proof-required-env-key", key])
        return " ".join(shlex.quote(str(part)) for part in parts)
    if "proof_required_env_keys_empty" in failure_codes:
        parts = _profiled_hermes_command_parts("mobile-bug-agent", "configure-proof-setup")
        _append_configured_proof_setup_command_args(parts, config)
        parts.extend(["--proof-required-env-key", "MONICA_TEST_LOGIN_TOKEN"])
        return " ".join(shlex.quote(str(part)) for part in parts)

    keys = _clean_sequence(config.proof.required_env_keys)
    key_text = ", ".join(keys) if keys else "the configured proof.required_env_keys"
    return f"Add {key_text} to the Monica profile .env, then rerun doctor."


def _append_configured_proof_setup_command_args(
    parts: list[str],
    config: MonicaConfig,
) -> None:
    setup_commands = _clean_sequence(config.proof.setup_commands)
    if not setup_commands:
        parts.extend(["--proof-setup-command", "<auth/session seed command>"])
        return
    for command in setup_commands:
        parts.extend(["--proof-setup-command", command])


def _suggested_required_env_keys_for_invalid_config(keys: tuple[str, ...]) -> tuple[str, ...]:
    suggested: list[str] = []
    seen: set[str] = set()
    for key in _invalid_required_env_keys(keys):
        candidate = str(key or "").strip()
        if (
            not candidate
            or candidate in seen
            or _invalid_required_env_keys((candidate,))
        ):
            continue
        seen.add(candidate)
        suggested.append(candidate)
    return tuple(suggested) or ("MONICA_TEST_LOGIN_TOKEN",)


def _profiled_hermes_command(*args: Any) -> str:
    return " ".join(shlex.quote(str(part)) for part in _profiled_hermes_command_parts(*args))


def _profiled_hermes_command_parts(*args: Any) -> list[str]:
    parts = ["hermes"]
    profile = _active_named_profile()
    if profile:
        parts.extend(["-p", profile])
    parts.extend(str(arg) for arg in args)
    return parts


def _active_named_profile() -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name

        profile = str(get_active_profile_name() or "").strip()
    except Exception:
        return ""
    return "" if profile in {"", "default"} else profile


def _readiness_report_payload(report: Any) -> dict[str, Any]:
    return {
        "ready": report.ready,
        "rollout_mode": report.rollout_mode,
        "runtime_root": report.runtime_root_value,
        "warnings": [_readiness_issue_payload(issue) for issue in report.warnings],
        "failures": [_readiness_issue_payload(issue) for issue in report.failures],
    }


def _readiness_issue_payload(issue: Any) -> dict[str, str]:
    return {
        "code": str(issue.code),
        "message": str(issue.message),
    }


def _config_for_doctor_target(
    *,
    config: MonicaConfig,
    target_rollout_mode: str = "",
) -> MonicaConfig:
    target = str(target_rollout_mode or "").strip()
    if not target:
        return config
    if target not in {"dry_run", "linear_only", "local_fix_only", "approved_pr"}:
        return replace(config, rollout_mode=target)
    return replace(config, rollout_mode=target, dry_run=(target == "dry_run"))


def _runtime_sync_block_message() -> str:
    return (
        "Hermes is updating; Monica is temporarily paused. "
        "Retry after the update finishes."
    )


def _runtime_sync_block_reason(state: MonicaState) -> str:
    gate = state.runtime_sync_gate()
    if gate.get("open", True):
        return ""
    return _runtime_sync_block_message()


def run_retry_command(
    *,
    state: MonicaState,
    run_id: str,
    config: MonicaConfig | None = None,
    readiness_checker: Any | None = None,
    json_output: bool = False,
) -> int:
    run = state.get_run(run_id)
    if run is None:
        message = f"Monica run not found: {run_id}"
        if json_output:
            _print_operator_error_json(action="retry", code="not_found", message=message)
            return 1
        print(message)
        return 1
    if run.status not in RETRYABLE_STATUSES:
        message = f"Monica run {run.id} is not retryable from status `{run.status}`."
        if json_output:
            _print_operator_error_json(
                action="retry",
                code="not_retryable",
                message=message,
                run=run,
            )
            return 1
        print(message)
        return 1
    if _is_cancelled_run(run):
        message = (
            f"Monica run {run.id} was cancelled from Slack and is not retryable from the CLI. "
            "Tag Monica again in the Slack thread with new context to start a fresh pass."
        )
        if json_output:
            _print_operator_error_json(
                action="retry",
                code="cancelled",
                message=message,
                run=run,
            )
            return 1
        print(message)
        return 1
    if str(run.pr_url or "").strip():
        message = (
            f"Monica run {run.id} already has a draft PR: {run.pr_url}. "
            "Open a new tagged Slack request for additional work instead of retrying this run."
        )
        if json_output:
            _print_operator_error_json(
                action="retry",
                code="pr_exists",
                message=message,
                run=run,
            )
            return 1
        print(message)
        return 1
    runtime_sync_reason = _runtime_sync_block_reason(state)
    if runtime_sync_reason:
        if json_output:
            _print_operator_error_json(
                action="retry",
                code="runtime_sync_in_progress",
                message=runtime_sync_reason,
                run=run,
            )
            return 1
        print(runtime_sync_reason)
        return 1
    branch_name = str(run.branch_name or "").strip()
    is_proof_resume = run.status in {"proof_blocked", "proofing"} and bool(branch_name)
    is_opening_pr_resume = (
        run.status == "failed"
        and str(run.failure_reason or "").startswith("opening_pr_failed:")
        and bool(branch_name)
        and bool(str(run.approved_by_user_id or "").strip())
    )
    is_existing_branch_resume = is_proof_resume or is_opening_pr_resume
    next_status = "proof_blocked" if is_existing_branch_resume else ("approved" if run.approved_by_user_id else "queued")
    if (
        (
            next_status == "approved"
            or (is_existing_branch_resume and config is not None and config.rollout_mode == "approved_pr")
        )
        and config is not None
        and not _is_allowed_local_approver(config=config, user_id=run.approved_by_user_id)
    ):
        message = (
            f"Monica run {run.id} cannot retry from approval because stored approver "
            f"{run.approved_by_user_id} is not configured as a Monica code approver."
        )
        if json_output:
            _print_operator_error_json(
                action="retry",
                code="approver_not_allowed",
                message=message,
                run=run,
            )
            return 1
        print(message)
        return 1
    should_check_readiness = (
        config is not None
        and config.rollout_mode in {"local_fix_only", "approved_pr"}
        and (next_status == "approved" or is_existing_branch_resume)
    )
    if should_check_readiness:
        checker = readiness_checker or (lambda: run_doctor_command(config=config))
        try:
            readiness_exit_code = int(checker())
        except Exception as exc:
            message = f"Monica readiness check failed: {exc}"
            if json_output:
                _print_operator_error_json(
                    action="retry",
                    code="readiness_exception",
                    message=message,
                    run=run,
                )
                return 1
            print(message)
            return 1
        if readiness_exit_code != 0:
            message = "Monica readiness check failed; not retrying the approved Monica run."
            if json_output:
                _print_operator_error_json(
                    action="retry",
                    code="readiness_failed",
                    message=message,
                    run=run,
                )
                return 1
            print(message)
            return 1
    state.update_run(
        run.id,
        status=next_status,
        failure_reason="",
        branch_name=run.branch_name if is_existing_branch_resume else "",
        base_branch=run.base_branch if is_existing_branch_resume else "",
        base_commit=run.base_commit if is_existing_branch_resume else "",
        proof_deep_link=run.proof_deep_link if is_existing_branch_resume else "",
        proof_expected_text=run.proof_expected_text if is_existing_branch_resume else "",
        pr_url="",
    )
    _run_loop(run.id, state=state)
    if json_output:
        _print_operator_success_json(action="retry", run=state.get_run(run.id) or run)
        return 0
    print(f"Retried Monica run {run.id}.")
    return 0


def run_approve_command(
    *,
    state: MonicaState,
    run_id: str,
    user_id: str,
    config: MonicaConfig | None = None,
    readiness_checker: Any | None = None,
    json_output: bool = False,
) -> int:
    run = state.get_run(run_id)
    if run is None:
        message = f"Monica run not found: {run_id}"
        if json_output:
            _print_operator_error_json(action="approve", code="not_found", message=message)
            return 1
        print(message)
        return 1
    if run.status != "awaiting_fix_approval":
        message = (
            f"Monica run {run.id} is not awaiting fix approval; current status is `{run.status}`."
        )
        if json_output:
            _print_operator_error_json(
                action="approve",
                code="not_awaiting_approval",
                message=message,
                run=run,
            )
            return 1
        print(message)
        return 1
    if config is not None and not _is_allowed_local_approver(config=config, user_id=user_id):
        message = f"{user_id} is not configured as a Monica code approver."
        if json_output:
            _print_operator_error_json(
                action="approve",
                code="approver_not_allowed",
                message=message,
                run=run,
            )
            return 1
        print(message)
        return 1
    runtime_sync_reason = _runtime_sync_block_reason(state)
    if runtime_sync_reason:
        if json_output:
            _print_operator_error_json(
                action="approve",
                code="runtime_sync_in_progress",
                message=runtime_sync_reason,
                run=run,
            )
            return 1
        print(runtime_sync_reason)
        return 1
    if config is not None and config.rollout_mode in {"local_fix_only", "approved_pr"}:
        checker = readiness_checker or (lambda: run_doctor_command(config=config))
        try:
            readiness_exit_code = int(checker())
        except Exception as exc:
            message = f"Monica readiness check failed: {exc}"
            if json_output:
                _print_operator_error_json(
                    action="approve",
                    code="readiness_exception",
                    message=message,
                    run=run,
                )
                return 1
            print(message)
            return 1
        if readiness_exit_code != 0:
            message = "Monica readiness check failed; not approving the Monica run."
            if json_output:
                _print_operator_error_json(
                    action="approve",
                    code="readiness_failed",
                    message=message,
                    run=run,
                )
                return 1
            print(message)
            return 1
    approve_once = getattr(state, "approve_fix_once", None)
    if callable(approve_once):
        approved, changed = approve_once(run.id, approved_by_user_id=user_id)
    else:
        approved = state.approve_fix(run.id, approved_by_user_id=user_id)
        changed = True
    if not changed:
        message = f"Monica run {run.id} is already approved or no longer awaiting fix approval."
        if json_output:
            _print_operator_error_json(
                action="approve",
                code="already_approved_or_changed",
                message=message,
                run=state.get_run(run.id) or run,
            )
            return 1
        print(message)
        return 1
    _run_loop(approved.id, state=state)
    if json_output:
        _print_operator_success_json(action="approve", run=state.get_run(approved.id) or approved)
        return 0
    print(f"Approved Monica run {run.id}.")
    return 0


def run_sync_approvals_command(
    *,
    config: MonicaConfig,
    state: MonicaState,
    run_id: str = "",
    limit: int = 20,
    json_output: bool = False,
    client_factory: Any = SlackThreadClient.from_token,
    readiness_checker: Any | None = None,
) -> int:
    runs = _approval_sync_runs(state=state, run_id=run_id, limit=limit)
    if run_id and not runs:
        message = f"Monica run not found: {run_id}"
        if json_output:
            _print_sync_approvals_json(ok=False, results=[], error={"code": "not_found", "message": message})
            return 1
        print(message)
        return 1
    runtime_sync_reason = _runtime_sync_block_reason(state)
    if runtime_sync_reason:
        if json_output:
            _print_sync_approvals_json(
                ok=False,
                results=[],
                error={
                    "code": "runtime_sync_in_progress",
                    "message": runtime_sync_reason,
                },
            )
            return 1
        print(runtime_sync_reason)
        return 1

    token = monica_slack_bot_token()
    if not token:
        message = f"{MONICA_SLACK_BOT_TOKEN} is missing; cannot read Slack approval threads."
        if json_output:
            _print_sync_approvals_json(ok=False, results=[], error={"code": "slack_bot_token_missing", "message": message})
            return 1
        print(message)
        return 1
    if not config.slack.approver_user_ids:
        message = "mobile_bug_agent.slack.approver_user_ids is empty; cannot sync approvals."
        if json_output:
            _print_sync_approvals_json(ok=False, results=[], error={"code": "approvers_empty", "message": message})
            return 1
        print(message)
        return 1

    try:
        client = client_factory(
            token=token,
            monica_user_ids=config.slack.bot_user_ids,
            download_attachments=False,
        )
    except Exception as exc:
        message = f"Could not create Slack client: {exc}"
        if json_output:
            _print_sync_approvals_json(ok=False, results=[], error={"code": "slack_client_failed", "message": message})
            return 1
        print(message)
        return 1

    results: list[dict[str, Any]] = []
    exit_code = 0
    for run in runs:
        result = _sync_one_approval(
            config=config,
            state=state,
            run=run,
            client=client,
            readiness_checker=readiness_checker,
        )
        results.append(result)
        if result.get("error"):
            exit_code = 1

    if json_output:
        _print_sync_approvals_json(ok=exit_code == 0, results=results)
        return exit_code

    synced = [item for item in results if item.get("action") == "approved"]
    if synced:
        for item in synced:
            print(
                f"Synced Slack approval for Monica run {item['run_id']} "
                f"from {item.get('approved_by_user_id', 'unknown')}."
            )
    else:
        print("No pending Monica Slack approvals were found.")
    for item in results:
        if item.get("action") != "approved":
            print(f"- {item['run_id']}: {item.get('reason', 'no approval')}")
    return exit_code


def _approval_sync_runs(*, state: MonicaState, run_id: str, limit: int) -> list[Any]:
    clean_run_id = str(run_id or "").strip()
    if clean_run_id:
        run = state.get_run(clean_run_id)
        return [run] if run is not None else []
    return [
        run
        for run in state.list_runs(limit=max(1, int(limit or 20)))
        if run.status == "awaiting_fix_approval"
    ]


def _sync_one_approval(
    *,
    config: MonicaConfig,
    state: MonicaState,
    run: Any,
    client: Any,
    readiness_checker: Any | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "run_id": run.id,
        "status": run.status,
        "thread_ts": run.thread_ts,
        "channel_id": run.channel_id,
    }
    if run.status != "awaiting_fix_approval":
        payload.update(action="skipped", reason=f"not awaiting approval: {run.status}")
        return payload
    try:
        thread = client.read_thread(
            channel_id=run.channel_id,
            thread_ts=run.thread_ts,
            limit=max(2, config.loop.max_thread_messages),
        )
    except SlackClientError as exc:
        payload.update(action="error", error={"code": "slack_read_failed", "message": str(exc)})
        return payload

    approval = _find_thread_approval(config=config, run=run, thread=thread)
    if approval is None:
        payload.update(action="skipped", reason="no configured approver approval found in Slack thread")
        return payload

    ready, reason = _approval_readiness(config=config, readiness_checker=readiness_checker)
    if not ready:
        payload.update(action="error", error={"code": "readiness_failed", "message": reason})
        return payload

    approved, changed = state.approve_fix_once(run.id, approved_by_user_id=approval["user_id"])
    if not changed:
        payload.update(action="skipped", reason="already approved or status changed")
        return payload
    _run_loop(approved.id, state=state)
    updated = state.get_run(approved.id) or approved
    payload.update(
        action="approved",
        approved_by_user_id=approval["user_id"],
        approval_message_ts=approval["ts"],
        final_status=updated.status,
        pr_url=updated.pr_url,
    )
    return payload


def _find_thread_approval(*, config: MonicaConfig, run: Any, thread: Any) -> dict[str, str] | None:
    approvers = set(config.slack.approver_user_ids)
    bot_ids = set(config.slack.bot_user_ids)
    waiting_since = _run_updated_at_epoch(run)
    for message in getattr(thread, "messages", []) or []:
        message_ts = str(getattr(message, "ts", "") or "")
        if message_ts and message_ts == str(getattr(run, "message_ts", "") or ""):
            continue
        if not _message_is_after_waiting_since(message_ts, waiting_since):
            continue
        user_id = str(getattr(message, "user_id", "") or "")
        if user_id in bot_ids or user_id not in approvers:
            continue
        text = str(getattr(message, "text", "") or "")
        if is_approval_text(text) and _recovered_approval_targets_monica(
            config=config,
            run=run,
            text=text,
        ):
            return {"user_id": user_id, "ts": message_ts}
    return None


def _recovered_approval_targets_monica(
    *,
    config: MonicaConfig,
    run: Any,
    text: str,
) -> bool:
    if _run_is_slack_direct_message(run):
        return True
    configured_ids = {
        str(user_id).strip()
        for user_id in config.slack.bot_user_ids
        if str(user_id).strip()
    }
    if not configured_ids:
        return False
    mentioned_ids = set(_SLACK_MENTION_RE.findall(str(text or "")))
    return bool(configured_ids & mentioned_ids)


def _run_is_slack_direct_message(run: Any) -> bool:
    raw_event = getattr(run, "raw_event", None)
    if isinstance(raw_event, dict):
        channel_type = str(raw_event.get("channel_type") or "").strip()
        if channel_type == "im":
            return True
        if channel_type == "mpim":
            return False
    channel_id = str(getattr(run, "channel_id", "") or "").strip()
    return channel_id.startswith("D")


def _message_is_after_waiting_since(message_ts: str, waiting_since: float | None) -> bool:
    if waiting_since is None:
        return True
    message_epoch = _slack_ts_epoch(message_ts)
    if message_epoch is None:
        return True
    return message_epoch >= waiting_since


def _slack_ts_epoch(value: str) -> float | None:
    try:
        return float(str(value or "").strip())
    except (TypeError, ValueError):
        return None


def _run_updated_at_epoch(run: Any) -> float | None:
    value = str(getattr(run, "updated_at", "") or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _approval_readiness(
    *,
    config: MonicaConfig,
    readiness_checker: Any | None,
) -> tuple[bool, str]:
    if config.rollout_mode not in {"local_fix_only", "approved_pr"}:
        return True, ""
    checker = readiness_checker or (lambda: run_doctor_command(config=config))
    try:
        exit_code = int(checker())
    except Exception as exc:
        return False, f"Monica readiness check failed: {exc}"
    if exit_code != 0:
        return False, "Monica readiness check failed."
    return True, ""


def _print_sync_approvals_json(
    *,
    ok: bool,
    results: list[dict[str, Any]],
    error: dict[str, str] | None = None,
) -> None:
    payload: dict[str, Any] = {"ok": ok, "results": results}
    if error is not None:
        payload["error"] = error
    print(json.dumps(payload, sort_keys=True))


def run_simulate_command(
    *,
    config: MonicaConfig,
    state: MonicaState,
    text: str,
    channel_id: str,
    user_id: str,
    thread_ts: str = "",
    allow_side_effects: bool = False,
    json_output: bool = False,
    loop_runner: Any | None = None,
    readiness_checker: Any | None = None,
) -> int:
    if not config.enabled:
        if json_output:
            _print_simulation_error_json(
                code="config_disabled",
                message="Monica is disabled: set mobile_bug_agent.enabled: true before simulating.",
                config=config,
                allow_side_effects=allow_side_effects,
            )
            return 1
        print("Monica is disabled: set mobile_bug_agent.enabled: true before simulating.")
        return 1
    runtime_sync_reason = _runtime_sync_block_reason(state)
    if runtime_sync_reason:
        if json_output:
            _print_simulation_error_json(
                code="runtime_sync_in_progress",
                message=runtime_sync_reason,
                config=config,
                allow_side_effects=allow_side_effects,
            )
            return 1
        print(runtime_sync_reason)
        return 1
    clean_text = " ".join(str(text or "").split())
    if not clean_text:
        if json_output:
            _print_simulation_error_json(
                code="empty_text",
                message="Simulation text is required.",
                config=config,
                allow_side_effects=allow_side_effects,
            )
            return 2
        print("Simulation text is required.")
        return 2
    if config.rollout_mode != "dry_run" and not allow_side_effects:
        message = (
            "Simulation is in a side-effect rollout mode "
            f"(`{config.rollout_mode}`). Re-run with --allow-side-effects only if you "
            "intend to create/update Linear issues or start a code-fix loop."
        )
        if json_output:
            _print_simulation_error_json(
                code="side_effects_not_allowed",
                message=message,
                config=config,
                allow_side_effects=allow_side_effects,
            )
            return 1
        print(message)
        return 1
    if config.rollout_mode in {"linear_only", "local_fix_only", "approved_pr"} and allow_side_effects:
        allowed_channels = set(config.slack.allowed_channels)
        if not allowed_channels:
            message = (
                "Simulation side effects require mobile_bug_agent.slack.allowed_channels. "
                "Configure the exact Slack channel IDs Monica may act in."
            )
            if json_output:
                _print_simulation_error_json(
                    code="allowed_channels_empty",
                    message=message,
                    config=config,
                    allow_side_effects=allow_side_effects,
                )
                return 1
            print(message)
            return 1
        if not config.slack.bot_user_ids:
            message = (
                "Simulation side effects require mobile_bug_agent.slack.bot_user_ids. "
                "Configure Monica's Slack mention user ID before exercising real side effects."
            )
            if json_output:
                _print_simulation_error_json(
                    code="bot_user_ids_empty",
                    message=message,
                    config=config,
                    allow_side_effects=allow_side_effects,
                )
                return 1
            print(message)
            return 1
        if channel_id not in allowed_channels:
            message = (
                f"Simulation channel {channel_id} is not in "
                "mobile_bug_agent.slack.allowed_channels."
            )
            if json_output:
                _print_simulation_error_json(
                    code="channel_not_allowed",
                    message=message,
                    config=config,
                    allow_side_effects=allow_side_effects,
                )
                return 1
            print(message)
            return 1
        if config.rollout_mode in {"local_fix_only", "approved_pr"} and not config.slack.approver_user_ids:
            message = (
                "Simulation code-fix side effects require "
                "mobile_bug_agent.slack.approver_user_ids. Configure at least one "
                "Monica code approver."
            )
            if json_output:
                _print_simulation_error_json(
                    code="approver_user_ids_empty",
                    message=message,
                    config=config,
                    allow_side_effects=allow_side_effects,
                )
                return 1
            print(message)
            return 1
        checker = readiness_checker or (lambda: run_doctor_command(config=config))
        try:
            readiness_exit_code = int(checker())
        except Exception as exc:
            message = f"Monica readiness check failed: {exc}"
            if json_output:
                _print_simulation_error_json(
                    code="readiness_exception",
                    message=message,
                    config=config,
                    allow_side_effects=allow_side_effects,
                )
                return 1
            print(message)
            return 1
        if readiness_exit_code != 0:
            if json_output:
                _print_simulation_error_json(
                    code="readiness_failed",
                    message="Monica readiness check failed; not creating a simulated side-effect run.",
                    config=config,
                    allow_side_effects=allow_side_effects,
                )
                return 1
            print("Monica readiness check failed; not creating a simulated side-effect run.")
            return 1

    now_value = time.time()
    now = f"{now_value:.6f}"
    thread = thread_ts.strip() or _next_simulated_thread_ts(now_value)
    run, created = state.create_run_once(
        platform="slack",
        channel_id=channel_id,
        thread_ts=thread,
        message_ts=now,
        user_id=user_id,
        request_text=clean_text,
        raw_event={
            "monica_simulated": True,
            "type": "app_mention",
            "text": f"<@MONICA_SIM> {clean_text}",
            "channel": channel_id,
            "user": user_id,
            "thread_ts": thread,
            "ts": now,
            "permalink": f"local://monica/simulated/{thread}",
            "files": [],
        },
    )
    if not created:
        if json_output:
            print(
                json.dumps(
                    {
                        "created": False,
                        "rollout_mode": config.rollout_mode,
                        "allow_side_effects": allow_side_effects,
                        "error": {
                            "code": "duplicate_thread",
                            "message": (
                                f"Monica simulation already exists for channel `{channel_id}` "
                                f"thread `{thread}`."
                            ),
                        },
                        "run": _run_payload(run, include_raw_event=True),
                    },
                    sort_keys=True,
                )
            )
            return 1
        print(
            f"Monica simulation already exists for channel `{channel_id}` thread `{thread}`: {run.id}. "
            "Use a different --thread-ts to start a new local simulation."
        )
        return 1
    runner = loop_runner or _run_loop
    runner(run.id, state=state)
    updated = state.get_run(run.id) or run
    if json_output:
        print(
            json.dumps(
                {
                    "created": True,
                    "rollout_mode": config.rollout_mode,
                    "allow_side_effects": allow_side_effects,
                    "run": _run_payload(updated, include_raw_event=True),
                },
                sort_keys=True,
            )
        )
        return 0
    print(f"Simulated Monica run {run.id}: {updated.status}")
    if updated.linear_identifier:
        print(f"Linear: {updated.linear_identifier}")
    if updated.linear_url:
        print(f"Linear URL: {updated.linear_url}")
    if updated.pr_url:
        print(f"PR: {updated.pr_url}")
    if updated.failure_reason:
        print(f"Reason: {updated.failure_reason}")
    return 0


def _print_simulation_error_json(
    *,
    code: str,
    message: str,
    config: MonicaConfig,
    allow_side_effects: bool,
) -> None:
    print(
        json.dumps(
            {
                "created": False,
                "rollout_mode": config.rollout_mode,
                "allow_side_effects": allow_side_effects,
                "error": {
                    "code": code,
                    "message": message,
                },
            },
            sort_keys=True,
        )
    )


def _print_operator_success_json(*, action: str, run: Any) -> None:
    print(
        json.dumps(
                {
                    "ok": True,
                    "action": action,
                    "run": _run_payload(run, include_raw_event=False),
                },
                sort_keys=True,
            )
    )


def _print_operator_error_json(
    *,
    action: str,
    code: str,
    message: str,
    run: Any | None = None,
) -> None:
    payload: dict[str, Any] = {
        "ok": False,
        "action": action,
        "error": {
            "code": code,
            "message": message,
        },
    }
    if run is not None:
        payload["run"] = _run_payload(run, include_raw_event=False)
    print(json.dumps(payload, sort_keys=True))


def _run_loop(run_id: str, *, state: MonicaState) -> None:
    config = load_monica_config()
    skills = DefaultMonicaSkills(config=config, state=state)
    MonicaLoop(config=config, state=state, skills=skills).run(run_id)


def _open_state(config: MonicaConfig | None = None) -> MonicaState:
    return MonicaState.open(runtime_root(config or load_monica_config()) / "state.sqlite")


def _one_line(value: Any, limit: int = 100) -> str:
    return " ".join(str(value).split())[:limit]


def _next_simulated_thread_ts(now: float) -> str:
    global _SIMULATION_THREAD_SEQUENCE
    seconds = int(now)
    micros = int((now - seconds) * 1_000_000)
    _SIMULATION_THREAD_SEQUENCE = (_SIMULATION_THREAD_SEQUENCE + 1) % 1_000_000
    micros = (micros + _SIMULATION_THREAD_SEQUENCE) % 1_000_000
    return f"{seconds}.{micros:06d}"


def _is_allowed_local_approver(*, config: MonicaConfig, user_id: str) -> bool:
    return str(user_id or "").strip() in set(config.slack.approver_user_ids)


def _is_cancelled_run(run: Any) -> bool:
    return str(getattr(run, "failure_reason", "") or "").startswith("cancelled by ")
