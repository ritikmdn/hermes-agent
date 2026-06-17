import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import parse_qsl, urlparse

from dotenv import dotenv_values

from hermes_constants import get_env_path

from .config import MonicaConfig, runtime_root
from .readiness import (
    _invalid_required_env_keys,
    _inline_secret_env_assignments,
    _missing_required_env_keys,
    _noop_only_proof_commands,
    _noop_only_proof_setup_commands,
    _placeholder_proof_commands,
    _placeholder_proof_setup_commands,
)
from .repo_manager import configured_remote_base_ref

_MANIFEST_NAME = "monica-proof-manifest.json"
_VISUAL_PROOF_SUFFIXES = {
    ".gif",
    ".heic",
    ".jpeg",
    ".jpg",
    ".m4v",
    ".mov",
    ".mp4",
    ".png",
    ".webm",
    ".webp",
}
_RASTER_IMAGE_PROOF_SUFFIXES = {
    ".gif",
    ".jpeg",
    ".jpg",
    ".png",
    ".webp",
}
_TEXT_PROOF_SUFFIXES = {
    ".html",
    ".json",
    ".log",
    ".txt",
    ".xml",
}
_TEXT_PROOF_READ_LIMIT_BYTES = 1_000_000
_GIT_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_LINEAR_ISSUE_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
_GENERIC_PROOF_TARGET_TOKENS = {
    "app",
    "default",
    "home",
    "index",
    "landing",
    "main",
    "marketplace",
    "offer",
    "offers",
    "pdp",
    "product",
    "products",
    "root",
    "start",
    "tab",
    "tabs",
}
_AUTH_PROOF_TARGET_TOKENS = {"auth", "login", "onboarding", "signin", "sign-in"}
_DEV_CLIENT_PROOF_TARGET_TOKENS = {"expo-development-client"}
_EXPO_RUNTIME_PROOF_TARGET_SCHEMES = {"exp", "exps"}
_PLACEHOLDER_PROOF_EXPECTED_TEXT_VALUES = {
    "none",
    "n/a",
    "na",
    "text visible on fixed screen",
    "text visible on the fixed screen",
    "unknown",
    "unavailable",
}
_GENERIC_PROOF_EXPECTED_TEXT_VALUES = {
    "home",
    "log in",
    "login",
    "marketplace",
    "onboarding",
    "offer",
    "offers",
    "pdp",
    "product",
    "products",
    "sign in",
    "sign-in",
    "signin",
}
_AUTH_FALLBACK_MARKERS = (
    "not logged in",
    "no session found",
    "initial_session none",
)
_AUTH_FALLBACK_DESTINATION_MARKERS = (
    "-> onboarding",
    "→ onboarding",
    "screen.load /onboarding",
    '"screen": "/onboarding"',
    "screen.load /login",
    '"screen": "/login"',
    "enter your mobile number",
)
_NON_TARGET_SCREEN_ROUTES = {
    "/splash",
    "/splash-screen",
    "/splashscreen",
    "/onboarding",
    "/login",
}
_ROUTE_MATCH_IGNORED_TOKENS = {
    "app",
    "default",
    "home",
    "index",
    "landing",
    "main",
    "root",
    "route",
    "routes",
    "screen",
    "screens",
    "start",
    "tab",
    "tabs",
}
_GENERIC_ROUTE_MATCH_TOKENS = {
    "marketplace",
    "offer",
    "offers",
    "product",
    "products",
}
_PDP_TARGET_TOKENS = {
    "detail",
    "pdp",
}
_SCREEN_ROUTE_RES = (
    re.compile(r"screen\.load\s+([^\s|\"'}]+)", re.IGNORECASE),
    re.compile(r"[\"']screen[\"']\s*:\s*[\"']([^\"']+)[\"']", re.IGNORECASE),
)
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
_LOCAL_PROOF_HOST_SUFFIXES = (".internal", ".lan", ".local")


@dataclass(frozen=True)
class ProofResult:
    passed: bool
    summary: str
    output: str
    artifacts: tuple[str, ...]
    platforms: tuple[str, ...]
    artifact_metadata: tuple[Mapping[str, object], ...] = ()
    proof_target: Mapping[str, str] | None = None
    setup_commands: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()
    required_env_keys: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "passed": self.passed,
            "summary": self.summary,
            "output": self.output,
            "artifacts": list(self.artifacts),
            "platforms": list(self.platforms),
            "setup_commands": list(self.setup_commands),
            "commands": list(self.commands),
        }
        if self.artifact_metadata:
            payload["artifact_metadata"] = [
                dict(item) for item in self.artifact_metadata
            ]
        if self.proof_target:
            payload["proof_target"] = dict(self.proof_target)
        if self.required_env_keys:
            payload["required_env_keys"] = list(self.required_env_keys)
        return payload


RunCommand = Callable[[str, Path, int, dict[str, str]], tuple[int, str, str]]


