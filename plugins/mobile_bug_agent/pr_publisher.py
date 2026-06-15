from __future__ import annotations

import json
import os
import re
import subprocess
from ipaddress import ip_address
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qsl, urlparse

from .readiness import (
    _inline_secret_env_assignments,
    _invalid_required_env_keys,
    _is_noop_shell_command,
    _placeholder_proof_commands,
    _placeholder_proof_setup_commands,
    _profile_env_values,
    _required_env_value_keys_in_commands,
)
from .proof import _route_matches_target, _target_route_tokens
from .repo_manager import configured_remote_base_ref, is_safe_git_branch_name


class DraftPrPublisherError(RuntimeError):
    pass


RunCommand = Callable[[list[str], Path | None], str]
_SLACK_CHANNEL_ID_RE = re.compile(r"^[CDG][A-Z0-9_]{2,}$")
_SLACK_THREAD_TS_RE = re.compile(r"^\d{10}\.\d{6}$")
_GIT_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_REQUIRED_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LINEAR_ISSUE_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
_PROOF_MANIFEST_NAME = "monica-proof-manifest.json"
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
_RASTER_IMAGE_PROOF_SUFFIXES = {".gif", ".jpeg", ".jpg", ".png", ".webp"}
_TEXT_PROOF_SUFFIXES = {".html", ".json", ".log", ".txt", ".xml"}
_TEXT_PROOF_READ_LIMIT_BYTES = 1_000_000
_LOCAL_SHAREABLE_HOST_SUFFIXES = (".internal", ".lan", ".local")
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
_SCREEN_ROUTE_RES = (
    re.compile(r"screen\.load\s+([^\s|\"'}]+)", re.IGNORECASE),
    re.compile(r"[\"']screen[\"']\s*:\s*[\"']([^\"']+)[\"']", re.IGNORECASE),
)


class DraftPrPublisher:
    def __init__(
        self,
        *,
        run_command: RunCommand | None = None,
        timeout_seconds: int = 600,
    ) -> None:
        self._run_command = run_command or self._default_run
        self.timeout_seconds = timeout_seconds

    def publish(
        self,
        *,
        worktree: str | Path,
        branch_name: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> str:
        safe_branch_name = str(branch_name or "").strip()
        if not safe_branch_name:
            raise DraftPrPublisherError("branch_name is required.")
        if not is_safe_git_branch_name(safe_branch_name):
            raise DraftPrPublisherError("branch_name must be a safe git branch name.")
        safe_base_branch = str(base_branch or "").strip()
        if not is_safe_git_branch_name(safe_base_branch):
            raise DraftPrPublisherError("base_branch must be a safe git branch name.")
        title_text = str(title or "").strip()
        if not title_text:
            raise DraftPrPublisherError("title is required.")
        body_text = str(body or "").strip()
        if not body_text:
            raise DraftPrPublisherError("body is required.")
        worktree_path = Path(worktree)
        _validate_pr_body(
            body_text,
            expected_branch_name=safe_branch_name,
            expected_worktree=str(worktree_path),
        )
        if not worktree_path.is_dir():
            raise DraftPrPublisherError(f"worktree does not exist: {worktree_path}")
        if not (worktree_path / ".git").exists():
            raise DraftPrPublisherError(f"worktree is not a git worktree: {worktree_path}")
        current_branch = self._run_command(["git", "branch", "--show-current"], worktree_path).strip()
        if current_branch != safe_branch_name:
            raise DraftPrPublisherError(
                f"worktree branch mismatch: expected {safe_branch_name}, got {current_branch or 'detached HEAD'}"
            )
        status = self._run_command(["git", "status", "--porcelain"], worktree_path)
        if status.strip():
            self._run_command(["git", "add", "-A"], worktree_path)
            self._run_command(
                [
                    "git",
                    "-c",
                    "user.name=Monica",
                    "-c",
                    "user.email=monica@hermes.local",
                    "commit",
                    "-m",
                    title_text,
                ],
                worktree_path,
            )

        diff_base_ref = configured_remote_base_ref(safe_base_branch)
        diff = self._run_command(
            ["git", "diff", "--name-only", f"{diff_base_ref}...HEAD"],
            worktree_path,
        )
        if not diff.strip():
            raise DraftPrPublisherError("No committed changes to publish.")

        self._run_command(
            ["git", "push", "origin", f"HEAD:{safe_branch_name}"],
            worktree_path,
        )
        create_command = [
            "gh",
            "pr",
            "create",
            "--draft",
            "--base",
            safe_base_branch,
            "--head",
            safe_branch_name,
            "--title",
            title_text,
            "--body",
            body_text,
        ]
        try:
            output = self._run_command(create_command, worktree_path)
        except DraftPrPublisherError as exc:
            if url := _extract_pr_url(str(exc)):
                self._verify_recovered_pr_is_draft(url, worktree_path)
                self._refresh_recovered_pr(
                    url,
                    worktree_path,
                    base_branch=safe_base_branch,
                    title=title_text,
                    body=body_text,
                )
                return url
            raise
        if not (match := _extract_pr_url(output)):
            raise DraftPrPublisherError("gh did not return a draft PR URL.")
        self._ensure_pr_is_draft(match, worktree_path, label="created PR")
        return match

    def _verify_recovered_pr_is_draft(self, url: str, worktree_path: Path) -> None:
        self._ensure_pr_is_draft(url, worktree_path, label="existing PR")

    def _refresh_recovered_pr(
        self,
        url: str,
        worktree_path: Path,
        *,
        base_branch: str,
        title: str,
        body: str,
    ) -> None:
        self._run_command(
            [
                "gh",
                "pr",
                "edit",
                url,
                "--base",
                base_branch,
                "--title",
                title,
                "--body",
                body,
            ],
            worktree_path,
        )

    def _verify_pr_is_draft(self, url: str, worktree_path: Path, *, label: str) -> None:
        if self._pr_is_draft(url, worktree_path):
            return
        raise DraftPrPublisherError(f"{label} is not draft: {url}")

    def _ensure_pr_is_draft(self, url: str, worktree_path: Path, *, label: str) -> None:
        if self._pr_is_draft(url, worktree_path):
            return
        self._run_command(
            [
                "gh",
                "pr",
                "ready",
                url,
                "--undo",
            ],
            worktree_path,
        )
        if self._pr_is_draft(url, worktree_path):
            return
        raise DraftPrPublisherError(f"{label} is not draft: {url}")

    def _pr_is_draft(self, url: str, worktree_path: Path) -> bool:
        output = self._run_command(
            [
                "gh",
                "pr",
                "view",
                url,
                "--json",
                "isDraft",
                "--jq",
                ".isDraft",
            ],
            worktree_path,
        )
        return str(output or "").strip().casefold() == "true"

    def _default_run(self, cmd: list[str], cwd: Path | None = None) -> str:
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(cwd) if cwd else None,
                text=True,
                capture_output=True,
                check=False,
                timeout=self.timeout_seconds,
            )
        except FileNotFoundError as exc:
            executable = cmd[0] if cmd else "command"
            raise DraftPrPublisherError(f"executable not found: {executable}") from exc
        except subprocess.TimeoutExpired as exc:
            raise DraftPrPublisherError(f"command timed out: {' '.join(cmd)}") from exc
        if proc.returncode != 0:
            raise DraftPrPublisherError(_command_failure_message(cmd, cwd, proc))
        return proc.stdout