class ProofRunner:
    def __init__(
        self,
        *,
        config: MonicaConfig,
        run_command: RunCommand | None = None,
    ) -> None:
        self.config = config
        self._run_command = run_command or self._default_run

    def run(
        self,
        *,
        run: Any,
        worktree: str | Path,
        verification: dict[str, Any] | None = None,
        proof_target: Mapping[str, Any] | None = None,
    ) -> ProofResult:
        worktree_path = Path(worktree)
        setup_commands = tuple(command for command in self.config.proof.setup_commands if command.strip())
        command_list = tuple(command for command in self.config.proof.commands if command.strip())
        required_env_keys = _normalized_required_env_keys(
            self.config.proof.required_env_keys
        )
        platforms = _normalized_platforms(self.config.proof.platform_order) or ("ios", "android")
        proof_dir = self._proof_dir(run)
        target = _proof_target(
            config_deep_link=self.config.proof.deep_link,
            proof_target=proof_target,
        )
        proof_dir.mkdir(parents=True, exist_ok=True)
        # Each proof attempt owns this run directory. Clear before early
        # preflight returns so stale visuals cannot look like current proof.
        _clear_proof_dir(proof_dir)

        if not worktree_path.is_dir():
            return ProofResult(
                passed=False,
                summary="Proof blocked: worktree does not exist.",
                output=f"Worktree does not exist: {worktree_path}",
                artifacts=(),
                platforms=platforms,
                proof_target=target,
                setup_commands=setup_commands,
                commands=command_list,
            )
        if not (worktree_path / ".git").exists():
            return ProofResult(
                passed=False,
                summary="Proof blocked: worktree is not a git worktree.",
                output=f"Worktree is not a git worktree: {worktree_path}",
                artifacts=(),
                platforms=platforms,
                proof_target=target,
                setup_commands=setup_commands,
                commands=command_list,
            )
        if not command_list:
            return ProofResult(
                passed=False,
                summary="Proof blocked: proof.commands is empty.",
                output="mobile_bug_agent.proof.commands is empty.",
                artifacts=(),
                platforms=platforms,
                proof_target=target,
                setup_commands=setup_commands,
                commands=command_list,
            )
        if self.config.rollout_mode == "approved_pr" and not setup_commands:
            return ProofResult(
                passed=False,
                summary="Proof blocked: proof.setup_commands is empty.",
                output="mobile_bug_agent.proof.setup_commands is empty.",
                artifacts=(),
                platforms=platforms,
                proof_target=target,
                setup_commands=setup_commands,
                commands=command_list,
            )
        placeholder_setup_commands = _placeholder_proof_setup_commands(setup_commands)
        if self.config.rollout_mode == "approved_pr" and placeholder_setup_commands:
            summary = "Proof blocked: proof.setup_commands contains a placeholder."
            return ProofResult(
                passed=False,
                summary=summary,
                output=(
                    f"{summary} Replace it with the real test-auth/session seed command: "
                    f"{placeholder_setup_commands[0]}"
                ),
                artifacts=(),
                platforms=platforms,
                proof_target=target,
                setup_commands=setup_commands,
                commands=command_list,
            )
        secret_setup_assignments = _inline_secret_env_assignments(setup_commands)
        if self.config.rollout_mode == "approved_pr" and secret_setup_assignments:
            summary = (
                "Proof blocked: proof.setup_commands must not inline secret env assignment(s)."
            )
            return ProofResult(
                passed=False,
                summary=summary,
                output=(
                    f"{summary} Put credentials in the Monica profile .env instead: "
                    f"{', '.join(secret_setup_assignments)}"
                ),
                artifacts=(),
                platforms=platforms,
                proof_target=target,
                setup_commands=setup_commands,
                commands=command_list,
            )
        noop_setup_commands = _noop_only_proof_setup_commands(setup_commands)
        if self.config.rollout_mode == "approved_pr" and noop_setup_commands:
            summary = "Proof blocked: proof.setup_commands contains only no-op commands."
            return ProofResult(
                passed=False,
                summary=summary,
                output=summary,
                artifacts=(),
                platforms=platforms,
                proof_target=target,
                setup_commands=setup_commands,
                commands=command_list,
            )
        placeholder_proof_commands = _placeholder_proof_commands(command_list)
        if self.config.rollout_mode == "approved_pr" and placeholder_proof_commands:
            summary = "Proof blocked: proof.commands contains a placeholder."
            return ProofResult(
                passed=False,
                summary=summary,
                output=(
                    f"{summary} Replace it with the real simulator proof command: "
                    f"{placeholder_proof_commands[0]}"
                ),
                artifacts=(),
                platforms=platforms,
                proof_target=target,
                setup_commands=setup_commands,
                commands=command_list,
            )
        secret_proof_assignments = _inline_secret_env_assignments(command_list)
        if self.config.rollout_mode == "approved_pr" and secret_proof_assignments:
            summary = (
                "Proof blocked: proof.commands must not inline secret env assignment(s)."
            )
            return ProofResult(
                passed=False,
                summary=summary,
                output=(
                    f"{summary} Put credentials in the Monica profile .env instead: "
                    f"{', '.join(secret_proof_assignments)}"
                ),
                artifacts=(),
                platforms=platforms,
                proof_target=target,
                setup_commands=setup_commands,
                commands=command_list,
            )
        noop_proof_commands = _noop_only_proof_commands(command_list)
        if self.config.rollout_mode == "approved_pr" and noop_proof_commands:
            summary = "Proof blocked: proof.commands contains only no-op commands."
            return ProofResult(
                passed=False,
                summary=summary,
                output=summary,
                artifacts=(),
                platforms=platforms,
                proof_target=target,
                setup_commands=setup_commands,
                commands=command_list,
            )
        invalid_required_env_keys = _invalid_required_env_keys(
            self.config.proof.required_env_keys
        )
        if invalid_required_env_keys:
            summary = "Proof blocked: proof.required_env_keys contains invalid key names."
            return ProofResult(
                passed=False,
                summary=summary,
                output=(
                    f"{summary} Use environment key names, not KEY=value or invalid "
                    f"values: {', '.join(invalid_required_env_keys)}"
                ),
                artifacts=(),
                platforms=platforms,
                proof_target=target,
                setup_commands=setup_commands,
                commands=command_list,
            )
        missing_required_platforms = _missing_approved_pr_required_platforms(platforms)
        if self.config.rollout_mode == "approved_pr" and missing_required_platforms:
            missing = ", ".join(missing_required_platforms)
            summary = (
                "Proof blocked: proof.platform_order must include both ios and android "
                f"in approved_pr mode; missing {missing}."
            )
            return ProofResult(
                passed=False,
                summary=summary,
                output=summary,
                artifacts=(),
                platforms=platforms,
                proof_target=target,
                setup_commands=setup_commands,
                commands=command_list,
            )
        base_metadata_block = _approved_pr_base_metadata_block_reason(run, self.config)
        if base_metadata_block:
            return ProofResult(
                passed=False,
                summary=base_metadata_block,
                output=base_metadata_block,
                artifacts=(),
                platforms=platforms,
                proof_target=target,
                setup_commands=setup_commands,
                commands=command_list,
            )
        target_block = _approved_pr_proof_target_block_reason(target, self.config)
        if target_block:
            summary = f"Proof blocked: {target_block}."
            return ProofResult(
                passed=False,
                summary=summary,
                output=summary,
                artifacts=(),
                platforms=platforms,
                proof_target=target,
                setup_commands=setup_commands,
                commands=command_list,
            )
        if self.config.rollout_mode == "approved_pr" and not required_env_keys:
            return ProofResult(
                passed=False,
                summary="Proof blocked: proof.required_env_keys is empty.",
                output="mobile_bug_agent.proof.required_env_keys is empty.",
                artifacts=(),
                platforms=platforms,
                proof_target=target,
                setup_commands=setup_commands,
                commands=command_list,
            )

        env = self._proof_env(
            run=run,
            worktree=worktree_path,
            proof_dir=proof_dir,
            proof_target=target,
            platforms=platforms,
            required_env_keys=required_env_keys,
        )
        missing_required_env_keys = _missing_required_env_keys(env, required_env_keys)
        if self.config.rollout_mode == "approved_pr" and missing_required_env_keys:
            return ProofResult(
                passed=False,
                summary="Proof blocked: required proof environment keys are missing.",
                output=f"Missing proof environment keys: {', '.join(missing_required_env_keys)}",
                artifacts=(),
                platforms=platforms,
                proof_target=target,
                setup_commands=setup_commands,
                commands=command_list,
                required_env_keys=required_env_keys,
            )
        unsafe_setup_commands = _commands_with_required_env_values(
            setup_commands,
            env=env,
            required_env_keys=required_env_keys,
        )
        if self.config.rollout_mode == "approved_pr" and unsafe_setup_commands:
            summary = "Proof blocked: proof.setup_commands must not include required environment values."
            return ProofResult(
                passed=False,
                summary=summary,
                output=_unsafe_required_env_value_command_output(
                    "proof.setup_commands",
                    unsafe_setup_commands,
                ),
                artifacts=(),
                platforms=platforms,
                proof_target=target,
                setup_commands=setup_commands,
                commands=command_list,
                required_env_keys=required_env_keys,
            )
        unsafe_proof_commands = _commands_with_required_env_values(
            command_list,
            env=env,
            required_env_keys=required_env_keys,
        )
        if self.config.rollout_mode == "approved_pr" and unsafe_proof_commands:
            summary = "Proof blocked: proof.commands must not include required environment values."
            return ProofResult(
                passed=False,
                summary=summary,
                output=_unsafe_required_env_value_command_output(
                    "proof.commands",
                    unsafe_proof_commands,
                ),
                artifacts=(),
                platforms=platforms,
                proof_target=target,
                setup_commands=setup_commands,
                commands=command_list,
                required_env_keys=required_env_keys,
            )

        output_parts: list[str] = []
        proof_output_parts: list[str] = []
        base_timeout = self.config.proof.timeout_minutes * 60
        for command in setup_commands:
            code, stdout, stderr = self._run_command(command, worktree_path, base_timeout, env)
            output_parts.append(
                "\n".join(
                    [
                        _redact_proof_output(f"$ {command}", env, required_env_keys),
                        _redact_proof_output(stdout.strip(), env, required_env_keys),
                        _redact_proof_output(stderr.strip(), env, required_env_keys),
                    ]
                ).strip()
            )
            if code != 0:
                return ProofResult(
                    passed=False,
                    summary=f"Proof blocked: proof setup failed: {command}",
                    output="\n\n".join(output_parts),
                    artifacts=self._collect_artifacts(proof_dir),
                    platforms=platforms,
                    proof_target=target,
                    setup_commands=setup_commands,
                    commands=command_list,
                    required_env_keys=required_env_keys,
                )
        setup_artifact_snapshot = _artifact_snapshot(self._collect_artifacts(proof_dir))
        for command in command_list:
            timeout = _proof_command_timeout(command, base_timeout, platforms)
            code, stdout, stderr = self._run_command(command, worktree_path, timeout, env)
            proof_output = "\n".join(
                [
                    _redact_proof_output(f"$ {command}", env, required_env_keys),
                    _redact_proof_output(stdout.strip(), env, required_env_keys),
                    _redact_proof_output(stderr.strip(), env, required_env_keys),
                ]
            ).strip()
            output_parts.append(proof_output)
            proof_output_parts.append(proof_output)
            if code != 0:
                return ProofResult(
                    passed=False,
                    summary=f"Proof blocked: {command}",
                    output="\n\n".join(output_parts),
                    artifacts=self._collect_artifacts(proof_dir),
                    platforms=platforms,
                    proof_target=target,
                    setup_commands=setup_commands,
                    commands=command_list,
                    required_env_keys=required_env_keys,
                )

        collected_artifacts = self._collect_artifacts(proof_dir)
        artifacts = _proof_command_artifacts(collected_artifacts, setup_artifact_snapshot)
        if not artifacts:
            return ProofResult(
                passed=False,
                summary="Proof blocked: proof commands produced no artifacts.",
                output="\n\n".join(output_parts),
                artifacts=collected_artifacts,
                platforms=platforms,
                proof_target=target,
                setup_commands=setup_commands,
                commands=command_list,
                required_env_keys=required_env_keys,
            )
        visual_artifacts = _visual_proof_artifacts(artifacts)
        if not visual_artifacts:
            return ProofResult(
                passed=False,
                summary="Proof blocked: proof commands produced no screenshot or recording artifacts.",
                output="\n\n".join(output_parts),
                artifacts=artifacts,
                platforms=platforms,
                proof_target=target,
                setup_commands=setup_commands,
                commands=command_list,
                required_env_keys=required_env_keys,
            )
        missing_platforms = _missing_platform_artifacts(artifacts=artifacts, platforms=platforms)
        if missing_platforms:
            missing = ", ".join(missing_platforms)
            return ProofResult(
                passed=False,
                summary=f"Proof blocked: missing required platform artifacts: {missing}.",
                output="\n\n".join(output_parts),
                artifacts=artifacts,
                platforms=platforms,
                proof_target=target,
                setup_commands=setup_commands,
                commands=command_list,
                required_env_keys=required_env_keys,
            )
        empty_platforms = _empty_visual_artifact_platforms(
            artifacts=artifacts,
            platforms=platforms,
        )
        if empty_platforms:
            empty = ", ".join(empty_platforms)
            return ProofResult(
                passed=False,
                summary=f"Proof blocked: empty proof artifact files: {empty}.",
                output="\n\n".join(output_parts),
                artifacts=artifacts,
                platforms=platforms,
                proof_target=target,
                setup_commands=setup_commands,
                commands=command_list,
                required_env_keys=required_env_keys,
            )
        invalid_platforms = _invalid_visual_artifact_platforms(
            artifacts=artifacts,
            platforms=platforms,
        ) if self.config.rollout_mode == "approved_pr" else ()
        if invalid_platforms:
            invalid = ", ".join(invalid_platforms)
            return ProofResult(
                passed=False,
                summary=f"Proof blocked: invalid proof artifact files: {invalid}.",
                output="\n\n".join(output_parts),
                artifacts=artifacts,
                platforms=platforms,
                proof_target=target,
                setup_commands=setup_commands,
                commands=command_list,
                required_env_keys=required_env_keys,
            )
        non_target_platforms = _non_target_screen_platforms(
            artifacts=artifacts,
            platforms=platforms,
        )
        if non_target_platforms:
            platforms_text = ", ".join(non_target_platforms)
            return ProofResult(
                passed=False,
                summary=f"Proof blocked: non-target app screen observed for: {platforms_text}.",
                output="\n\n".join(output_parts),
                artifacts=artifacts,
                platforms=platforms,
                proof_target=target,
                setup_commands=setup_commands,
                commands=command_list,
                required_env_keys=required_env_keys,
            )
        auth_fallback_platforms = _auth_fallback_platforms(artifacts=artifacts, platforms=platforms)
        if auth_fallback_platforms:
            platforms_text = ", ".join(auth_fallback_platforms)
            return ProofResult(
                passed=False,
                summary=f"Proof blocked: auth/onboarding proof fallback observed for: {platforms_text}.",
                output="\n\n".join(output_parts),
                artifacts=artifacts,
                platforms=platforms,
                proof_target=target,
                setup_commands=setup_commands,
                commands=command_list,
                required_env_keys=required_env_keys,
            )
        missing_route_platforms = _missing_target_route_platforms(
            artifacts=artifacts,
            platforms=platforms,
        ) if self.config.rollout_mode == "approved_pr" else ()
        if missing_route_platforms:
            platforms_text = ", ".join(missing_route_platforms)
            return ProofResult(
                passed=False,
                summary=f"Proof blocked: target screen route was not observed for: {platforms_text}.",
                output="\n\n".join(output_parts),
                artifacts=artifacts,
                platforms=platforms,
                proof_target=target,
                setup_commands=setup_commands,
                commands=command_list,
                required_env_keys=required_env_keys,
            )
        mismatched_route_platforms = _mismatched_target_route_platforms(
            artifacts=artifacts,
            platforms=platforms,
            proof_target=target,
        ) if self.config.rollout_mode == "approved_pr" else ()
        if mismatched_route_platforms:
            platforms_text = ", ".join(mismatched_route_platforms)
            return ProofResult(
                passed=False,
                summary=f"Proof blocked: target screen route does not match proof target for: {platforms_text}.",
                output="\n\n".join(output_parts),
                artifacts=artifacts,
                platforms=platforms,
                proof_target=target,
                setup_commands=setup_commands,
                commands=command_list,
                required_env_keys=required_env_keys,
            )
        expected_text = target.get("expected_text", "")
        missing_text_platforms = _missing_expected_text_platforms(
            expected_text=expected_text,
            output="\n\n".join(proof_output_parts),
            artifacts=artifacts,
            platforms=platforms,
        )
        if missing_text_platforms:
            summary = (
                f"Proof blocked: expected target text was not observed: {expected_text}"
                if len(missing_text_platforms) == 1 and len(_normalized_platforms(platforms)) == 1
                else "Proof blocked: expected target text was not observed for: "
                + ", ".join(missing_text_platforms)
            )
            return ProofResult(
                passed=False,
                summary=summary,
                output="\n\n".join(output_parts),
                artifacts=artifacts,
                platforms=platforms,
                proof_target=target,
                setup_commands=setup_commands,
                commands=command_list,
                required_env_keys=required_env_keys,
            )

        artifact_metadata = _proof_artifact_metadata(
            artifacts=artifacts,
            platforms=platforms,
        )
        manifest_path = self._write_manifest(
            run=run,
            worktree=worktree_path,
            proof_dir=proof_dir,
            artifacts=artifacts,
            artifact_metadata=artifact_metadata,
            platforms=platforms,
            proof_target=target,
            setup_commands=setup_commands,
            commands=command_list,
            required_env_keys=required_env_keys,
        )
        artifacts = (str(manifest_path), *artifacts)
        return ProofResult(
            passed=True,
            summary="Proof captured.",
            output="\n\n".join(output_parts),
            artifacts=artifacts,
            platforms=platforms,
            artifact_metadata=artifact_metadata,
            proof_target=target,
            setup_commands=setup_commands,
            commands=command_list,
            required_env_keys=required_env_keys,
        )

    def _proof_dir(self, run: Any) -> Path:
        raw_artifact_dir = self.config.proof.artifact_dir.strip() or "proof"
        artifact_dir = Path(raw_artifact_dir).expanduser()
        if artifact_dir.is_absolute():
            root = artifact_dir
        else:
            root = runtime_root(self.config) / artifact_dir
        return root / str(getattr(run, "id", "") or "unknown-run")

    def _proof_env(
        self,
        *,
        run: Any,
        worktree: Path,
        proof_dir: Path,
        proof_target: Mapping[str, str],
        platforms: tuple[str, ...],
        required_env_keys: tuple[str, ...],
    ) -> dict[str, str]:
        env = dict(os.environ)
        env.update(_profile_env_values())
        hermes_agent_root = Path(__file__).resolve().parents[2]
        pythonpath_parts = [str(hermes_agent_root)]
        if env.get("PYTHONPATH"):
            pythonpath_parts.append(str(env["PYTHONPATH"]))
        env.update(
            {
                "MONICA_HERMES_AGENT_ROOT": str(hermes_agent_root),
                "MONICA_PROOF_DIR": str(proof_dir),
                "MONICA_WORKTREE": str(worktree),
                "MONICA_RUN_ID": str(getattr(run, "id", "") or ""),
                "MONICA_LINEAR_IDENTIFIER": _linear_identifier(run),
                "MONICA_LINEAR_URL": str(getattr(run, "linear_url", "") or ""),
                "MONICA_BRANCH_NAME": str(getattr(run, "branch_name", "") or ""),
                "MONICA_BASE_REF": _base_ref(run),
                "MONICA_BASE_COMMIT": str(getattr(run, "base_commit", "") or ""),
                "MONICA_PROOF_PLATFORM_ORDER": ",".join(platforms),
                "MONICA_REQUIRED_ENV_KEYS": ",".join(required_env_keys),
                "MONICA_DEV_CLIENT_SCHEME": self.config.proof.dev_client_scheme,
                "MONICA_IOS_SIMULATOR_UDID": self.config.proof.ios_simulator_udid,
                "MONICA_IOS_BUNDLE_ID": self.config.proof.ios_bundle_id,
                "MONICA_ANDROID_SERIAL": self.config.proof.android_serial,
                "MONICA_ANDROID_AVD": self.config.proof.android_avd,
                "MONICA_ANDROID_PACKAGE": self.config.proof.android_package,
                "MONICA_DEEP_LINK": proof_target.get("deep_link", ""),
                "MONICA_PROOF_EXPECTED_TEXT": proof_target.get("expected_text", ""),
                "MONICA_PROOF_SCREEN": proof_target.get("screen", ""),
                "PYTHONPATH": os.pathsep.join(pythonpath_parts),
            }
        )
        return env

    @staticmethod
    def _collect_artifacts(proof_dir: Path) -> tuple[str, ...]:
        if not proof_dir.is_dir():
            return ()
        return tuple(
            str(path)
            for path in sorted(proof_dir.rglob("*"))
            if path.is_file() and path.name != _MANIFEST_NAME
        )

    def _write_manifest(
        self,
        *,
        run: Any,
        worktree: Path,
        proof_dir: Path,
        artifacts: tuple[str, ...],
        artifact_metadata: tuple[Mapping[str, object], ...],
        platforms: tuple[str, ...],
        proof_target: Mapping[str, str],
        setup_commands: tuple[str, ...],
        commands: tuple[str, ...],
        required_env_keys: tuple[str, ...],
    ) -> Path:
        manifest_path = proof_dir / _MANIFEST_NAME
        base_ref = _base_ref(run)
        payload = {
            "run_id": str(getattr(run, "id", "") or ""),
            "linear_identifier": _linear_identifier(run),
            "linear_url": str(getattr(run, "linear_url", "") or ""),
            "branch_name": str(getattr(run, "branch_name", "") or ""),
            "base_branch": base_ref,
            "base_ref": base_ref,
            "base_commit": str(getattr(run, "base_commit", "") or ""),
            "worktree": str(worktree),
            "platforms": list(platforms),
            "proof_artifacts": list(artifacts),
            "proof_artifact_metadata": [
                dict(item) for item in artifact_metadata
            ],
            "setup_commands": list(setup_commands),
            "commands": list(commands),
            "required_env_keys": list(required_env_keys),
        }
        if proof_target:
            payload["proof_target"] = dict(proof_target)
        manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return manifest_path

    @staticmethod
    def _default_run(
        command: str,
        cwd: Path,
        timeout: int,
        env: dict[str, str],
    ) -> tuple[int, str, str]:
        try:
            proc = subprocess.run(
                command,
                cwd=str(cwd),
                shell=True,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return 124, "", f"command timed out after {timeout}s"
        return proc.returncode, proc.stdout, proc.stderr


def _missing_platform_artifacts(*, artifacts: tuple[str, ...], platforms: tuple[str, ...]) -> tuple[str, ...]:
    missing: list[str] = []
    visual_artifacts = _visual_proof_artifacts(artifacts)
    used_artifacts: set[str] = set()
    for platform in platforms:
        normalized = _normalize_platform_name(platform)
        if not normalized:
            continue
        matched = ""
        for artifact in visual_artifacts:
            artifact_key = str(Path(artifact))
            if artifact_key in used_artifacts:
                continue
            if _artifact_matches_platform(artifact, normalized):
                matched = artifact_key
                break
        if matched:
            used_artifacts.add(matched)
        else:
            missing.append(normalized)
    return tuple(missing)


def _empty_visual_artifact_platforms(
    *,
    artifacts: tuple[str, ...],
    platforms: tuple[str, ...],
) -> tuple[str, ...]:
    empty: list[str] = []
    visual_artifacts = _visual_proof_artifacts(artifacts)
    for platform in _normalized_platforms(platforms):
        platform_artifacts = [
            Path(artifact)
            for artifact in visual_artifacts
            if _artifact_matches_platform(artifact, platform)
        ]
        if platform_artifacts and not any(_path_has_bytes(path) for path in platform_artifacts):
            empty.append(platform)
    return tuple(empty)


def _path_has_bytes(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _invalid_visual_artifact_platforms(
    *,
    artifacts: tuple[str, ...],
    platforms: tuple[str, ...],
) -> tuple[str, ...]:
    invalid: list[str] = []
    visual_artifacts = _visual_proof_artifacts(artifacts)
    for platform in _normalized_platforms(platforms):
        platform_artifacts = [
            Path(artifact)
            for artifact in visual_artifacts
            if _artifact_matches_platform(artifact, platform)
        ]
        raster_artifacts = [
            path
            for path in platform_artifacts
            if path.suffix.lower() in _RASTER_IMAGE_PROOF_SUFFIXES
        ]
        if raster_artifacts and not any(_raster_image_is_readable(path) for path in raster_artifacts):
            invalid.append(platform)
    return tuple(invalid)


def _raster_image_is_readable(path: Path) -> bool:
    if not _path_has_bytes(path):
        return False
    try:
        from PIL import Image

        with Image.open(path) as image:
            image.verify()
    except Exception:
        return False
    return True


def _missing_approved_pr_required_platforms(platforms: tuple[str, ...]) -> tuple[str, ...]:
    present = set(_normalized_platforms(platforms))
    return tuple(platform for platform in ("ios", "android") if platform not in present)


def _artifact_snapshot(artifacts: tuple[str, ...]) -> dict[str, tuple[int, str]]:
    snapshot: dict[str, tuple[int, str]] = {}
    for artifact in artifacts:
        path = Path(artifact)
        fingerprint = _artifact_fingerprint(path)
        if fingerprint is None:
            continue
        snapshot[str(path)] = fingerprint
    return snapshot


def _proof_command_artifacts(
    artifacts: tuple[str, ...],
    setup_snapshot: dict[str, tuple[int, str]],
) -> tuple[str, ...]:
    proof_artifacts: list[str] = []
    for artifact in artifacts:
        path = Path(artifact)
        before = setup_snapshot.get(str(path))
        if before is None:
            proof_artifacts.append(artifact)
            continue
        current = _artifact_fingerprint(path)
        if current is None:
            continue
        if current != before:
            proof_artifacts.append(artifact)
    return tuple(proof_artifacts)


def _proof_artifact_metadata(
    *,
    artifacts: tuple[str, ...],
    platforms: tuple[str, ...],
) -> tuple[Mapping[str, object], ...]:
    metadata: list[dict[str, object]] = []
    normalized_platforms = _normalized_platforms(platforms)
    for artifact in artifacts:
        path = Path(artifact)
        entry: dict[str, object] = {
            "path": str(path),
            "platform": _artifact_platform(path, normalized_platforms),
        }
        fingerprint = _artifact_fingerprint(path)
        if fingerprint is not None:
            size, digest = fingerprint
            entry["bytes"] = size
            entry["sha256"] = digest
        metadata.append(entry)
    return tuple(metadata)


def _artifact_platform(path: Path, platforms: tuple[str, ...]) -> str:
    for platform in platforms:
        if _artifact_matches_platform(str(path), platform):
            return platform
    return ""


def _artifact_fingerprint(path: Path) -> tuple[int, str] | None:
    try:
        size = path.stat().st_size
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return size, digest.hexdigest()


def _clear_proof_dir(proof_dir: Path) -> None:
    for child in proof_dir.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
            continue
        child.unlink(missing_ok=True)


def _proof_command_timeout(command: str, base_timeout: int, platforms: tuple[str, ...]) -> int:
    if "plugins.mobile_bug_agent.simulator_proof" not in command:
        return base_timeout

    platform_count = max(
        1,
        len(tuple(platform for platform in (_normalize_platform_name(value) for value in platforms) if platform)),
    )
    per_platform_timeout = _simulator_proof_timeout_seconds(command) or base_timeout
    return max(base_timeout, per_platform_timeout * platform_count + 600)


def _simulator_proof_timeout_seconds(command: str) -> int:
    try:
        parts = shlex.split(command)
    except ValueError:
        return 0

    for index, part in enumerate(parts):
        if part == "--timeout-seconds" and index + 1 < len(parts):
            return _positive_int(parts[index + 1])
        if part.startswith("--timeout-seconds="):
            return _positive_int(part.split("=", 1)[1])
    return 0


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _artifact_matches_platform(artifact: str, platform: str) -> bool:
    path = Path(artifact)
    haystack = " ".join((path.name, path.stem, *path.parts[-3:])).lower()
    if platform == "ios":
        return any(token in haystack for token in ("ios", "iphone", "ipad"))
    return platform in haystack


def _visual_proof_artifacts(artifacts: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(artifact for artifact in artifacts if _is_visual_proof_artifact(artifact))


def _is_visual_proof_artifact(artifact: str) -> bool:
    return Path(artifact).suffix.lower() in _VISUAL_PROOF_SUFFIXES


def _auth_fallback_platforms(
    *,
    artifacts: tuple[str, ...],
    platforms: tuple[str, ...],
) -> tuple[str, ...]:
    normalized_platforms = _normalized_platforms(platforms)
    observed: list[str] = []
    observed_artifacts: set[str] = set()
    for platform in normalized_platforms:
        for artifact in artifacts:
            if not _is_text_proof_artifact(artifact):
                continue
            if not _artifact_matches_platform(artifact, platform):
                continue
            if _text_shows_auth_fallback(_read_text_artifact(artifact)):
                observed.append(platform)
                observed_artifacts.add(str(Path(artifact)))
                break
    for artifact in artifacts:
        artifact_key = str(Path(artifact))
        if artifact_key in observed_artifacts:
            continue
        if not _is_text_proof_artifact(artifact):
            continue
        if any(_artifact_matches_platform(artifact, platform) for platform in normalized_platforms):
            continue
        if _text_shows_auth_fallback(_read_text_artifact(artifact)):
            observed.append("unattributed")
            break
    return tuple(dict.fromkeys(observed))


def _text_shows_auth_fallback(value: str) -> bool:
    normalized = _normalize_text_for_match(value)
    if not normalized:
        return False
    return any(marker in normalized for marker in _AUTH_FALLBACK_MARKERS) and any(
        marker in normalized for marker in _AUTH_FALLBACK_DESTINATION_MARKERS
    )


def _non_target_screen_platforms(
    *,
    artifacts: tuple[str, ...],
    platforms: tuple[str, ...],
) -> tuple[str, ...]:
    observed: list[str] = []
    for platform in _normalized_platforms(platforms):
        final_route = ""
        for artifact in artifacts:
            if not _is_text_proof_artifact(artifact):
                continue
            if not _artifact_matches_platform(artifact, platform):
                continue
            route = _last_screen_load_route(_read_text_artifact(artifact))
            if route:
                final_route = route
        if final_route.casefold() in _NON_TARGET_SCREEN_ROUTES:
            observed.append(platform)
    return tuple(observed)


def _missing_target_route_platforms(
    *,
    artifacts: tuple[str, ...],
    platforms: tuple[str, ...],
) -> tuple[str, ...]:
    missing: list[str] = []
    for platform in _normalized_platforms(platforms):
        final_route = ""
        for artifact in artifacts:
            if not _is_text_proof_artifact(artifact):
                continue
            if not _artifact_matches_platform(artifact, platform):
                continue
            route = _last_screen_load_route(_read_text_artifact(artifact))
            if route:
                final_route = route
        if not final_route:
            missing.append(platform)
    return tuple(missing)


def _mismatched_target_route_platforms(
    *,
    artifacts: tuple[str, ...],
    platforms: tuple[str, ...],
    proof_target: Mapping[str, Any],
) -> tuple[str, ...]:
    target_tokens = _target_route_tokens(proof_target)
    if not target_tokens:
        return ()
    mismatched: list[str] = []
    for platform in _normalized_platforms(platforms):
        final_route = ""
        for artifact in artifacts:
            if not _is_text_proof_artifact(artifact):
                continue
            if not _artifact_matches_platform(artifact, platform):
                continue
            route = _last_screen_load_route(_read_text_artifact(artifact))
            if route:
                final_route = route
        if final_route and not _route_matches_target(final_route, target_tokens):
            mismatched.append(platform)
    return tuple(mismatched)


def _target_route_tokens(proof_target: Mapping[str, Any]) -> frozenset[str]:
    screen_tokens = _route_match_tokens(str(proof_target.get("screen") or ""))
    if screen_tokens:
        return _expand_target_route_tokens(screen_tokens)

    deep_link = str(proof_target.get("deep_link") or "").strip()
    if not deep_link:
        return frozenset()
    parsed = urlparse(deep_link)
    route_parts: list[str] = []
    if parsed.scheme.casefold() in {"http", "https"}:
        route_parts.append(parsed.path)
    else:
        route_parts.extend([parsed.netloc, parsed.path])
    route_parts.append(parsed.fragment)
    route_parts.extend(item_value for _, item_value in parse_qsl(parsed.query, keep_blank_values=False))
    return _expand_target_route_tokens(_route_match_tokens(" ".join(route_parts)))


def _route_matches_target(route: str, target_tokens: frozenset[str]) -> bool:
    route_tokens = _route_match_tokens(route)
    if not route_tokens or not target_tokens:
        return False
    matched_tokens = route_tokens.intersection(target_tokens)
    return any(token not in _GENERIC_ROUTE_MATCH_TOKENS for token in matched_tokens)


def _expand_target_route_tokens(tokens: frozenset[str]) -> frozenset[str]:
    expanded = set(tokens)
    if tokens.intersection(_GENERIC_ROUTE_MATCH_TOKENS):
        expanded.update(_PDP_TARGET_TOKENS)
    return frozenset(expanded)


def _route_match_tokens(value: str) -> frozenset[str]:
    camel_spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(value or ""))
    return frozenset(
        token
        for token in (
            raw.casefold()
            for raw in re.split(r"[^A-Za-z0-9]+", camel_spaced)
        )
        if token and token not in _ROUTE_MATCH_IGNORED_TOKENS
    )


def _last_screen_load_route(value: str) -> str:
    matches: list[tuple[int, str]] = []
    for pattern in _SCREEN_ROUTE_RES:
        matches.extend(
            (match.start(), match.group(1))
            for match in pattern.finditer(str(value or ""))
        )
    route = ""
    for _position, raw_candidate in sorted(matches, key=lambda item: item[0]):
        candidate = raw_candidate.strip().rstrip(".,;")
        if candidate:
            route = candidate
    return route


def _missing_expected_text_platforms(
    *,
    expected_text: str,
    output: str,
    artifacts: tuple[str, ...],
    platforms: tuple[str, ...],
) -> tuple[str, ...]:
    needle = _normalize_text_for_match(expected_text)
    if not needle:
        return ()
    normalized_platforms = _normalized_platforms(platforms)
    if not normalized_platforms:
        return ()
    missing: list[str] = []
    used_artifacts: set[str] = set()
    for platform in normalized_platforms:
        matched = ""
        for artifact in artifacts:
            artifact_key = str(Path(artifact))
            if artifact_key in used_artifacts:
                continue
            if not _is_text_proof_artifact(artifact):
                continue
            if not _artifact_matches_platform(artifact, platform):
                continue
            if needle in _normalize_text_for_match(_read_text_artifact(artifact)):
                matched = artifact_key
                break
        if not matched:
            missing.append(platform)
        else:
            used_artifacts.add(matched)
    return tuple(missing)


def _is_text_proof_artifact(artifact: str) -> bool:
    return Path(artifact).suffix.lower() in _TEXT_PROOF_SUFFIXES


def _read_text_artifact(artifact: str) -> str:
    try:
        with Path(artifact).open("rb") as handle:
            payload = handle.read(_TEXT_PROOF_READ_LIMIT_BYTES)
    except OSError:
        return ""
    return payload.decode("utf-8", errors="replace")


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


def _normalized_required_env_keys(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = str(value or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        normalized.append(key)
    return tuple(normalized)


def _redact_proof_output(
    value: str,
    env: Mapping[str, str],
    required_env_keys: tuple[str, ...],
) -> str:
    text = str(value or "")
    required = {str(key).strip() for key in required_env_keys if str(key).strip()}
    redactions: dict[str, str] = {}
    for key, raw_value in env.items():
        name = str(key or "").strip()
        if not name:
            continue
        normalized_name = name.upper()
        if name not in required and not any(
            marker in normalized_name for marker in _SECRET_ENV_NAME_MARKERS
        ):
            continue
        secret = str(raw_value or "")
        stripped_secret = secret.strip()
        if name in required:
            if not stripped_secret:
                continue
            redactions.setdefault(stripped_secret, name)
            if secret and secret != stripped_secret:
                redactions.setdefault(secret, name)
            continue
        if len(stripped_secret) < 4:
            continue
        redactions.setdefault(secret, name)
    for secret, name in sorted(redactions.items(), key=lambda item: len(item[0]), reverse=True):
        text = text.replace(secret, f"[redacted:{name}]")
    return text


def _commands_with_required_env_values(
    commands: tuple[str, ...],
    *,
    env: Mapping[str, str],
    required_env_keys: tuple[str, ...],
) -> tuple[str, ...]:
    unsafe: list[str] = []
    for command in commands:
        if _command_contains_required_env_value(
            command,
            env=env,
            required_env_keys=required_env_keys,
        ):
            unsafe.append(_redact_proof_output(command, env, required_env_keys))
    return tuple(unsafe)


def _command_contains_required_env_value(
    command: str,
    *,
    env: Mapping[str, str],
    required_env_keys: tuple[str, ...],
) -> bool:
    raw_command = str(command or "")
    if not raw_command:
        return False
    try:
        tokens = shlex.split(raw_command)
    except ValueError:
        tokens = raw_command.split()
    for key in required_env_keys:
        secret = str(env.get(str(key).strip()) or "").strip()
        if not secret:
            continue
        for token in tokens:
            if token == secret:
                return True
            if token.endswith(f"={secret}") or token.endswith(f":{secret}"):
                return True
            if len(secret) >= 4 and secret in token:
                return True
    return False


def _unsafe_required_env_value_command_output(label: str, commands: tuple[str, ...]) -> str:
    lines = [
        f"mobile_bug_agent.{label} must reference required credentials from the environment, "
        "not include their literal values."
    ]
    lines.extend(f"$ {command}" for command in commands)
    return "\n".join(lines)


def _linear_identifier(run: Any) -> str:
    value = str(getattr(run, "linear_identifier", "") or "").strip()
    if value:
        return value
    match = _LINEAR_ISSUE_KEY_RE.search(str(getattr(run, "linear_url", "") or "").upper())
    return match.group(0) if match else ""


def _base_ref(run: Any) -> str:
    return str(
        getattr(run, "base_branch", "")
        or getattr(run, "base_ref", "")
        or ""
    ).strip()


def _approved_pr_base_metadata_block_reason(run: Any, config: MonicaConfig) -> str:
    if config.rollout_mode != "approved_pr":
        return ""
    base_ref = _base_ref(run)
    base_commit = str(getattr(run, "base_commit", "") or "").strip()
    if not base_ref or not _looks_like_git_commit(base_commit):
        return "Proof blocked: run base commit metadata is invalid."
    expected_base_ref = configured_remote_base_ref(config.repo.default_branch)
    if base_ref != expected_base_ref:
        return "Proof blocked: run base branch does not match configured default branch."
    return ""


def _looks_like_git_commit(value: str) -> bool:
    return bool(_GIT_COMMIT_RE.fullmatch(str(value or "").strip()))


def _approved_pr_proof_target_block_reason(target: Mapping[str, str], config: MonicaConfig) -> str:
    if config.rollout_mode != "approved_pr":
        return ""
    deep_link = str(target.get("deep_link") or "").strip()
    if not deep_link:
        return "proof target deep link is required"
    generic_target_reason = _generic_proof_target_block_reason(deep_link)
    if generic_target_reason:
        return generic_target_reason
    expected_text_block = _proof_expected_text_block_reason(target.get("expected_text"))
    if expected_text_block:
        return expected_text_block
    return ""


def _normalized_proof_expected_text(value: Any) -> str:
    text = " ".join(str(value or "").split())
    if _proof_expected_text_placeholder_key(text) in _PLACEHOLDER_PROOF_EXPECTED_TEXT_VALUES:
        return ""
    return text


def _proof_expected_text_placeholder_key(value: str) -> str:
    text = " ".join(str(value or "").split())
    wrappers = (("`", "`"), ("<", ">"), ('"', '"'), ("'", "'"))
    while text:
        original = text
        for opener, closer in wrappers:
            if len(text) >= 2 and text.startswith(opener) and text.endswith(closer):
                text = text[1:-1].strip()
        text = " ".join(text.split())
        if text == original:
            break
    return text.casefold()


def _proof_expected_text_block_reason(value: Any) -> str:
    text = _normalized_proof_expected_text(value)
    if not text:
        return "proof target expected text is required"
    if text.casefold() in _GENERIC_PROOF_EXPECTED_TEXT_VALUES:
        return "proof target expected text is too generic"
    return ""


def _generic_proof_target_block_reason(value: Any) -> str:
    deep_link = str(value or "").strip()
    parsed = urlparse(deep_link)
    if parsed.scheme.casefold() in _EXPO_RUNTIME_PROOF_TARGET_SCHEMES:
        return "Expo runtime proof target is not enough"
    if parsed.scheme.casefold() in {"http", "https"} and _is_local_host(parsed.hostname):
        return "local proof target is not enough"
    route_parts: list[str] = []
    if parsed.scheme.casefold() in {"http", "https"}:
        route_parts.append(parsed.path)
    else:
        route_parts.extend([parsed.netloc, parsed.path])
    route_parts.append(parsed.fragment)
    route_parts.extend(item_value for _, item_value in parse_qsl(parsed.query, keep_blank_values=False))
    tokens = [
        token
        for token in re.split(r"[^a-z0-9-]+", " ".join(route_parts).casefold())
        if token
    ]
    if any(token in _DEV_CLIENT_PROOF_TARGET_TOKENS for token in tokens):
        return "Expo Dev Client proof target is not enough"
    if not tokens or all(token in _GENERIC_PROOF_TARGET_TOKENS for token in tokens):
        return "generic proof target is not enough"
    if any(token in _AUTH_PROOF_TARGET_TOKENS for token in tokens):
        return "auth/onboarding proof target is not enough"
    return ""


def _is_local_host(hostname: str | None) -> bool:
    host = str(hostname or "").strip().lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ip_address(host)
    except ValueError:
        return "." not in host or host.endswith(_LOCAL_PROOF_HOST_SUFFIXES)
    return (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_unspecified
    )


def _normalize_text_for_match(value: str) -> str:
    return " ".join(str(value or "").casefold().split())


def _normalized_platforms(platforms: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            platform
            for platform in (_normalize_platform_name(value) for value in platforms)
            if platform
        )
    )


def _proof_target(
    *,
    config_deep_link: str,
    proof_target: Mapping[str, Any] | None,
) -> dict[str, str]:
    target: dict[str, str] = {}
    if isinstance(proof_target, Mapping):
        for key in ("deep_link", "expected_text", "screen", "notes"):
            value = str(proof_target.get(key) or "").strip()
            if value:
                target[key] = value
    fallback = str(config_deep_link or "").strip()
    if fallback and not target.get("deep_link"):
        target["deep_link"] = fallback
    if not _looks_like_deep_link(target.get("deep_link", "")):
        target.pop("deep_link", None)
    return target


def _looks_like_deep_link(value: str) -> bool:
    text = str(value or "").strip()
    if not text or any(char.isspace() for char in text):
        return False
    return "://" in text or text.startswith(("exp+", "http://", "https://"))


def _normalize_platform_name(platform: str) -> str:
    value = str(platform or "").strip().lower()
    if value in {"iphone", "ipad", "ios-simulator"}:
        return "ios"
    if value in {"android-emulator"}:
        return "android"
    return value