def _command_failure_message(
    cmd: list[str],
    cwd: Path | None,
    proc: subprocess.CompletedProcess[str],
) -> str:
    return "\n".join(
        part
        for part in [
            f"command failed ({proc.returncode}): {' '.join(cmd)}",
            f"cwd: {cwd}" if cwd else "",
            f"stdout: {_tail(proc.stdout)}" if _tail(proc.stdout) else "",
            f"stderr: {_tail(proc.stderr)}" if _tail(proc.stderr) else "",
        ]
        if part
    )


def _tail(value: str | None, *, limit: int = 2000) -> str:
    return str(value or "").strip()[-limit:]


def _extract_pr_url(value: str) -> str:
    for match in re.finditer(r"https://\S+", str(value or "")):
        candidate = match.group(0).rstrip(").,;:'\"")
        if _is_github_pull_request_url(candidate):
            return candidate
    return ""


def _is_github_pull_request_url(value: str) -> bool:
    parsed = urlparse(str(value or "").strip())
    host = str(parsed.hostname or "").strip().lower().rstrip(".")
    return (
        parsed.scheme.lower() == "https"
        and host == "github.com"
        and bool(re.fullmatch(r"/[^/]+/[^/]+/pull/\d+/?", parsed.path))
    )


def _validate_pr_body(
    body: str,
    *,
    expected_branch_name: str = "",
    expected_worktree: str = "",
) -> None:
    if not _has_linear_issue_context(body):
        raise DraftPrPublisherError("body must include Linear issue context.")
    if not _has_slack_thread_context(body):
        raise DraftPrPublisherError("body must include Slack thread context.")
    if not _verification_section_has_test_evidence(body):
        raise DraftPrPublisherError("body must include verification evidence.")
    if not _proof_section_text(body):
        raise DraftPrPublisherError("body must include proof evidence.")
    if not _proof_section_has_exact_target(body):
        raise DraftPrPublisherError("body must include proof target and expected text.")
    if generic_target_reason := _proof_section_generic_target_block_reason(body):
        raise DraftPrPublisherError(generic_target_reason)
    if not _proof_section_has_shareable_platform_links(body):
        raise DraftPrPublisherError(
            "body must include iOS and Android shareable proof links."
        )
    if not _proof_section_has_distinct_platform_links(body):
        raise DraftPrPublisherError(
            "body must include distinct iOS and Android shareable proof links."
        )
    missing_local_artifacts = _proof_section_missing_local_visual_artifacts(body)
    if missing_local_artifacts:
        raise DraftPrPublisherError(
            "body must include local iOS and Android proof artifacts: "
            f"missing {', '.join(missing_local_artifacts)}."
        )
    if not _proof_section_has_distinct_local_visual_artifacts(body):
        raise DraftPrPublisherError(
            "body must include distinct local iOS and Android proof artifacts."
        )
    if not _proof_section_has_local_manifest_artifact(body):
        raise DraftPrPublisherError("body must include local proof manifest artifact.")
    missing_local_artifact_files = _proof_section_missing_local_artifact_files(body)
    if missing_local_artifact_files:
        raise DraftPrPublisherError(
            "body local proof artifact files must exist and be non-empty: "
            f"{', '.join(missing_local_artifact_files)}."
        )
    invalid_local_image_files = _proof_section_invalid_local_image_artifact_files(body)
    if invalid_local_image_files:
        raise DraftPrPublisherError(
            "body local proof artifact files must be readable images: "
            f"{', '.join(invalid_local_image_files)}."
        )
    if not _has_base_commit_context(body):
        raise DraftPrPublisherError("body must include base commit context.")
    if not _proof_section_has_setup_and_capture_commands(body):
        raise DraftPrPublisherError("body must include proof setup and capture commands.")
    placeholder_commands = _proof_section_placeholder_commands(body)
    if placeholder_commands:
        raise DraftPrPublisherError(
            "body proof setup/capture commands must not be placeholder proof command(s): "
            f"{', '.join(placeholder_commands)}."
        )
    secret_command_assignments = _proof_section_inline_secret_env_assignments(body)
    if secret_command_assignments:
        raise DraftPrPublisherError(
            "body proof setup/capture commands must not inline secret env assignment(s): "
            f"{', '.join(secret_command_assignments)}."
        )
    required_env_assignments = _proof_section_required_env_key_assignments(body)
    if required_env_assignments:
        raise DraftPrPublisherError(
            "body proof required env key names must not include values: "
            f"{', '.join(required_env_assignments)}."
        )
    if not _proof_section_has_required_env_key_names(body):
        raise DraftPrPublisherError("body must include proof required env key names.")
    invalid_required_env_keys = _invalid_required_env_keys(
        tuple(_proof_section_required_env_key_names(body))
    )
    if invalid_required_env_keys:
        raise DraftPrPublisherError(
            "body proof required env key names must be real test-auth secret names, "
            "not built-in Monica proof context or invalid values: "
            f"{', '.join(invalid_required_env_keys)}."
        )
    literal_required_env_values = _proof_section_literal_required_env_value_keys(body)
    if literal_required_env_values:
        raise DraftPrPublisherError(
            "body proof setup/capture commands must not include literal required env "
            f"values: {', '.join(literal_required_env_values)}."
        )
    manifest_block_reason = _proof_section_manifest_block_reason(
        body,
        expected_branch_name=expected_branch_name,
        expected_worktree=expected_worktree,
    )
    if manifest_block_reason:
        raise DraftPrPublisherError(manifest_block_reason)


def _has_linear_issue_context(body: str) -> bool:
    value = _named_value(body, "Linear")
    if not _usable_context_value(value):
        return False
    url = _safe_shareable_url(value)
    if not url:
        return False
    host = str(urlparse(url).hostname or "").strip().lower().rstrip(".")
    path = str(urlparse(url).path or "").strip("/")
    return (host == "linear.app" or host.endswith(".linear.app")) and bool(path)


def _named_value(body: str, label: str) -> str:
    pattern = re.compile(rf"(?im)^\s*(?:-\s*)?{re.escape(label)}\s*:\s*(.+?)\s*$")
    match = pattern.search(body)
    return match.group(1).strip() if match else ""


def _has_slack_thread_context(body: str) -> bool:
    value = _named_value(body, "Slack")
    if not _usable_context_value(value):
        return False
    if _is_slack_context_url(value):
        return True
    channel = _labeled_inline_value(value, "channel")
    thread = _labeled_inline_value(value, "thread") or _labeled_inline_value(value, "thread_ts")
    return _is_slack_channel_id(channel) and _is_slack_thread_ts(thread)


def _is_slack_channel_id(value: str) -> bool:
    return bool(_SLACK_CHANNEL_ID_RE.fullmatch(str(value or "").strip()))


def _is_slack_thread_ts(value: str) -> bool:
    return bool(_SLACK_THREAD_TS_RE.fullmatch(str(value or "").strip()))


def _is_slack_context_url(value: str) -> bool:
    url = _safe_shareable_url(value)
    if not url:
        return False
    host = str(urlparse(url).hostname or "").strip().lower().rstrip(".")
    return host == "slack.com" or host.endswith(".slack.com")


def _labeled_inline_value(value: str, label: str) -> str:
    match = re.search(rf"(?i)\b{re.escape(label)}\s*=\s*([^\s]+)", str(value or ""))
    return match.group(1).strip() if match else ""


def _usable_context_value(value: str) -> bool:
    text = str(value or "").strip()
    return bool(text and text.casefold() not in {"unavailable", "none", "n/a"})


def _has_base_commit_context(body: str) -> bool:
    return bool(_body_base_context(body))


def _body_base_context(body: str) -> dict[str, str]:
    match = re.search(
        r"(?im)^\s*(?:-\s*)?Base\s*:\s*(origin/[^\s@]+)\s*@\s*([^\s]+)\s*$",
        body,
    )
    if not match:
        return {}
    base_ref = match.group(1).strip()
    base_commit = match.group(2).strip()
    if not is_safe_git_branch_name(base_ref) or not _looks_like_git_commit(base_commit):
        return {}
    return {"base_ref": base_ref, "base_commit": base_commit}


def _looks_like_git_commit(value: str) -> bool:
    return bool(_GIT_COMMIT_RE.fullmatch(str(value or "").strip()))


def _verification_section_text(body: str) -> str:
    return _clean_verification_section(_verification_section(body))


def _verification_section(body: str) -> str:
    match = re.search(r"(?ims)^##\s+Verification\s*$([\s\S]*?)(?:^##\s+|\Z)", body)
    if not match:
        return ""
    return match.group(1)


def _verification_section_has_test_evidence(body: str) -> bool:
    raw_section = _verification_section(body)
    if not raw_section:
        return False
    section = _clean_verification_section(raw_section)
    if not section:
        return False
    generic_success = {"verificationpassed", "verificationpassed."}
    if section.lower() in generic_success:
        return False
    return _verification_section_has_command_evidence(raw_section)


def _clean_verification_section(section: str) -> str:
    return re.sub(r"[`#\s-]+", "", section).strip()


def _verification_section_has_command_evidence(section: str) -> bool:
    command_re = re.compile(
        r"(?im)^\s*(?:[$>]\s*)?(?:"
        r"(?:uv\s+run\s+)?pytest\b"
        r"|python\s+-m\s+pytest\b"
        r"|npm\s+(?:test|run\s+[\w:.-]*(?:test|lint|type[-:]?check|check)[\w:.-]*)\b"
        r"|pnpm\s+(?:test|lint|run\s+[\w:.-]*(?:test|lint|type[-:]?check|check)[\w:.-]*)\b"
        r"|yarn\s+(?:test|lint|run\s+[\w:.-]*(?:test|lint|type[-:]?check|check)[\w:.-]*)\b"
        r"|(?:npx\s+)?(?:jest|vitest|detox|maestro)\b"
        r"|(?:\./)?gradlew\b"
        r"|gradle\b"
        r"|xcodebuild\b"
        r")"
    )
    return bool(command_re.search(section))


def _proof_section_text(body: str) -> str:
    section = _proof_section(body)
    if not section:
        return ""
    return re.sub(r"[`#\s-]+", "", section).strip()


def _proof_section(body: str) -> str:
    match = re.search(r"(?ims)^##\s+Proof\s*$([\s\S]*?)(?:^##\s+|\Z)", body)
    return match.group(1) if match else ""


def _proof_section_has_shareable_platform_links(body: str) -> bool:
    section = _proof_section(body)
    if not section:
        return False
    return _proof_section_has_platform_link(section, "ios") and _proof_section_has_platform_link(
        section,
        "android",
    )


def _proof_section_has_distinct_platform_links(body: str) -> bool:
    section = _proof_section(body)
    if not section:
        return False
    ios_url = _proof_section_platform_url(section, "ios")
    android_url = _proof_section_platform_url(section, "android")
    return bool(ios_url and android_url and ios_url != android_url)


def _proof_section_missing_local_visual_artifacts(body: str) -> list[str]:
    section = _proof_section(body)
    if not section:
        return ["ios", "android"]
    refs = _local_visual_artifact_refs(section)
    missing: list[str] = []
    for platform in ("ios", "android"):
        if not any(_artifact_ref_matches_platform(ref, platform) for ref in refs):
            missing.append(platform)
    return missing


def _proof_section_has_distinct_local_visual_artifacts(body: str) -> bool:
    section = _proof_section(body)
    if not section:
        return False
    refs = _local_visual_artifact_refs(section)
    ios_refs = {ref for ref in refs if _artifact_ref_matches_platform(ref, "ios")}
    android_refs = {ref for ref in refs if _artifact_ref_matches_platform(ref, "android")}
    return any(ios_ref != android_ref for ios_ref in ios_refs for android_ref in android_refs)


def _proof_section_has_local_manifest_artifact(body: str) -> bool:
    section = _proof_section(body)
    if not section:
        return False
    return any(Path(ref).name == _PROOF_MANIFEST_NAME for ref in _local_artifact_refs(section))


def _proof_section_manifest_block_reason(
    body: str,
    *,
    expected_branch_name: str = "",
    expected_worktree: str = "",
) -> str:
    section = _proof_section(body)
    if not section:
        return ""
    manifest_ref = _local_manifest_artifact_ref(section)
    if not manifest_ref:
        return ""
    manifest_path = Path(manifest_ref).expanduser()
    manifest_dir = manifest_path.parent
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return "body local proof manifest artifact must be readable JSON."
    if not isinstance(payload, dict):
        return "body local proof manifest artifact must be a JSON object."
    manifest_artifacts = _manifest_proof_artifact_keys(payload)
    if not manifest_artifacts:
        return "body local proof manifest artifact must list proof_artifacts."
    outside_manifest_dir = _manifest_artifact_refs_outside_dir(
        payload,
        manifest_dir=manifest_dir,
    )
    if outside_manifest_dir:
        return (
            "body local proof manifest artifacts must stay under the proof manifest "
            f"directory: {', '.join(outside_manifest_dir)}."
        )
    outside_body_artifacts = _body_artifact_refs_outside_dir(
        section,
        manifest_dir=manifest_dir,
        manifest_ref=manifest_ref,
    )
    if outside_body_artifacts:
        return (
            "body local proof artifacts must stay under the proof manifest "
            f"directory: {', '.join(outside_body_artifacts)}."
        )
    body_visual_artifacts = {
        _local_artifact_ref_key(ref)
        for ref in _local_visual_artifact_refs(section)
        if _local_artifact_ref_key(ref)
    }
    missing = sorted(body_visual_artifacts - manifest_artifacts)
    if missing:
        return (
            "body local proof manifest artifacts do not include local visual proof "
            f"artifact(s): {', '.join(missing)}."
        )
    manifest_target = _normalized_manifest_proof_target(payload.get("proof_target"))
    body_target = _proof_section_target(section)
    if not manifest_target:
        return "body local proof manifest target does not match proof target."
    if manifest_target != body_target:
        return "body local proof manifest target does not match proof target."
    manifest_base = _normalized_manifest_base_context(payload)
    body_base = _body_base_context(body)
    if not manifest_base:
        return "body local proof manifest base metadata does not match PR base metadata."
    if manifest_base != body_base:
        return "body local proof manifest base metadata does not match PR base metadata."
    manifest_setup_commands = _normalized_manifest_command_list(payload.get("setup_commands"))
    body_setup_commands = _normalized_command_list(
        _proof_labeled_block_commands(section, "Setup commands")
    )
    if not manifest_setup_commands:
        return "body local proof manifest setup commands do not match proof setup commands."
    if manifest_setup_commands != body_setup_commands:
        return "body local proof manifest setup commands do not match proof setup commands."
    manifest_proof_commands = _normalized_manifest_command_list(payload.get("commands"))
    body_proof_commands = _normalized_command_list(
        _proof_labeled_block_commands(section, "Proof commands")
    )
    if not manifest_proof_commands:
        return "body local proof manifest proof commands do not match proof commands."
    if manifest_proof_commands != body_proof_commands:
        return "body local proof manifest proof commands do not match proof commands."
    invalid_manifest_required_env_keys = _invalid_manifest_required_env_keys(
        payload.get("required_env_keys")
    )
    if invalid_manifest_required_env_keys:
        return (
            "body local proof manifest required env keys must be environment key names: "
            f"{', '.join(invalid_manifest_required_env_keys)}."
        )
    manifest_required_env_keys = _normalized_manifest_required_env_keys(
        payload.get("required_env_keys")
    )
    body_required_env_keys = _proof_section_required_env_key_names(body)
    if not manifest_required_env_keys:
        return "body local proof manifest required env keys do not match proof required env keys."
    if manifest_required_env_keys != body_required_env_keys:
        return "body local proof manifest required env keys do not match proof required env keys."
    manifest_platforms = _normalized_manifest_platforms(payload.get("platforms"))
    missing_platforms = sorted({"android", "ios"} - manifest_platforms)
    if missing_platforms:
        return (
            "body local proof manifest platforms must include iOS and Android: "
            f"missing {', '.join(missing_platforms)}."
        )
    auth_fallback_platforms = _manifest_auth_fallback_platforms(
        payload,
        manifest_dir=manifest_dir,
    )
    if auth_fallback_platforms:
        return (
            "body local proof manifest auth/onboarding proof fallback observed: "
            f"{', '.join(auth_fallback_platforms)}."
        )
    non_target_platforms = _manifest_non_target_screen_platforms(
        payload,
        manifest_dir=manifest_dir,
    )
    if non_target_platforms:
        return (
            "body local proof manifest non-target app screen observed: "
            f"{', '.join(non_target_platforms)}."
        )
    missing_target_route = _manifest_missing_target_route_platforms(
        payload,
        manifest_dir=manifest_dir,
    )
    if missing_target_route:
        return (
            "body local proof manifest target route evidence must include iOS and "
            f"Android artifacts: missing {', '.join(missing_target_route)}."
        )
    mismatched_target_route = _manifest_mismatched_target_route_platforms(
        payload,
        manifest_dir=manifest_dir,
    )
    if mismatched_target_route:
        return (
            "body local proof manifest target route does not match proof target: "
            f"{', '.join(mismatched_target_route)}."
        )
    missing_target_evidence = _manifest_missing_target_text_evidence(
        payload,
        manifest_dir=manifest_dir,
    )
    if missing_target_evidence:
        return (
            "body local proof manifest target text evidence must include iOS and "
            f"Android artifacts: missing {', '.join(missing_target_evidence)}."
        )
    manifest_run_id = str(payload.get("run_id") or "").strip()
    if not manifest_run_id:
        return "body local proof manifest run id does not identify a Monica run."
    manifest_linear = _normalized_manifest_linear_context(payload)
    body_linear = _body_linear_context(body)
    if not manifest_linear:
        return "body local proof manifest Linear issue does not match PR Linear issue."
    if not _linear_context_matches(manifest_linear, body_linear):
        return "body local proof manifest Linear issue does not match PR Linear issue."
    manifest_branch = str(payload.get("branch_name") or "").strip()
    if not manifest_branch:
        return "body local proof manifest branch does not match PR branch."
    if str(expected_branch_name or "").strip() and manifest_branch != str(
        expected_branch_name or ""
    ).strip():
        return "body local proof manifest branch does not match PR branch."
    manifest_worktree = str(payload.get("worktree") or payload.get("worktree_path") or "").strip()
    require_worktree = _should_require_manifest_worktree(expected_worktree)
    if require_worktree and not manifest_worktree:
        return "body local proof manifest worktree does not match PR worktree."
    if require_worktree and manifest_worktree and not _same_local_path(
        manifest_worktree,
        str(expected_worktree),
    ):
        return "body local proof manifest worktree does not match PR worktree."
    return ""


def _manifest_auth_fallback_platforms(
    payload: dict[str, object],
    *,
    manifest_dir: Path,
) -> list[str]:
    artifacts = _manifest_proof_artifact_refs(payload)
    observed: list[str] = []
    observed_artifacts: set[str] = set()
    for platform in ("ios", "android"):
        for artifact in artifacts:
            if not _artifact_ref_matches_platform(artifact, platform):
                continue
            if Path(artifact).suffix.lower() not in _TEXT_PROOF_SUFFIXES:
                continue
            artifact_path = _manifest_artifact_path(artifact, manifest_dir=manifest_dir)
            if not _path_is_under_dir(artifact_path, manifest_dir):
                continue
            if _text_shows_auth_fallback(_read_text_artifact_path(artifact_path)):
                observed.append(platform)
                observed_artifacts.add(_local_artifact_ref_key(artifact))
                break
    for artifact in artifacts:
        artifact_key = _local_artifact_ref_key(artifact)
        if artifact_key in observed_artifacts:
            continue
        if any(_artifact_ref_matches_platform(artifact, platform) for platform in ("ios", "android")):
            continue
        if Path(artifact).suffix.lower() not in _TEXT_PROOF_SUFFIXES:
            continue
        artifact_path = _manifest_artifact_path(artifact, manifest_dir=manifest_dir)
        if not _path_is_under_dir(artifact_path, manifest_dir):
            continue
        if _text_shows_auth_fallback(_read_text_artifact_path(artifact_path)):
            observed.append("unattributed")
            break
    return list(dict.fromkeys(observed))


def _manifest_non_target_screen_platforms(
    payload: dict[str, object],
    *,
    manifest_dir: Path,
) -> list[str]:
    artifacts = _manifest_proof_artifact_refs(payload)
    observed: list[str] = []
    for platform in ("ios", "android"):
        final_route = ""
        for artifact in artifacts:
            if not _artifact_ref_matches_platform(artifact, platform):
                continue
            if Path(artifact).suffix.lower() not in _TEXT_PROOF_SUFFIXES:
                continue
            artifact_path = _manifest_artifact_path(artifact, manifest_dir=manifest_dir)
            if not _path_is_under_dir(artifact_path, manifest_dir):
                continue
            route = _last_screen_load_route(_read_text_artifact_path(artifact_path))
            if route:
                final_route = route
        if final_route.casefold() in _NON_TARGET_SCREEN_ROUTES:
            observed.append(platform)
    return observed


def _manifest_missing_target_route_platforms(
    payload: dict[str, object],
    *,
    manifest_dir: Path,
) -> list[str]:
    artifacts = _manifest_proof_artifact_refs(payload)
    missing: list[str] = []
    for platform in ("ios", "android"):
        final_route = ""
        for artifact in artifacts:
            if not _artifact_ref_matches_platform(artifact, platform):
                continue
            if Path(artifact).suffix.lower() not in _TEXT_PROOF_SUFFIXES:
                continue
            artifact_path = _manifest_artifact_path(artifact, manifest_dir=manifest_dir)
            if not _path_is_under_dir(artifact_path, manifest_dir):
                continue
            route = _last_screen_load_route(_read_text_artifact_path(artifact_path))
            if route:
                final_route = route
        if not final_route:
            missing.append(platform)
    return missing


def _manifest_mismatched_target_route_platforms(
    payload: dict[str, object],
    *,
    manifest_dir: Path,
) -> list[str]:
    target_tokens = _target_route_tokens(
        _normalized_manifest_proof_target(payload.get("proof_target"))
    )
    if not target_tokens:
        return []
    artifacts = _manifest_proof_artifact_refs(payload)
    mismatched: list[str] = []
    for platform in ("ios", "android"):
        final_route = ""
        for artifact in artifacts:
            if not _artifact_ref_matches_platform(artifact, platform):
                continue
            if Path(artifact).suffix.lower() not in _TEXT_PROOF_SUFFIXES:
                continue
            artifact_path = _manifest_artifact_path(artifact, manifest_dir=manifest_dir)
            if not _path_is_under_dir(artifact_path, manifest_dir):
                continue
            route = _last_screen_load_route(_read_text_artifact_path(artifact_path))
            if route:
                final_route = route
        if final_route and not _route_matches_target(final_route, target_tokens):
            mismatched.append(platform)
    return mismatched


def _last_screen_load_route(value: object) -> str:
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


def _local_manifest_artifact_ref(section: str) -> str:
    for ref in _local_artifact_refs(section):
        if Path(ref).name == _PROOF_MANIFEST_NAME:
            return ref
    return ""


def _manifest_proof_artifact_keys(payload: dict[str, object]) -> set[str]:
    artifacts = payload.get("proof_artifacts")
    if not isinstance(artifacts, list):
        return set()
    return {
        _local_artifact_ref_key(str(item or ""))
        for item in artifacts
        if str(item or "").strip()
    }


def _manifest_proof_artifact_refs(payload: dict[str, object]) -> list[str]:
    artifacts = payload.get("proof_artifacts")
    if not isinstance(artifacts, list):
        return []
    return [str(item or "").strip() for item in artifacts if str(item or "").strip()]


def _manifest_artifact_refs_outside_dir(
    payload: dict[str, object],
    *,
    manifest_dir: Path,
) -> list[str]:
    outside: list[str] = []
    for artifact in _manifest_proof_artifact_refs(payload):
        artifact_path = _manifest_artifact_path(artifact, manifest_dir=manifest_dir)
        if not _path_is_under_dir(artifact_path, manifest_dir):
            outside.append(_local_artifact_ref_key(artifact) or artifact)
    return outside


def _body_artifact_refs_outside_dir(
    section: str,
    *,
    manifest_dir: Path,
    manifest_ref: str,
) -> list[str]:
    manifest_key = _local_artifact_ref_key(manifest_ref)
    outside: list[str] = []
    for artifact in _local_artifact_refs(section):
        artifact_key = _local_artifact_ref_key(artifact)
        if not artifact_key or artifact_key == manifest_key:
            continue
        artifact_path = _manifest_artifact_path(artifact, manifest_dir=manifest_dir)
        if _path_is_under_dir(artifact_path, manifest_dir):
            continue
        outside.append(artifact_key or artifact)
    return sorted(dict.fromkeys(outside))


def _normalized_manifest_proof_target(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        "deep_link": str(value.get("deep_link") or "").strip(),
        "expected_text": " ".join(str(value.get("expected_text") or "").split()),
        "screen": " ".join(str(value.get("screen") or "").split()),
    }


def _manifest_missing_target_text_evidence(
    payload: dict[str, object],
    *,
    manifest_dir: Path,
) -> list[str]:
    target = _normalized_manifest_proof_target(payload.get("proof_target"))
    expected_text = target.get("expected_text", "")
    if not expected_text:
        return ["ios", "android"]
    needle = _normalize_text_for_match(expected_text)
    artifacts = _manifest_proof_artifact_refs(payload)
    missing: list[str] = []
    used_artifacts: set[str] = set()
    for platform in ("ios", "android"):
        matched = ""
        for artifact in artifacts:
            artifact_key = _local_artifact_ref_key(artifact)
            if not artifact_key or artifact_key in used_artifacts:
                continue
            if not _artifact_ref_matches_platform(artifact, platform):
                continue
            if Path(artifact).suffix.lower() not in _TEXT_PROOF_SUFFIXES:
                continue
            artifact_path = _manifest_artifact_path(artifact, manifest_dir=manifest_dir)
            if not _path_is_under_dir(artifact_path, manifest_dir):
                continue
            if needle in _normalize_text_for_match(_read_text_artifact_path(artifact_path)):
                matched = artifact_key
                break
        if matched:
            used_artifacts.add(matched)
        else:
            missing.append(platform)
    return missing


def _normalize_text_for_match(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _text_shows_auth_fallback(value: object) -> bool:
    normalized = _normalize_text_for_match(value)
    if not normalized:
        return False
    return any(marker in normalized for marker in _AUTH_FALLBACK_MARKERS) and any(
        marker in normalized for marker in _AUTH_FALLBACK_DESTINATION_MARKERS
    )


def _manifest_artifact_path(value: str, *, manifest_dir: Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return manifest_dir / path


def _path_is_under_dir(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
    except ValueError:
        return False
    return True


def _read_text_artifact_path(path: Path) -> str:
    try:
        return path.read_bytes()[:_TEXT_PROOF_READ_LIMIT_BYTES].decode(
            "utf-8",
            errors="replace",
        )
    except Exception:
        return ""


def _proof_section_target(section: str) -> dict[str, str]:
    return {
        "deep_link": _proof_named_value(section, "Target"),
        "expected_text": " ".join(_proof_named_value(section, "Expected text").split()),
        "screen": " ".join(_proof_named_value(section, "Screen").split()),
    }


def _normalized_manifest_base_context(payload: dict[str, object]) -> dict[str, str]:
    base_ref = str(payload.get("base_ref") or payload.get("base_branch") or "").strip()
    base_commit = str(payload.get("base_commit") or "").strip()
    if not base_ref or not _looks_like_git_commit(base_commit):
        return {}
    return {"base_ref": base_ref, "base_commit": base_commit}


def _normalized_manifest_linear_context(payload: dict[str, object]) -> dict[str, str]:
    url = _normalize_shareable_url(payload.get("linear_url"))
    identifier = _linear_issue_key(payload.get("linear_identifier"))
    if not identifier:
        identifier = _linear_issue_key(url)
    if not url and not identifier:
        return {}
    return {"url": url, "identifier": identifier}


def _body_linear_context(body: str) -> dict[str, str]:
    value = _named_value(body, "Linear")
    url = _normalize_shareable_url(value)
    identifier = _linear_issue_key(value)
    if not identifier:
        identifier = _linear_issue_key(url)
    return {"url": url, "identifier": identifier}


def _linear_context_matches(
    manifest: dict[str, str],
    body: dict[str, str],
) -> bool:
    manifest_url = manifest.get("url", "")
    body_url = body.get("url", "")
    if manifest_url and body_url and manifest_url != body_url:
        return False
    manifest_identifier = manifest.get("identifier", "")
    body_identifier = body.get("identifier", "")
    if manifest_identifier and body_identifier and manifest_identifier != body_identifier:
        return False
    return bool((manifest_url and body_url) or (manifest_identifier and body_identifier))


def _normalize_shareable_url(value: object) -> str:
    url = _safe_shareable_url(str(value or ""))
    return url.rstrip("/") if url else ""


def _linear_issue_key(value: object) -> str:
    match = _LINEAR_ISSUE_KEY_RE.search(str(value or "").upper())
    return match.group(0).casefold() if match else ""


def _should_require_manifest_worktree(value: object) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    path = Path(text).expanduser()
    return path.is_dir() and (path / ".git").exists()


def _same_local_path(left: str, right: str) -> bool:
    try:
        return Path(left).expanduser().resolve(strict=False) == Path(right).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return str(Path(left).expanduser()) == str(Path(right).expanduser())


def _normalized_manifest_command_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return _normalized_command_list(value)


def _normalized_manifest_required_env_keys(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted(
        {
            str(item or "").strip()
            for item in value
            if _REQUIRED_ENV_KEY_RE.fullmatch(str(item or "").strip())
        }
    )


def _invalid_manifest_required_env_keys(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    invalid = {
        str(item or "").strip()
        for item in value
        if str(item or "").strip()
        and not _REQUIRED_ENV_KEY_RE.fullmatch(str(item or "").strip())
    }
    return sorted(invalid)


def _normalized_manifest_platforms(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {
        str(item or "").strip().casefold()
        for item in value
        if str(item or "").strip()
    }


def _normalized_command_list(value: object) -> list[str]:
    if isinstance(value, str):
        candidates = (value,)
    elif isinstance(value, (list, tuple)):
        candidates = value
    else:
        return []
    return [" ".join(str(item or "").split()) for item in candidates if str(item or "").strip()]


def _local_artifact_ref_key(ref: str) -> str:
    text = str(ref or "").strip()
    if not text:
        return ""
    return str(Path(text).expanduser())


def _proof_section_missing_local_artifact_files(body: str) -> list[str]:
    section = _proof_section(body)
    if not section:
        return []
    missing: list[str] = []
    for ref in _required_local_artifact_refs(section):
        if not _local_artifact_ref_has_bytes(ref):
            missing.append(ref)
    return sorted(dict.fromkeys(missing))


def _required_local_artifact_refs(section: str) -> list[str]:
    refs: list[str] = []
    for ref in _local_artifact_refs(section):
        path = Path(ref)
        if path.name == _PROOF_MANIFEST_NAME or path.suffix.lower() in _VISUAL_PROOF_SUFFIXES:
            refs.append(ref)
    return refs


def _local_artifact_ref_has_bytes(ref: str) -> bool:
    try:
        path = Path(ref).expanduser()
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _proof_section_invalid_local_image_artifact_files(body: str) -> list[str]:
    section = _proof_section(body)
    if not section:
        return []
    invalid = [
        ref
        for ref in _local_visual_artifact_refs(section)
        if Path(ref).suffix.lower() in _RASTER_IMAGE_PROOF_SUFFIXES
        and not _local_image_artifact_is_readable(ref)
    ]
    return sorted(dict.fromkeys(invalid))


def _local_image_artifact_is_readable(ref: str) -> bool:
    path = Path(ref).expanduser()
    if not _local_artifact_ref_has_bytes(ref):
        return False
    try:
        from PIL import Image

        with Image.open(path) as image:
            image.verify()
    except Exception:
        return False
    return True


def _local_visual_artifact_refs(section: str) -> list[str]:
    return [
        ref
        for ref in _local_artifact_refs(section)
        if Path(ref).suffix.lower() in _VISUAL_PROOF_SUFFIXES
    ]


def _local_artifact_refs(section: str) -> list[str]:
    refs: list[str] = []
    for line in section.splitlines():
        if _proof_named_metadata_line(line):
            continue
        for token in re.split(r"[\s,]+", line):
            candidate = token.strip("`'\"()[]{}<>,.;")
            if not candidate or "://" in candidate:
                continue
            if not _looks_like_local_artifact_ref(candidate):
                continue
            refs.append(candidate)
    return refs


def _proof_named_metadata_line(value: str) -> bool:
    return bool(
        re.match(
            r"^\s*(?:-\s*)?(?:Target|Expected text|Screen|Required env keys)\s*:",
            str(value or ""),
            flags=re.IGNORECASE,
        )
    )


def _looks_like_local_artifact_ref(value: str) -> bool:
    text = str(value or "").strip()
    return bool(text and (text.startswith(("/", "./", "../", "~")) or "/" in text))


def _artifact_ref_matches_platform(value: str, platform: str) -> bool:
    path = Path(value)
    haystack = " ".join((path.name, path.stem, *path.parts[-3:])).casefold()
    if platform == "ios":
        return any(token in haystack for token in ("ios", "iphone", "ipad"))
    return platform in haystack


def _proof_section_has_exact_target(body: str) -> bool:
    section = _proof_section(body)
    if not section:
        return False
    target = _proof_named_value(section, "Target")
    expected_text = _proof_named_value(section, "Expected text")
    return _looks_like_deep_link(target) and _usable_expected_text(expected_text)


def _usable_expected_text(value: str) -> bool:
    text = str(value or "").strip()
    return bool(
        text
        and _expected_text_unusable_key(text)
        not in _UNUSABLE_EXPECTED_TEXT_VALUES
    )


def _expected_text_unusable_key(value: str) -> str:
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


_UNUSABLE_EXPECTED_TEXT_VALUES = {
    "home",
    "log in",
    "login",
    "marketplace",
    "n/a",
    "na",
    "none",
    "onboarding",
    "offer",
    "offers",
    "pdp",
    "product",
    "products",
    "sign in",
    "sign-in",
    "signin",
    "text visible on fixed screen",
    "text visible on the fixed screen",
    "unknown",
    "unavailable",
}


def _proof_section_has_setup_and_capture_commands(body: str) -> bool:
    section = _proof_section(body)
    if not section:
        return False
    return _proof_labeled_block_has_command(section, "Setup commands") and _proof_labeled_block_has_command(
        section,
        "Proof commands",
    )


def _proof_section_inline_secret_env_assignments(body: str) -> list[str]:
    section = _proof_section(body)
    if not section:
        return []
    commands = [
        *_proof_labeled_block_commands(section, "Setup commands"),
        *_proof_labeled_block_commands(section, "Proof commands"),
    ]
    return _inline_secret_env_assignments(tuple(commands))


def _proof_section_literal_required_env_value_keys(body: str) -> list[str]:
    section = _proof_section(body)
    if not section:
        return []
    required_keys = tuple(_proof_section_required_env_key_names(body))
    if not required_keys:
        return []
    commands = (
        *_proof_labeled_block_commands(section, "Setup commands"),
        *_proof_labeled_block_commands(section, "Proof commands"),
    )
    env = dict(os.environ)
    env.update(_profile_env_values())
    return _required_env_value_keys_in_commands(
        commands,
        env=env,
        required_env_keys=required_keys,
    )


def _proof_section_placeholder_commands(body: str) -> list[str]:
    section = _proof_section(body)
    if not section:
        return []
    placeholders = [
        *_placeholder_proof_setup_commands(
            tuple(_proof_labeled_block_commands(section, "Setup commands"))
        ),
        *_placeholder_proof_commands(
            tuple(_proof_labeled_block_commands(section, "Proof commands"))
        ),
    ]
    return sorted(dict.fromkeys(placeholders))


def _proof_section_required_env_key_assignments(body: str) -> list[str]:
    section = _proof_section(body)
    if not section:
        return []
    assignments: list[str] = []
    lines = section.splitlines()
    for index, line in enumerate(lines):
        if _proof_metadata_label(line) != "required env keys":
            continue
        _, _, inline_value = line.partition(":")
        assignments.extend(_required_env_key_assignment_names(inline_value))
        for candidate in lines[index + 1 :]:
            if _proof_metadata_label(candidate):
                break
            text = candidate.strip()
            if text.startswith("-"):
                text = text[1:].strip()
            assignments.extend(_required_env_key_assignment_names(text))
    return sorted(dict.fromkeys(assignments))


def _proof_section_has_required_env_key_names(body: str) -> bool:
    return bool(_proof_section_required_env_key_names(body))


def _proof_section_required_env_key_names(body: str) -> list[str]:
    section = _proof_section(body)
    if not section:
        return []
    names: list[str] = []
    lines = section.splitlines()
    for index, line in enumerate(lines):
        if _proof_metadata_label(line) != "required env keys":
            continue
        _, _, inline_value = line.partition(":")
        names.extend(_required_env_key_names(inline_value))
        for candidate in lines[index + 1 :]:
            if _proof_metadata_label(candidate):
                break
            text = candidate.strip()
            if text.startswith("-"):
                text = text[1:].strip()
            names.extend(_required_env_key_names(text))
    return sorted(dict.fromkeys(names))


def _required_env_key_names(value: str) -> list[str]:
    names: list[str] = []
    for raw_token in re.split(r"[\s,]+", str(value or "").strip()):
        token = raw_token.strip().strip("`'\"").rstrip(".,;")
        if not token or token.casefold() in {"none", "n/a", "na", "unknown", "unavailable"}:
            continue
        if "=" in token:
            continue
        if _REQUIRED_ENV_KEY_RE.fullmatch(token):
            names.append(token)
    return names


def _required_env_key_assignment_names(value: str) -> list[str]:
    return [
        match.group(1)
        for match in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=", str(value or ""))
    ]


def _proof_labeled_block_has_command(section: str, label: str) -> bool:
    return bool(_proof_labeled_block_commands(section, label))


def _proof_labeled_block_commands(section: str, label: str) -> list[str]:
    lines = section.splitlines()
    for index, line in enumerate(lines):
        if _proof_metadata_label(line) != label.casefold():
            continue
        commands: list[str] = []
        for candidate in lines[index + 1 :]:
            if _proof_metadata_label(candidate):
                break
            command = _proof_command_entry(candidate)
            if command:
                commands.append(command)
        return commands
    return []


def _proof_metadata_label(line: str) -> str:
    text = line.strip()
    if text.startswith("-"):
        text = text[1:].strip()
    if ":" not in text:
        return ""
    label, value = text.split(":", 1)
    normalized = label.strip().casefold()
    if normalized in {
        "android",
        "expected text",
        "ios",
        "local artifacts",
        "platforms",
        "proof commands",
        "required env keys",
        "target",
        "setup commands",
    }:
        return normalized
    return "" if value.strip() else normalized


def _proof_command_entry(line: str) -> str:
    text = line.strip()
    if not text.startswith("- ") or _proof_metadata_label(text):
        return ""
    command = text[2:].strip()
    if not command:
        return ""
    if command.casefold() in {"none", "n/a", "na", "unknown", "unavailable"}:
        return ""
    if _is_noop_shell_command(command):
        return ""
    return command


def _proof_named_value(section: str, label: str) -> str:
    pattern = re.compile(rf"(?im)^\s*(?:-\s*)?{re.escape(label)}\s*:\s*(.+?)\s*$")
    match = pattern.search(section)
    return match.group(1).strip() if match else ""


def _looks_like_deep_link(value: str) -> bool:
    text = str(value or "").strip()
    if not text or any(char.isspace() for char in text):
        return False
    return "://" in text or text.startswith(("exp+", "http://", "https://"))


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


def _proof_section_generic_target_block_reason(body: str) -> str:
    section = _proof_section(body)
    if not section:
        return ""
    target = _proof_named_value(section, "Target")
    parsed = urlparse(target)
    if parsed.scheme.casefold() in _EXPO_RUNTIME_PROOF_TARGET_SCHEMES:
        return "body Expo runtime proof target is not enough."
    if parsed.scheme.casefold() in {"http", "https"} and _is_local_shareable_host(parsed.hostname):
        return "body local proof target is not enough."
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
        return "body Expo Dev Client proof target is not enough."
    if not tokens or all(token in _GENERIC_PROOF_TARGET_TOKENS for token in tokens):
        return "body generic proof target is not enough."
    if any(token in _AUTH_PROOF_TARGET_TOKENS for token in tokens):
        return "body auth/onboarding proof target is not enough."
    return ""


def _proof_section_has_platform_link(section: str, platform: str) -> bool:
    return bool(_proof_section_platform_url(section, platform))


def _proof_section_platform_url(section: str, platform: str) -> str:
    labels = ("ios", "iphone", "ipad") if platform == "ios" else ("android",)
    for line in section.splitlines():
        if not any(label in line.casefold() for label in labels):
            continue
        for url in _http_urls(line):
            safe_url = _safe_shareable_url(url)
            if safe_url:
                return safe_url
    return ""


def _http_urls(value: str) -> list[str]:
    return [url.rstrip(").,;]}>") for url in re.findall(r"https?://[^\s<>()]+", value)]


def _safe_shareable_url(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    if _is_local_shareable_host(parsed.hostname):
        return ""
    return text


def _is_local_shareable_host(hostname: str | None) -> bool:
    host = str(hostname or "").strip().lower().rstrip(".")
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ip_address(host)
    except ValueError:
        return "." not in host or host.endswith(_LOCAL_SHAREABLE_HOST_SUFFIXES)
    return (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_unspecified
    )
