from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from copy import deepcopy
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlparse

from .config import MonicaConfig, runtime_root
from .proof import (
    _auth_fallback_platforms,
    _missing_target_route_platforms,
    _mismatched_target_route_platforms,
    _non_target_screen_platforms,
)
from .readiness import (
    _invalid_required_env_keys,
    _inline_secret_env_assignments,
    _noop_only_proof_commands,
    _noop_only_proof_setup_commands,
    _placeholder_proof_commands,
    _placeholder_proof_setup_commands,
    _profile_env_values,
    _required_env_value_keys_in_commands,
)
from .repo_manager import configured_remote_base_ref, is_safe_git_branch_name
from .state import MonicaState

SUPPORTED_ROLLOUT_MODES = {"dry_run", "linear_only", "local_fix_only", "approved_pr"}
LINEAR_ROLLOUT_MODES = {"linear_only", "local_fix_only", "approved_pr"}
CODE_ROLLOUT_MODES = {"local_fix_only", "approved_pr"}
APPROVED_PR_REQUIRED_PROOF_PLATFORMS = ("ios", "android")
PROOF_MANIFEST_NAME = "monica-proof-manifest.json"
LOCAL_SHAREABLE_HOST_SUFFIXES = (".internal", ".lan", ".local")
VISUAL_PROOF_SUFFIXES = {
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
TEXT_PROOF_SUFFIXES = {".html", ".json", ".log", ".txt", ".xml"}
TEXT_PROOF_READ_LIMIT_BYTES = 1_000_000
APPROVED_PR_FIX_SCOPE_LABEL = "marketplace copy/design"
APPROVED_PR_FIX_SURFACE_TERMS = (
    "marketplace",
    "pdp",
    "product detail",
    "offer detail",
    "offer card",
)
APPROVED_PR_FIX_KIND_TERMS = (
    "badge",
    "color",
    "content",
    "copy",
    "design",
    "font",
    "label",
    "layout",
    "spacing",
    "style",
    "tag",
    "text",
    "title",
    "visual",
    "wording",
)
APPROVED_PR_FIX_EXCLUDED_TERMS = (
    "api",
    "backend",
    "build failure",
    "checkout",
    "crash",
    "hang",
    "latency",
    "native module",
    "navigation",
    "payment",
    "performance",
    "slow",
    "slowness",
    "timeout",
)
GIT_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
LINEAR_ISSUE_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
_SLACK_MENTION_RE = re.compile(r"<@([A-Z0-9_]+)(?:\|[^>]+)?>")
logger = logging.getLogger(__name__)


class MonicaLoopSkills(Protocol):
    def read_slack_thread(self, run: Any) -> dict[str, Any]:
        ...

    def infer_user_intent(self, run: Any, thread: dict[str, Any]) -> dict[str, Any]:
        ...

    def create_or_update_linear(
        self,
        run: Any,
        thread: dict[str, Any],
        intent: dict[str, Any],
    ) -> dict[str, Any]:
        ...

    def ask_fix_approval(self, run: Any, issue: dict[str, Any]) -> None:
        ...

    def run_internal_codex_worker(self, run: Any) -> dict[str, Any]:
        ...

    def run_verification(self, run: Any, worker_result: dict[str, Any]) -> dict[str, Any]:
        ...

    def run_proof(
        self,
        run: Any,
        worker_result: dict[str, Any],
        verification: dict[str, Any],
    ) -> dict[str, Any]:
        ...

    def open_draft_pr(
        self,
        run: Any,
        worker_result: dict[str, Any],
        verification: dict[str, Any],
    ) -> dict[str, Any]:
        ...

    def share_proof_artifacts(self, run: Any, proof: dict[str, Any]) -> dict[str, Any]:
        ...


class MonicaLoop:
    def __init__(self, *, config: MonicaConfig, state: MonicaState, skills: MonicaLoopSkills) -> None:
        self.config = config
        self.state = state
        self.skills = skills

    def run(self, run_id: str) -> None:
        run = self.state.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        try:
            self._run(run)
        except Exception as exc:
            self._mark_failed(run_id, exc)

    def _run(self, run: Any) -> None:
        if self.config.rollout_mode not in SUPPORTED_ROLLOUT_MODES:
            blocked = self.state.update_run(
                run.id,
                status="blocked",
                failure_reason=f"unknown_rollout_mode: {self.config.rollout_mode}",
            )
            self._log_run("blocked", blocked, stage="preflight")
            self._post_status(
                blocked,
                "Monica is not configured with a known rollout mode, so I stopped before taking action. "
                "Use one of: `dry_run`, `linear_only`, `local_fix_only`, `approved_pr`.",
            )
            return

        if self.config.rollout_mode in LINEAR_ROLLOUT_MODES and not self.config.loop.create_linear:
            blocked = self.state.update_run(
                run.id,
                status="blocked",
                failure_reason="linear_creation_disabled_in_rollout",
            )
            self._log_run("blocked", blocked, stage="preflight")
            self._post_status(
                blocked,
                "Linear creation is disabled for this Monica rollout mode, so I stopped before taking action.",
            )
            return

        if (
            self.config.rollout_mode == "approved_pr"
            and run.status in {"approved", "proof_blocked", "proofing"}
            and str(getattr(run, "pr_url", "") or "").strip()
        ):
            blocked = self.state.update_run(
                run.id,
                status="blocked",
                failure_reason="draft_pr_already_exists",
            )
            self._log_run("blocked", blocked, stage="preflight")
            self._post_status(
                blocked,
                "I stopped because this Monica run already has a draft PR: "
                f"{blocked.pr_url}. Open a new tagged Slack request for follow-up work.",
            )
            return

        if run.status == "approved":
            self._run_approved_fix(run)
            return

        if run.status in {"proof_blocked", "proofing"}:
            self._resume_after_proof_blocked(run)
            return

        if run.status not in {"queued", "needs_clarification"}:
            return

        if self.config.rollout_mode == "approved_pr":
            intake_block_reason = _approved_pr_slack_intake_block_reason(run, self.config)
            if intake_block_reason:
                blocked = self.state.update_run(
                    run.id,
                    status="blocked",
                    failure_reason=intake_block_reason,
                )
                self._log_run("blocked", blocked, stage="preflight")
                self._post_status(
                    blocked,
                    "Monica approved-PR work only starts from a tagged Slack message or direct message. "
                    "Tagged channel messages must come from an allowed Slack channel, "
                    "so I stopped before filing Linear, changing code, or opening a PR.",
                )
                return

        run = self.state.update_run(run.id, status="triaging")
        thread = self.skills.read_slack_thread(run)
        if self._is_cancelled(run.id):
            return
        intent = self.skills.infer_user_intent(run, thread)
        if self._is_cancelled(run.id):
            return

        if intent.get("needs_clarification"):
            needs_clarification = self.state.update_run(run.id, status="needs_clarification")
            self._log_run("needs_clarification", needs_clarification, stage="triaging")
            self._post_status(needs_clarification, self._clarification_text(intent))
            return

        if not intent.get("is_mobile_bug"):
            blocked = self.state.update_run(run.id, status="blocked", failure_reason="not_a_mobile_bug")
            self._log_run("blocked", blocked, stage="triaging")
            self._post_status(
                blocked,
                "I could not confidently classify this as a mobile app bug. "
                "Tag me again with the app/platform details if you want me to file it.",
            )
            return

        if intent.get("wants_linear") is False and not intent.get("wants_fix"):
            needs_clarification = self.state.update_run(run.id, status="needs_clarification")
            self._log_run("needs_clarification", needs_clarification, stage="triaging")
            self._post_status(
                needs_clarification,
                "I see mobile app bug context here, but I do not have a clear next step yet. "
                "Tag me again if you want me to file a Linear issue or prepare a fix.",
            )
            return

        if self.config.rollout_mode == "approved_pr" and not _is_approved_pr_fix_scope(
            run,
            intent=intent,
        ):
            blocked = self.state.update_run(
                run.id,
                status="blocked",
                failure_reason="approved_pr_fix_scope_unsupported",
            )
            self._log_run("blocked", blocked, stage="triaging")
            self._post_status(
                blocked,
                "Monica approved-PR work is limited to "
                f"{APPROVED_PR_FIX_SCOPE_LABEL} bugs in the React Native mobile app, "
                "so I stopped before filing Linear or changing code.",
            )
            return

        if self.config.loop.create_linear:
            run = self.state.update_run(run.id, status="creating_linear")
            issue = self.skills.create_or_update_linear(run, thread, intent)
            if self._is_cancelled(run.id):
                return
        else:
            issue = {"identifier": "", "url": "", "dry_run": True, "title": intent.get("summary", "")}
        run = self.state.update_run(
            run.id,
            status="linear_created",
            linear_identifier=issue.get("identifier", ""),
            linear_issue_id=issue.get("id", ""),
            linear_url=issue.get("url", ""),
        )

        if issue.get("dry_run"):
            self._post_status(run, self._linear_done_text(issue))
            completed = self.state.update_run(run.id, status="done")
            self._log_run("done", completed, stage="dry_run")
            return

        if intent.get("wants_fix") and self.config.rollout_mode not in CODE_ROLLOUT_MODES:
            self._post_status(
                run,
                self._linear_done_text(issue)
                + "\n\nCode fixes are disabled in the current Monica rollout mode.",
            )
            completed = self.state.update_run(run.id, status="done")
            self._log_run("done", completed, stage=self.config.rollout_mode)
            return

        if intent.get("wants_fix"):
            run = self.state.update_run(run.id, status="awaiting_fix_approval")
            self._log_run("awaiting_fix_approval", run, stage="linear_created")
            self.skills.ask_fix_approval(run, issue)
            return

        self._post_status(run, self._linear_done_text(issue))
        completed = self.state.update_run(run.id, status="done")
        self._log_run("done", completed, stage=self.config.rollout_mode)

    def _run_approved_fix(self, run: Any) -> None:
        if self.config.rollout_mode not in CODE_ROLLOUT_MODES:
            blocked = self.state.update_run(
                run.id,
                status="blocked",
                failure_reason="approved_pr_rollout_not_enabled",
            )
            self._log_run("blocked", blocked, stage="preflight")
            self._post_status(
                blocked,
                "I have approval, but code rollout is not enabled. "
                "Set `mobile_bug_agent.rollout_mode` to `local_fix_only` or `approved_pr` before I write code.",
            )
            return

        if not (run.linear_identifier or run.linear_issue_id or run.linear_url):
            blocked = self.state.update_run(
                run.id,
                status="blocked",
                failure_reason="linear_issue_missing_before_fix",
            )
            self._log_run("blocked", blocked, stage="preflight")
            self._post_status(
                blocked,
                "I have approval, but no Linear issue is attached to this Monica run, "
                "so I stopped before writing code.",
            )
            return

        if self.config.rollout_mode == "approved_pr" and not str(run.linear_url or "").strip():
            blocked = self.state.update_run(
                run.id,
                status="blocked",
                failure_reason="linear_issue_url_missing_before_fix",
            )
            self._log_run("blocked", blocked, stage="preflight")
            self._post_status(
                blocked,
                "I have approval, but the attached Linear issue URL is missing, "
                "so I stopped before writing code.",
            )
            return

        if self.config.rollout_mode == "approved_pr" and not _is_linear_issue_url(run.linear_url):
            blocked = self.state.update_run(
                run.id,
                status="blocked",
                failure_reason="linear_issue_url_invalid_before_fix",
            )
            self._log_run("blocked", blocked, stage="preflight")
            self._post_status(
                blocked,
                "I have approval, but the attached Linear issue URL is not a Linear issue link, "
                "so I stopped before writing code.",
            )
            return

        if not any(command.strip() for command in self.config.verification.commands):
            blocked = self.state.update_run(
                run.id,
                status="blocked",
                failure_reason="verification_commands_missing",
            )
            self._log_run("blocked", blocked, stage="preflight")
            self._post_status(
                blocked,
                "I have approval, but `mobile_bug_agent.verification.commands` is empty, "
                "so I stopped before writing code.",
            )
            return

        if self.config.rollout_mode == "approved_pr":
            approval_block_reason = _approved_pr_code_approval_block_reason(run, self.config)
            if approval_block_reason:
                blocked = self.state.update_run(
                    run.id,
                    status="blocked",
                    failure_reason=approval_block_reason,
                )
                self._log_run("blocked", blocked, stage="preflight")
                self._post_status(
                    blocked,
                    "I have approval status, but Monica approved-PR code work requires "
                    "explicit configured approver approval, so I stopped before writing code.",
                )
                return

        if self.config.rollout_mode == "approved_pr" and not _is_approved_pr_fix_scope(run):
            blocked = self.state.update_run(
                run.id,
                status="blocked",
                failure_reason="approved_pr_fix_scope_unsupported",
            )
            self._log_run("blocked", blocked, stage="preflight")
            self._post_status(
                blocked,
                "I have approval, but Monica approved-PR fixes are limited to "
                f"{APPROVED_PR_FIX_SCOPE_LABEL} bugs in the React Native mobile app, "
                "so I stopped before writing code.",
            )
            return

        if self.config.rollout_mode == "approved_pr":
            intake_block_reason = _approved_pr_slack_intake_block_reason(run, self.config)
            if intake_block_reason:
                blocked = self.state.update_run(
                    run.id,
                    status="blocked",
                    failure_reason=intake_block_reason,
                )
                self._log_run("blocked", blocked, stage="preflight")
                self._post_status(
                    blocked,
                    "I have approval, but Monica approved-PR code work must start from "
                    "a tagged Slack message or direct message. "
                    "Tagged channel messages must come from an allowed Slack channel, "
                    "so I stopped before writing code.",
                )
                return

        run = self.state.update_run(run.id, status="fixing")
        worker_result = self.skills.run_internal_codex_worker(run)
        if self._is_cancelled(run.id):
            return
        branch_name = str(worker_result.get("branch_name") or run.branch_name or "")
        if not branch_name:
            blocked = self.state.update_run(
                run.id,
                status="blocked",
                failure_reason="worker_branch_missing",
            )
            self._log_run("blocked", blocked, stage="fixing")
            self._post_status(
                blocked,
                "I stopped after the code worker because I could not identify a branch to verify and push.",
            )
            return
        if not self._is_expected_worker_branch(run=run, branch_name=branch_name):
            blocked = self.state.update_run(
                run.id,
                status="blocked",
                failure_reason="worker_branch_mismatch",
            )
            self._log_run("blocked", blocked, stage="fixing")
            self._post_status(
                blocked,
                "I stopped after the code worker because it returned an unexpected branch "
                f"`{branch_name}` for this Monica run.",
            )
            return
        update_fields = _worker_proof_target_update_fields(worker_result)
        if worker_result.get("changed") is False:
            run = self.state.update_run(
                run.id,
                branch_name=branch_name,
                **update_fields,
            )
            blocked = self.state.update_run(
                run.id,
                status="blocked",
                failure_reason="worker_no_changes",
            )
            self._log_run("blocked", blocked, stage="fixing")
            self._post_status(
                blocked,
                "I stopped after the code worker because it did not report any code changes.",
            )
            return
        base_metadata_block_reason = _approved_pr_base_metadata_block_reason(worker_result, self.config)
        if base_metadata_block_reason:
            blocked = self.state.update_run(
                run.id,
                status="blocked",
                failure_reason=base_metadata_block_reason,
            )
            self._log_run("blocked", blocked, stage="fixing")
            self._post_status(
                blocked,
                "I stopped before verification because the code worker did not return "
                "fresh mobile base commit metadata from the configured default branch.",
            )
            return
        worker_base_ref = str(worker_result.get("base_ref") or worker_result.get("base_branch") or "").strip()
        worker_base_commit = str(worker_result.get("base_commit") or "").strip()
        if worker_base_ref:
            update_fields["base_branch"] = worker_base_ref
        if worker_base_commit:
            update_fields["base_commit"] = worker_base_commit
        run = self.state.update_run(
            run.id,
            branch_name=branch_name,
            **update_fields,
        )

        self._run_post_worker_gates(run, worker_result)

    def _resume_after_proof_blocked(self, run: Any) -> None:
        if self.config.rollout_mode not in CODE_ROLLOUT_MODES:
            blocked = self.state.update_run(
                run.id,
                status="blocked",
                failure_reason="proof_retry_rollout_not_enabled",
            )
            self._log_run("blocked", blocked, stage="preflight")
            self._post_status(
                blocked,
                "I have a proof-blocked code branch, but code rollout is not enabled. "
                "Set `mobile_bug_agent.rollout_mode` to `local_fix_only` or `approved_pr` before retrying.",
            )
            return

        if self.config.rollout_mode == "approved_pr" and not _is_approved_pr_fix_scope(run):
            blocked = self.state.update_run(
                run.id,
                status="blocked",
                failure_reason="approved_pr_fix_scope_unsupported",
            )
            self._log_run("blocked", blocked, stage="preflight")
            self._post_status(
                blocked,
                "I have a proof-blocked branch, but Monica approved-PR fixes are limited to "
                f"{APPROVED_PR_FIX_SCOPE_LABEL} bugs in the React Native mobile app, "
                "so I stopped before retrying proof.",
            )
            return

        if not any(command.strip() for command in self.config.verification.commands):
            blocked = self.state.update_run(
                run.id,
                status="blocked",
                failure_reason="verification_commands_missing",
            )
            self._log_run("blocked", blocked, stage="preflight")
            self._post_status(
                blocked,
                "I have a proof-blocked branch, but `mobile_bug_agent.verification.commands` is empty, "
                "so I stopped before retrying verification or proof.",
            )
            return

        branch_name = str(getattr(run, "branch_name", "") or "").strip()
        if not branch_name:
            blocked = self.state.update_run(
                run.id,
                status="blocked",
                failure_reason="proof_retry_branch_missing",
            )
            self._log_run("blocked", blocked, stage="preflight")
            self._post_status(
                blocked,
                "I cannot retry proof because this Monica run does not have a stored branch name.",
            )
            return
        if not is_safe_git_branch_name(branch_name) or not self._is_expected_worker_branch(
            run=run, branch_name=branch_name
        ):
            blocked = self.state.update_run(
                run.id,
                status="blocked",
                failure_reason="proof_retry_branch_mismatch",
            )
            self._log_run("blocked", blocked, stage="preflight")
            self._post_status(
                blocked,
                "I cannot retry proof because the stored branch does not look like the expected Monica branch "
                f"for this run: `{branch_name}`.",
            )
            return

        worktree_path = (
            runtime_root(self.config)
            / "workspace"
            / "worktrees"
            / branch_name.replace("/", "-")
        )
        if not worktree_path.is_dir() or not (worktree_path / ".git").exists():
            blocked = self.state.update_run(
                run.id,
                status="blocked",
                failure_reason="proof_retry_worktree_missing",
            )
            self._log_run("blocked", blocked, stage="preflight")
            self._post_status(
                blocked,
                "I cannot retry proof because the stored Monica worktree is missing: "
                f"`{worktree_path}`.",
            )
            return
        current_branch = _worktree_current_branch(worktree_path)
        if current_branch != branch_name:
            blocked = self.state.update_run(
                run.id,
                status="blocked",
                failure_reason="proof_retry_worktree_branch_mismatch",
            )
            self._log_run("blocked", blocked, stage="preflight")
            actual = current_branch or "detached HEAD or unreadable branch"
            self._post_status(
                blocked,
                "I cannot retry proof because the stored Monica worktree is not on "
                f"the stored Monica branch `{branch_name}`. Current branch: `{actual}`.",
            )
            return
        if self.config.rollout_mode == "approved_pr":
            if not (run.linear_identifier or run.linear_issue_id or run.linear_url):
                blocked = self.state.update_run(
                    run.id,
                    status="blocked",
                    failure_reason="linear_issue_missing_before_fix",
                )
                self._log_run("blocked", blocked, stage="preflight")
                self._post_status(
                    blocked,
                    "I have a proof-blocked branch, but no Linear issue is attached to this Monica run, "
                    "so I stopped before retrying proof.",
                )
                return
            if not str(getattr(run, "linear_url", "") or "").strip():
                blocked = self.state.update_run(
                    run.id,
                    status="blocked",
                    failure_reason="linear_issue_url_missing_before_fix",
                )
                self._log_run("blocked", blocked, stage="preflight")
                self._post_status(
                    blocked,
                    "I have a proof-blocked branch, but the attached Linear issue URL is missing, "
                    "so I stopped before retrying proof.",
                )
                return
            if not _is_linear_issue_url(run.linear_url):
                blocked = self.state.update_run(
                    run.id,
                    status="blocked",
                    failure_reason="linear_issue_url_invalid_before_fix",
                )
                self._log_run("blocked", blocked, stage="preflight")
                self._post_status(
                    blocked,
                    "I have a proof-blocked branch, but the attached Linear issue URL is not a Linear issue link, "
                    "so I stopped before retrying proof.",
                )
                return
            approval_block_reason = _approved_pr_code_approval_block_reason(run, self.config)
            if approval_block_reason:
                blocked = self.state.update_run(
                    run.id,
                    status="blocked",
                    failure_reason=approval_block_reason,
                )
                self._log_run("blocked", blocked, stage="preflight")
                self._post_status(
                    blocked,
                    "I have a proof-blocked branch, but Monica approved-PR work still requires "
                    "explicit configured approver approval, so I stopped before retrying proof.",
                )
                return
            base_metadata_block_reason = _approved_pr_base_metadata_block_reason(
                {
                    "base_ref": str(getattr(run, "base_branch", "") or ""),
                    "base_commit": str(getattr(run, "base_commit", "") or ""),
                },
                self.config,
            )
            if base_metadata_block_reason:
                blocked = self.state.update_run(
                    run.id,
                    status="blocked",
                    failure_reason=base_metadata_block_reason,
                )
                self._log_run("blocked", blocked, stage="preflight")
                self._post_status(
                    blocked,
                    "I cannot retry proof because this Monica run is missing fresh mobile "
                    "base commit metadata from the configured default branch.",
                )
                return
            intake_block_reason = _approved_pr_slack_intake_block_reason(run, self.config)
            if intake_block_reason:
                blocked = self.state.update_run(
                    run.id,
                    status="blocked",
                    failure_reason=intake_block_reason,
                )
                self._log_run("blocked", blocked, stage="preflight")
                self._post_status(
                    blocked,
                    "I have a proof-blocked branch, but Monica approved-PR proof retry "
                    "must come from a tagged Slack message or direct message. Tagged channel "
                    "messages must come from an allowed Slack channel, so I stopped before "
                    "retrying proof.",
                )
                return

        worker_result = {
            "branch_name": branch_name,
            "worktree_path": str(worktree_path),
            "base_ref": str(getattr(run, "base_branch", "") or ""),
            "base_commit": str(getattr(run, "base_commit", "") or ""),
            "proof_deep_link": str(getattr(run, "proof_deep_link", "") or ""),
            "proof_expected_text": str(getattr(run, "proof_expected_text", "") or ""),
            "proof_screen": str(getattr(run, "proof_screen", "") or ""),
            "proof_retry": True,
            "changed": True,
            "slack_permalink": self._run_permalink(run),
            "summary": "Resuming Monica from the existing proof-blocked branch.",
            "evidence": [],
        }
        self._run_post_worker_gates(run, worker_result)

    def _run_post_worker_gates(self, run: Any, worker_result: dict[str, Any]) -> None:
        run = self.state.update_run(run.id, status="verifying")
        verification = self.skills.run_verification(run, worker_result)
        if self._is_cancelled(run.id):
            return
        if not verification.get("passed"):
            blocked = self.state.update_run(
                run.id,
                status="blocked",
                failure_reason=f"verification_failed: {verification.get('summary', '')}".strip(),
            )
            self._log_run("blocked", blocked, stage="verifying")
            self._post_status(run, f"Verification failed, so I did not open a PR.\n{verification.get('summary', '')}")
            return

        verification_block_reason = _approved_pr_verification_block_reason(verification, self.config)
        if verification_block_reason:
            blocked = self.state.update_run(
                run.id,
                status="blocked",
                failure_reason=verification_block_reason,
            )
            self._log_run("blocked", blocked, stage="verifying")
            self._post_status(
                blocked,
                "Verification did not report the configured verification command evidence, "
                "so I did not run proof or open a PR.",
            )
            return

        if self._proof_required():
            proof_config_block_reason = self._proof_config_block_reason(worker_result)
            if proof_config_block_reason:
                blocked = self.state.update_run(
                    run.id,
                    status="proof_blocked",
                    failure_reason=f"proof_unavailable: {proof_config_block_reason}".strip(),
                )
                self._log_run("proof_blocked", blocked, stage="proofing")
                self._post_status(
                    blocked,
                    "Verification passed, but simulator proof is unavailable, "
                    "so I did not mark this run done or open a PR.\n"
                    f"{proof_config_block_reason}",
                )
                return
            run = self.state.update_run(run.id, status="proofing")
            try:
                proof = self.skills.run_proof(run, worker_result, verification)
            except Exception as exc:
                if self._is_cancelled(run.id):
                    return
                detail = str(exc).strip() or exc.__class__.__name__
                blocked = self.state.update_run(
                    run.id,
                    status="proof_blocked",
                    failure_reason=f"proof_unavailable: {detail}",
                )
                self._log_run("proof_blocked", blocked, stage="proofing")
                self._post_status(
                    blocked,
                    "Verification passed, but simulator proof is unavailable, "
                    "so I did not mark this run done or open a PR.\n"
                    f"{detail}",
                )
                return
            if self._is_cancelled(run.id):
                return
            proof_block_reason = self._proof_block_reason(
                proof,
                worker_result=worker_result,
                run=run,
            )
            if proof_block_reason:
                blocked = self.state.update_run(
                    run.id,
                    status="proof_blocked",
                    failure_reason=f"proof_unavailable: {proof_block_reason}".strip(),
                )
                self._log_run("proof_blocked", blocked, stage="proofing")
                self._post_status(
                    blocked,
                    "Verification passed, but simulator proof is unavailable, "
                    "so I did not mark this run done or open a PR.\n"
                    f"{proof_block_reason}",
                )
                return
            if self.config.rollout_mode == "approved_pr":
                proof = self._share_proof_artifacts(run, proof)
                proof_share_block_reason = self._proof_share_block_reason(proof)
                if proof_share_block_reason:
                    blocked = self.state.update_run(
                        run.id,
                        status="proof_blocked",
                        failure_reason=f"proof_unavailable: {proof_share_block_reason}".strip(),
                    )
                    self._log_run("proof_blocked", blocked, stage="proofing")
                    self._post_status(
                        blocked,
                        "Verification and simulator proof passed, but reviewer-visible proof links "
                        "are unavailable, so I did not open a PR.\n"
                        f"{proof_share_block_reason}",
                    )
                    return
            worker_result["proof"] = dict(proof)

        if self.config.rollout_mode == "local_fix_only":
            completed = self.state.update_run(run.id, status="done")
            self._log_run("done", completed, stage="local_fix_only")
            proof_text = self._proof_status_text(worker_result.get("proof"))
            self._post_status(
                completed,
                "Local fix is ready on branch "
                f"`{completed.branch_name}`. Verification passed. "
                "The branch was not pushed and no PR was opened."
                f"{proof_text}",
            )
            return

        run = self.state.update_run(run.id, status="opening_pr")
        try:
            pr = self.skills.open_draft_pr(run, worker_result, verification)
        except Exception as exc:
            proof_publish_block_reason = _proof_publish_block_reason(exc)
            if proof_publish_block_reason:
                blocked = self.state.update_run(
                    run.id,
                    status="proof_blocked",
                    failure_reason=f"proof_unavailable: {proof_publish_block_reason}".strip(),
                )
                self._log_run("proof_blocked", blocked, stage="opening_pr")
                self._post_status(
                    blocked,
                    "Verification and simulator proof passed, but final proof validation failed, "
                    "so I did not open a PR.\n"
                    f"{proof_publish_block_reason}",
                )
                return
            raise
        pr_url = str(pr.get("url") or "")
        if not pr_url:
            blocked = self.state.update_run(
                run.id,
                status="blocked",
                failure_reason="draft_pr_url_missing",
            )
            self._log_run("blocked", blocked, stage="opening_pr")
            self._post_status(
                blocked,
                "I opened the final PR stage, but the publisher did not return a draft PR URL, "
                "so I stopped before marking this run complete.",
            )
            return
        if not _is_valid_pull_request_url(pr_url):
            blocked = self.state.update_run(
                run.id,
                status="blocked",
                failure_reason="draft_pr_url_invalid",
            )
            self._log_run("blocked", blocked, stage="opening_pr")
            self._post_status(
                blocked,
                "I opened the final PR stage, but the publisher did not return a valid draft PR URL, "
                "so I stopped before marking this run complete.",
            )
            return
        if self._is_cancelled(run.id):
            if pr_url:
                cancelled = self.state.update_run(run.id, pr_url=pr_url)
                self._log_run("blocked", cancelled, stage="opening_pr")
            return
        pr_ready = self.state.update_run(run.id, pr_url=pr_url)
        proof_text = self._proof_status_text(worker_result.get("proof"))
        if not self._post_status(
            pr_ready,
            _final_pr_status_text(pr_ready, pr_url, verification, proof_text),
        ):
            blocked = self.state.update_run(
                run.id,
                status="blocked",
                failure_reason="final_slack_update_failed",
            )
            self._log_run("blocked", blocked, stage="opening_pr")
            self._post_status(
                blocked,
                "Draft PR was created, but Monica could not post the final Slack update. "
                "I kept the PR URL on the run and did not mark this run done.",
            )
            return
        completed = self.state.update_run(run.id, status="done")
        self._log_run("done", completed, stage="opening_pr")

    @staticmethod
    def _run_permalink(run: Any) -> str:
        raw_event = getattr(run, "raw_event", None)
        if isinstance(raw_event, dict):
            return str(raw_event.get("permalink") or "").strip()
        return ""

    def _is_cancelled(self, run_id: str) -> bool:
        run = self.state.get_run(run_id)
        return bool(
            run
            and run.status == "blocked"
            and str(run.failure_reason or "").startswith("cancelled by ")
        )

    def _post_status(self, run: Any, text: str) -> bool:
        poster = getattr(self.skills, "post_status", None)
        if callable(poster):
            try:
                result = poster(run, text)
                return result is not False
            except Exception:
                return False
        return False

    def _share_proof_artifacts(self, run: Any, proof: dict[str, Any]) -> dict[str, Any]:
        sharer = getattr(self.skills, "share_proof_artifacts", None)
        result = deepcopy(proof)
        result["shareable_artifacts"] = []
        if not callable(sharer):
            result.setdefault("share_errors", []).append("proof artifact sharer is unavailable")
            return result
        try:
            shared = sharer(run, deepcopy(proof))
        except Exception as exc:
            result.setdefault("share_errors", []).append(str(exc) or exc.__class__.__name__)
            return result
        if isinstance(shared, dict):
            result["shareable_artifacts"] = shared.get("shareable_artifacts") or []
            if "share_errors" in shared:
                result["share_errors"] = shared["share_errors"]
        return result

    def _proof_required(self) -> bool:
        return self.config.rollout_mode == "approved_pr" or bool(self.config.proof.required_for_done)

    def _proof_config_block_reason(self, worker_result: dict[str, Any]) -> str:
        if self.config.rollout_mode != "approved_pr":
            return ""
        if not any(command.strip() for command in self.config.proof.commands):
            return "proof.commands is empty"
        if not any(command.strip() for command in self.config.proof.setup_commands):
            return "proof.setup_commands is empty"
        if _placeholder_proof_setup_commands(self.config.proof.setup_commands):
            return "proof.setup_commands contains a placeholder"
        if _placeholder_proof_commands(self.config.proof.commands):
            return "proof.commands contains a placeholder"
        secret_setup_assignments = _inline_secret_env_assignments(self.config.proof.setup_commands)
        if secret_setup_assignments:
            return (
                "proof.setup_commands must not inline secret env assignment(s): "
                f"{', '.join(secret_setup_assignments)}"
            )
        secret_proof_assignments = _inline_secret_env_assignments(self.config.proof.commands)
        if secret_proof_assignments:
            return (
                "proof.commands must not inline secret env assignment(s): "
                f"{', '.join(secret_proof_assignments)}"
            )
        if _noop_only_proof_setup_commands(self.config.proof.setup_commands):
            return "proof.setup_commands contains only no-op commands"
        if _noop_only_proof_commands(self.config.proof.commands):
            return "proof.commands contains only no-op commands"
        if not any(key.strip() for key in self.config.proof.required_env_keys):
            return "proof.required_env_keys is empty"
        invalid_required_env_keys = _invalid_required_env_keys(
            self.config.proof.required_env_keys
        )
        if invalid_required_env_keys:
            return (
                "proof.required_env_keys contains invalid key names: "
                f"{', '.join(invalid_required_env_keys)}"
            )
        env = dict(os.environ)
        env.update(_profile_env_values())
        setup_value_keys = _required_env_value_keys_in_commands(
            self.config.proof.setup_commands,
            env=env,
            required_env_keys=self.config.proof.required_env_keys,
        )
        if setup_value_keys:
            return (
                "proof.setup_commands must not include literal values from "
                f"proof.required_env_keys: {', '.join(setup_value_keys)}"
            )
        proof_value_keys = _required_env_value_keys_in_commands(
            self.config.proof.commands,
            env=env,
            required_env_keys=self.config.proof.required_env_keys,
        )
        if proof_value_keys:
            return (
                "proof.commands must not include literal values from "
                f"proof.required_env_keys: {', '.join(proof_value_keys)}"
            )
        missing_platforms = _missing_approved_pr_proof_platforms(self.config.proof.platform_order)
        if missing_platforms:
            return (
                "proof.platform_order must include both ios and android: "
                f"missing {', '.join(missing_platforms)}"
            )
        if not _worker_proof_target_deep_link(worker_result):
            return "missing proof target deep link"
        expected_text_block_reason = _proof_expected_text_block_reason(
            _worker_proof_target_expected_text_value(worker_result)
        )
        if expected_text_block_reason:
            return expected_text_block_reason
        generic_target_reason = _generic_proof_target_block_reason(
            _worker_proof_target_deep_link(worker_result)
        )
        if generic_target_reason:
            return generic_target_reason
        return ""

    @staticmethod
    def _proof_artifacts(proof: dict[str, Any]) -> list[str]:
        return [str(path).strip() for path in proof.get("artifacts") or [] if str(path).strip()]

    def _proof_block_reason(
        self,
        proof: dict[str, Any],
        *,
        worker_result: dict[str, Any] | None = None,
        run: Any | None = None,
    ) -> str:
        if not isinstance(proof, dict):
            return "Proof unavailable."
        summary = str(proof.get("summary") or "Proof unavailable.").strip()
        artifacts = self._proof_artifacts(proof)
        if not proof.get("passed") or not artifacts:
            return summary
        if self.config.rollout_mode == "approved_pr" and not _proof_target_deep_link(proof):
            return "missing proof target deep link"
        target_mismatch = self._proof_target_mismatch_reason(proof, worker_result or {})
        if target_mismatch:
            return target_mismatch
        if self.config.rollout_mode == "approved_pr":
            expected_text_block_reason = _proof_expected_text_block_reason(
                _proof_target_expected_text_value(proof)
            )
            if expected_text_block_reason:
                return expected_text_block_reason
        command_mismatch = self._proof_command_mismatch_reason(proof)
        if command_mismatch:
            return command_mismatch
        missing_platforms = self._missing_required_proof_platforms(artifacts)
        if missing_platforms:
            return f"missing required platform artifacts: {', '.join(missing_platforms)}"
        missing_files = self._missing_required_proof_files(artifacts)
        if missing_files:
            return f"missing proof artifact files: {', '.join(missing_files)}"
        if self.config.rollout_mode == "approved_pr":
            auth_fallback_platforms = _auth_fallback_platforms(
                artifacts=tuple(artifacts),
                platforms=APPROVED_PR_REQUIRED_PROOF_PLATFORMS,
            )
            if auth_fallback_platforms:
                return (
                    "auth/onboarding proof fallback observed for: "
                    f"{', '.join(auth_fallback_platforms)}"
                )
            non_target_screen_platforms = _non_target_screen_platforms(
                artifacts=tuple(artifacts),
                platforms=APPROVED_PR_REQUIRED_PROOF_PLATFORMS,
            )
            if non_target_screen_platforms:
                return (
                    "non-target app screen observed for: "
                    f"{', '.join(non_target_screen_platforms)}"
                )
            missing_target_route = _missing_target_route_platforms(
                artifacts=tuple(artifacts),
                platforms=APPROVED_PR_REQUIRED_PROOF_PLATFORMS,
            )
            if missing_target_route:
                return (
                    "missing proof target route evidence artifacts: "
                    f"{', '.join(missing_target_route)}"
                )
            proof_target = proof.get("proof_target")
            mismatched_target_route = _mismatched_target_route_platforms(
                artifacts=tuple(artifacts),
                platforms=APPROVED_PR_REQUIRED_PROOF_PLATFORMS,
                proof_target=proof_target if isinstance(proof_target, dict) else {},
            )
            if mismatched_target_route:
                return (
                    "proof target route does not match proof target for: "
                    f"{', '.join(mismatched_target_route)}"
                )
            manifest = _proof_manifest_artifact(artifacts)
            if not manifest:
                return "missing proof manifest artifact"
            if not Path(manifest).is_file():
                return "missing proof manifest artifact file"
            manifest_block_reason = _proof_manifest_block_reason(
                manifest,
                worker_result or {},
                proof,
                run=run,
            )
            if manifest_block_reason:
                return manifest_block_reason
            stale_artifacts = _proof_artifacts_outside_manifest_dir(
                artifacts,
                manifest=manifest,
            )
            if stale_artifacts:
                return (
                    "proof artifacts must stay under proof manifest directory: "
                    f"{', '.join(stale_artifacts)}"
                )
            missing_target_evidence = _missing_target_text_evidence_platforms(
                artifacts=artifacts,
                expected_text=_proof_target_expected_text(proof),
            )
            if missing_target_evidence:
                return (
                    "missing proof target evidence artifacts: "
                    f"{', '.join(missing_target_evidence)}"
                )
        return ""

    def _proof_target_mismatch_reason(self, proof: dict[str, Any], worker_result: dict[str, Any]) -> str:
        if self.config.rollout_mode != "approved_pr":
            return ""
        expected_deep_link = _worker_proof_target_deep_link(
            worker_result,
            config_deep_link=self.config.proof.deep_link,
        )
        actual_deep_link = _proof_target_deep_link(proof)
        if expected_deep_link and actual_deep_link != expected_deep_link:
            return "proof target does not match requested deep link"
        expected_text = _worker_proof_target_expected_text(worker_result)
        actual_text = _proof_target_expected_text(proof)
        if expected_text and actual_text != expected_text:
            return "proof target does not match requested expected text"
        expected_screen = _worker_proof_target_screen(worker_result)
        actual_screen = _proof_target_screen(proof)
        if expected_screen and actual_screen != expected_screen:
            return "proof target does not match requested screen"
        return ""

    def _proof_command_mismatch_reason(self, proof: dict[str, Any]) -> str:
        if self.config.rollout_mode != "approved_pr":
            return ""
        configured_setup = _normalized_command_list(self.config.proof.setup_commands)
        if configured_setup and _normalized_command_list(proof.get("setup_commands")) != configured_setup:
            return "proof setup commands do not match configured proof setup commands"
        configured_commands = _normalized_command_list(self.config.proof.commands)
        if configured_commands and _normalized_command_list(proof.get("commands")) != configured_commands:
            return "proof commands do not match configured proof commands"
        configured_required_env_keys = _normalized_required_env_key_list(
            self.config.proof.required_env_keys
        )
        if (
            configured_required_env_keys
            and _normalized_required_env_key_list(proof.get("required_env_keys"))
            != configured_required_env_keys
        ):
            return "proof required env keys do not match configured proof required env keys"
        return ""

    def _missing_required_proof_platforms(self, artifacts: list[str]) -> list[str]:
        if self.config.rollout_mode != "approved_pr":
            return []
        visual_artifacts = [artifact for artifact in artifacts if _is_visual_proof_artifact(artifact)]
        missing: list[str] = []
        used_artifacts: set[str] = set()
        for platform in APPROVED_PR_REQUIRED_PROOF_PLATFORMS:
            matched = ""
            for artifact in visual_artifacts:
                artifact_key = _proof_path_key(artifact)
                if artifact_key in used_artifacts:
                    continue
                if _artifact_matches_platform(artifact, platform):
                    matched = artifact_key
                    break
            if matched:
                used_artifacts.add(matched)
            else:
                missing.append(platform)
        return missing

    def _missing_required_proof_files(self, artifacts: list[str]) -> list[str]:
        if self.config.rollout_mode != "approved_pr":
            return []
        visual_artifacts = [artifact for artifact in artifacts if _is_visual_proof_artifact(artifact)]
        missing: list[str] = []
        for platform in APPROVED_PR_REQUIRED_PROOF_PLATFORMS:
            platform_artifacts = [
                artifact
                for artifact in visual_artifacts
                if _artifact_matches_platform(artifact, platform)
            ]
            if platform_artifacts and not any(_proof_artifact_has_bytes(artifact) for artifact in platform_artifacts):
                missing.append(platform)
        return missing

    def _proof_share_block_reason(self, proof: dict[str, Any]) -> str:
        if self.config.rollout_mode != "approved_pr":
            return ""
        missing = _missing_shareable_platforms(proof.get("shareable_artifacts"))
        if missing:
            return _with_share_errors(
                f"missing shareable proof links: {', '.join(missing)}",
                proof.get("share_errors"),
            )
        mismatched = _shareable_platforms_without_local_artifact_match(
            proof.get("shareable_artifacts"),
            artifacts=self._proof_artifacts(proof),
        )
        if mismatched:
            return _with_share_errors(
                f"shareable proof links do not match local artifacts: {', '.join(mismatched)}",
                proof.get("share_errors"),
            )
        duplicates = _shareable_platforms_with_duplicate_urls(proof.get("shareable_artifacts"))
        if duplicates:
            return _with_share_errors(
                f"duplicate shareable proof links: {', '.join(duplicates)}",
                proof.get("share_errors"),
            )
        return ""

    def _proof_status_text(self, proof: object) -> str:
        if not isinstance(proof, dict):
            return ""
        target_text = _proof_target_status_text(proof)
        shareable = _shareable_proof_refs(proof.get("shareable_artifacts"))
        if shareable:
            visible = ", ".join(_shareable_ref_text(item) for item in shareable[:5])
            suffix = "" if len(shareable) <= 5 else f", +{len(shareable) - 5} more"
            return f"{target_text}\nProof: {visible}{suffix}"
        artifacts = self._proof_artifacts(proof)
        if not artifacts:
            return ""
        visible = ", ".join(artifacts[:5])
        suffix = "" if len(artifacts) <= 5 else f", +{len(artifacts) - 5} more"
        return f"{target_text}\nProof captured: {visible}{suffix}"

    def _is_expected_worker_branch(self, *, run: Any, branch_name: str) -> bool:
        branch = str(branch_name or "").strip()
        if not branch:
            return False
        prefix = str(self.config.repo.branch_prefix or "").strip().rstrip("/")
        if prefix and not branch.startswith(f"{prefix}/"):
            return False
        if self.config.rollout_mode == "approved_pr":
            issue_keys = _run_linear_issue_keys(run)
            branch_key = branch.casefold()
            return bool(issue_keys) and all(key.casefold() in branch_key for key in issue_keys)
        linear_identifier = str(getattr(run, "linear_identifier", "") or "").strip()
        return not linear_identifier or linear_identifier in branch

    def _mark_failed(self, run_id: str, exc: Exception) -> None:
        run = self.state.get_run(run_id)
        if run is None:
            raise KeyError(run_id) from exc
        stage = run.status or "unknown"
        detail = str(exc) or exc.__class__.__name__
        failed = self.state.update_run(
            run_id,
            status="failed",
            failure_reason=f"{stage}_failed: {detail}",
        )
        self._log_run("failed", failed, stage=stage)
        self._post_status(
            failed,
            "I hit a problem while working this Monica run, so I stopped before taking the next action.\n"
            f"Stage: {stage}\n"
            f"Check Monica logs or `hermes mobile-bug-agent show {failed.id}` on the host for details.",
        )

    @staticmethod
    def _log_run(event: str, run: Any, *, stage: str = "") -> None:
        logger.info(
            "monica_run event=%s run_id=%s status=%s stage=%s channel_id=%s thread_ts=%s "
            "linear_identifier=%s linear_url=%s branch_name=%s pr_url=%s failure_reason=%s",
            event,
            getattr(run, "id", ""),
            getattr(run, "status", ""),
            stage,
            getattr(run, "channel_id", ""),
            getattr(run, "thread_ts", ""),
            getattr(run, "linear_identifier", ""),
            getattr(run, "linear_url", ""),
            getattr(run, "branch_name", ""),
            getattr(run, "pr_url", ""),
            getattr(run, "failure_reason", ""),
        )

    @staticmethod
    def _clarification_text(intent: dict[str, Any]) -> str:
        questions = intent.get("missing_questions") or []
        if isinstance(questions, str):
            questions = [questions]
        clean = [str(question).strip() for question in questions if str(question).strip()]
        if not clean:
            return "I need a little more context before I file this as a mobile bug."
        return "I need a little more context before I file this:\n" + "\n".join(f"- {q}" for q in clean)

    @staticmethod
    def _linear_done_text(issue: dict[str, Any]) -> str:
        if issue.get("dry_run"):
            preview = _compact(str(issue.get("description") or ""), limit=500)
            if preview:
                return (
                    f"Dry run: I would create a Linear issue titled `{issue.get('title', 'Mobile bug')}`.\n"
                    f"Preview:\n{preview}"
                )
            return f"Dry run: I would create a Linear issue titled `{issue.get('title', 'Mobile bug')}`."
        if issue.get("url"):
            return f"Created Linear issue: {issue.get('url')}"
        return "Created the Linear issue."


def _compact(value: str, *, limit: int) -> str:
    compacted = "\n".join(line.rstrip() for line in value.strip().splitlines() if line.strip())
    if len(compacted) <= limit:
        return compacted
    return compacted[: max(0, limit - 3)].rstrip() + "..."


def _worktree_current_branch(worktree_path: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(worktree_path), "branch", "--show-current"],
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    return str(proc.stdout or "").strip()


def _proof_publish_block_reason(exc: Exception) -> str:
    detail = str(exc or "").strip() or exc.__class__.__name__
    normalized = detail.casefold()
    proof_markers = (
        "proof",
        "shareable",
        "screenshot",
        "recording",
        "simulator",
    )
    if not any(marker in normalized for marker in proof_markers):
        return ""
    return detail


def _is_visual_proof_artifact(artifact: str) -> bool:
    return Path(artifact).suffix.lower() in VISUAL_PROOF_SUFFIXES


def _is_text_proof_artifact(artifact: str) -> bool:
    return Path(artifact).suffix.lower() in TEXT_PROOF_SUFFIXES


def _proof_artifact_has_bytes(artifact: str) -> bool:
    path = Path(artifact)
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _proof_artifacts_outside_manifest_dir(
    artifacts: list[str],
    *,
    manifest: str,
) -> list[str]:
    manifest_path = Path(manifest)
    manifest_dir = manifest_path.parent
    manifest_key = _proof_path_key(manifest)
    outside: list[str] = []
    for artifact in artifacts:
        artifact_text = str(artifact or "").strip()
        if not artifact_text or _proof_path_key(artifact_text) == manifest_key:
            continue
        artifact_path = Path(artifact_text).expanduser()
        if not artifact_path.is_absolute():
            artifact_path = manifest_dir / artifact_path
        if _path_is_under_dir(artifact_path, manifest_dir):
            continue
        label = Path(artifact_text).name or artifact_text
        if label not in outside:
            outside.append(label)
    return outside


def _path_is_under_dir(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _artifact_matches_platform(artifact: str, platform: str) -> bool:
    path = Path(artifact)
    haystack = " ".join((path.name, path.stem, *path.parts[-3:])).lower()
    if platform == "ios":
        return any(token in haystack for token in ("ios", "iphone", "ipad"))
    return platform in haystack


def _missing_target_text_evidence_platforms(*, artifacts: list[str], expected_text: str) -> list[str]:
    expected = _normalize_text_for_match(expected_text)
    if not expected:
        return []
    text_artifacts = [
        artifact
        for artifact in artifacts
        if _is_text_proof_artifact(artifact) and Path(artifact).name != PROOF_MANIFEST_NAME
    ]
    missing: list[str] = []
    used_artifacts: set[str] = set()
    for platform in APPROVED_PR_REQUIRED_PROOF_PLATFORMS:
        matched = ""
        for artifact in text_artifacts:
            artifact_key = _proof_path_key(artifact)
            if artifact_key in used_artifacts:
                continue
            if not _artifact_matches_platform(artifact, platform):
                continue
            if expected in _normalize_text_for_match(_read_text_proof_artifact(artifact)):
                matched = artifact_key
                break
        if matched:
            used_artifacts.add(matched)
        else:
            missing.append(platform)
    return missing


def _read_text_proof_artifact(artifact: str) -> str:
    try:
        return Path(artifact).read_bytes()[:TEXT_PROOF_READ_LIMIT_BYTES].decode(
            "utf-8",
            errors="replace",
        )
    except Exception:
        return ""


def _proof_manifest_artifact(artifacts: list[str]) -> str:
    for artifact in artifacts:
        path = Path(artifact)
        if path.name == PROOF_MANIFEST_NAME:
            return str(path)
    return ""


def _proof_manifest_block_reason(
    manifest: str,
    worker_result: dict[str, Any],
    proof: dict[str, Any],
    *,
    run: Any | None = None,
) -> str:
    expected_commit = str(worker_result.get("base_commit") or "").strip()
    expected_ref = str(worker_result.get("base_ref") or worker_result.get("base_branch") or "").strip()
    try:
        payload = json.loads(Path(manifest).read_text(encoding="utf-8"))
    except Exception:
        return "proof manifest is not readable"
    if not isinstance(payload, dict):
        return "proof manifest is not readable"
    expected_run_id = str(getattr(run, "id", "") or "").strip()
    actual_run_id = str(payload.get("run_id") or "").strip()
    if expected_run_id and actual_run_id != expected_run_id:
        return "proof manifest run id does not match Monica run"
    actual_commit = str(payload.get("base_commit") or "").strip()
    if expected_commit and actual_commit != expected_commit:
        return "proof manifest base commit does not match worker base commit"
    actual_ref = str(payload.get("base_ref") or payload.get("base_branch") or "").strip()
    if expected_ref and actual_ref != expected_ref:
        return "proof manifest base ref does not match worker base ref"
    expected_linear_identifier = str(getattr(run, "linear_identifier", "") or "").strip()
    actual_linear_identifier = str(payload.get("linear_identifier") or "").strip()
    if (
        expected_linear_identifier
        and actual_linear_identifier != expected_linear_identifier
    ):
        return "proof manifest Linear issue does not match Monica run"
    expected_linear_url = str(getattr(run, "linear_url", "") or "").strip()
    actual_linear_url = str(payload.get("linear_url") or "").strip()
    if expected_linear_url and actual_linear_url != expected_linear_url:
        return "proof manifest Linear issue does not match Monica run"
    manifest_target = payload.get("proof_target")
    if not isinstance(manifest_target, dict):
        return "proof manifest target does not match proof target"
    if _normalized_proof_target(manifest_target) != _normalized_proof_target(
        proof.get("proof_target") if isinstance(proof, dict) else {}
    ):
        return "proof manifest target does not match proof target"
    if _normalized_command_list(payload.get("setup_commands")) != _normalized_command_list(
        proof.get("setup_commands") if isinstance(proof, dict) else ()
    ):
        return "proof manifest setup commands do not match proof setup commands"
    if _normalized_command_list(payload.get("commands")) != _normalized_command_list(
        proof.get("commands") if isinstance(proof, dict) else ()
    ):
        return "proof manifest commands do not match proof commands"
    invalid_proof_required_env_keys = _invalid_required_env_keys(
        tuple(_normalized_required_env_key_list(proof.get("required_env_keys") if isinstance(proof, dict) else ()))
    )
    invalid_manifest_required_env_keys = _invalid_required_env_keys(
        tuple(_normalized_required_env_key_list(payload.get("required_env_keys")))
    )
    if invalid_manifest_required_env_keys and not invalid_proof_required_env_keys:
        return (
            "proof manifest required env keys are invalid: "
            f"{', '.join(invalid_manifest_required_env_keys)}"
        )
    if _normalized_required_env_key_list(payload.get("required_env_keys")) != _normalized_required_env_key_list(
        proof.get("required_env_keys") if isinstance(proof, dict) else ()
    ):
        return "proof manifest required env keys do not match proof required env keys"
    if _normalized_platform_list(payload.get("platforms")) != _normalized_platform_list(
        proof.get("platforms") if isinstance(proof, dict) else ()
    ):
        return "proof manifest platforms do not match proof platforms"
    if _normalized_artifact_list(payload.get("proof_artifacts")) != _normalized_artifact_list(
        _proof_artifacts_without_manifest(proof.get("artifacts") if isinstance(proof, dict) else ())
    ):
        return "proof manifest artifacts do not match proof artifacts"
    expected_branch = str(worker_result.get("branch_name") or "").strip()
    actual_branch = str(payload.get("branch_name") or "").strip()
    if expected_branch and actual_branch != expected_branch:
        return "proof manifest branch does not match worker branch"
    expected_worktree = str(worker_result.get("worktree_path") or worker_result.get("worktree") or "").strip()
    actual_worktree = str(payload.get("worktree") or payload.get("worktree_path") or "").strip()
    if expected_worktree and actual_worktree != expected_worktree:
        return "proof manifest worktree does not match worker worktree"
    return ""


def _normalized_proof_target(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        "deep_link": str(value.get("deep_link") or "").strip(),
        "expected_text": " ".join(str(value.get("expected_text") or "").split()),
        "screen": " ".join(str(value.get("screen") or "").split()),
    }


def _normalize_text_for_match(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _normalized_command_list(value: Any) -> list[str]:
    if isinstance(value, str):
        candidates = (value,)
    elif isinstance(value, (list, tuple)):
        candidates = value
    else:
        return []
    return [_normalized_command_text(item) for item in candidates if str(item).strip()]


def _normalized_required_env_key_list(value: Any) -> list[str]:
    if isinstance(value, str):
        candidates = (value,)
    elif isinstance(value, (list, tuple)):
        candidates = value
    else:
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        key = str(item or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        normalized.append(key)
    return normalized


def _normalized_platform_list(value: Any) -> list[str]:
    if isinstance(value, str):
        candidates = (value,)
    elif isinstance(value, (list, tuple)):
        candidates = value
    else:
        return []
    return sorted({str(item or "").strip().lower() for item in candidates if str(item).strip()})


def _missing_approved_pr_proof_platforms(value: Any) -> list[str]:
    if isinstance(value, str):
        candidates = (value,)
    elif isinstance(value, (list, tuple)):
        candidates = value
    else:
        candidates = ()
    present = {_normalize_proof_platform(item) for item in candidates}
    return [platform for platform in APPROVED_PR_REQUIRED_PROOF_PLATFORMS if platform not in present]


def _normalize_proof_platform(value: Any) -> str:
    platform = str(value or "").strip().lower()
    if platform in {"iphone", "ipad", "ios-simulator"}:
        return "ios"
    if platform == "android-emulator":
        return "android"
    return platform


def _proof_artifacts_without_manifest(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [
        str(item).strip()
        for item in value
        if str(item).strip() and Path(str(item).strip()).name != PROOF_MANIFEST_NAME
    ]


def _normalized_artifact_list(value: Any) -> list[str]:
    if isinstance(value, str):
        candidates = (value,)
    elif isinstance(value, (list, tuple)):
        candidates = value
    else:
        return []
    return sorted({_proof_path_key(str(item)) for item in candidates if str(item).strip()})


def _is_approved_pr_fix_scope(run: Any, *, intent: dict[str, Any] | None = None) -> bool:
    intent = intent or {}
    request_text = _normalized_scope_text(str(getattr(run, "request_text", "") or ""))
    text = _normalized_scope_text(
        "\n".join(
            str(value or "")
            for value in (
                request_text,
                getattr(run, "intent", ""),
                intent.get("summary", ""),
                intent.get("observed_behavior", ""),
                intent.get("expected_behavior", ""),
                intent.get("reason", ""),
            )
        )
    )
    has_surface = any(term in text for term in APPROVED_PR_FIX_SURFACE_TERMS)
    has_kind = any(term in text for term in APPROVED_PR_FIX_KIND_TERMS)
    has_excluded_kind = _has_approved_pr_excluded_term(text)
    return has_surface and has_kind and not has_excluded_kind


def _has_approved_pr_excluded_term(text: str) -> bool:
    normalized = _normalized_scope_text(text)
    for term in APPROVED_PR_FIX_EXCLUDED_TERMS:
        if term == "crash":
            if re.search(r"\bcrash(?:es|ed|ing)?\b", normalized):
                return True
            continue
        if re.search(rf"\b{re.escape(term)}\b", normalized):
            return True
    return False


def _approved_pr_code_approval_block_reason(run: Any, config: MonicaConfig) -> str:
    approved_by = str(getattr(run, "approved_by_user_id", "") or "").strip()
    if not approved_by:
        return "approved_pr_approval_missing"
    configured_approvers = {
        str(user_id).strip()
        for user_id in config.slack.approver_user_ids
        if str(user_id).strip()
    }
    if approved_by not in configured_approvers:
        return "approved_pr_approver_not_configured"
    return ""


def _approved_pr_slack_intake_block_reason(run: Any, config: MonicaConfig) -> str:
    configured_bot_ids = {
        str(user_id).strip()
        for user_id in config.slack.bot_user_ids
        if str(user_id).strip()
    }
    if str(getattr(run, "platform", "") or "").strip() != "slack":
        return "approved_pr_slack_intake_missing"
    raw_event = getattr(run, "raw_event", None)
    if not isinstance(raw_event, dict) or not raw_event:
        return "approved_pr_slack_intake_missing"
    if raw_event.get("monica_simulated"):
        return "approved_pr_slack_intake_missing"
    channel_type = str(raw_event.get("channel_type") or "").strip()
    if channel_type == "im":
        return ""
    if channel_type == "mpim":
        return "approved_pr_slack_intake_missing"
    channel_id = str(getattr(run, "channel_id", "") or raw_event.get("channel") or "").strip()
    if channel_id.startswith("D"):
        return ""
    allowed_channels = {
        str(channel).strip()
        for channel in config.slack.allowed_channels
        if str(channel).strip()
    }
    if allowed_channels and channel_id not in allowed_channels:
        return "approved_pr_slack_channel_not_allowed"
    raw_type = str(raw_event.get("type") or "").strip()
    mentioned_ids = set(_SLACK_MENTION_RE.findall(str(raw_event.get("text") or "")))
    if raw_type == "app_mention":
        if configured_bot_ids:
            return "" if configured_bot_ids & mentioned_ids else "approved_pr_slack_intake_missing"
        return "approved_pr_slack_intake_missing"
    if configured_bot_ids and configured_bot_ids & mentioned_ids:
        return ""
    return "approved_pr_slack_intake_missing"


def _approved_pr_base_metadata_block_reason(
    worker_result: dict[str, Any],
    config: MonicaConfig,
) -> str:
    if config.rollout_mode != "approved_pr":
        return ""
    base_ref = str(worker_result.get("base_ref") or worker_result.get("base_branch") or "").strip()
    base_commit = str(worker_result.get("base_commit") or "").strip()
    if not base_ref or not base_commit:
        return "approved_pr_base_metadata_missing"
    if not GIT_COMMIT_RE.fullmatch(base_commit):
        return "approved_pr_base_commit_invalid"
    expected_base_ref = configured_remote_base_ref(config.repo.default_branch)
    if base_ref != expected_base_ref:
        return "approved_pr_base_ref_mismatch"
    return ""


def _approved_pr_verification_block_reason(
    verification: dict[str, Any],
    config: MonicaConfig,
) -> str:
    if config.rollout_mode != "approved_pr":
        return ""
    configured = _normalized_command_set(config.verification.commands)
    reported = _normalized_command_set(verification.get("commands") or ())
    if not configured:
        return "verification_evidence_missing"
    if configured.issubset(reported):
        return ""
    output = _normalized_command_text(verification.get("output") or "")
    if not all(command in output for command in configured):
        return "verification_evidence_missing"
    return ""


def _normalized_command_set(values: Any) -> set[str]:
    if isinstance(values, str):
        candidates = (values,)
    elif isinstance(values, (list, tuple, set)):
        candidates = values
    else:
        return set()
    return {_normalized_command_text(value) for value in candidates if str(value).strip()}


def _normalized_command_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _with_share_errors(reason: str, value: Any) -> str:
    errors = _proof_share_errors(value)
    if not errors:
        return reason
    return f"{reason} (share errors: {'; '.join(errors)})"


def _proof_share_errors(value: Any) -> list[str]:
    if isinstance(value, str):
        candidates = (value,)
    elif isinstance(value, (list, tuple, set)):
        candidates = value
    else:
        return []
    errors: list[str] = []
    for item in candidates:
        text = " ".join(str(item or "").split())
        if not text:
            continue
        errors.append(text[:240])
        if len(errors) >= 3:
            break
    return errors


def _normalized_scope_text(value: str) -> str:
    return " ".join(str(value or "").lower().replace("_", " ").split())


def _shareable_proof_refs(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, (list, tuple)):
        return []
    refs: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        url = _safe_shareable_url(item.get("url") or item.get("permalink") or "")
        if not url:
            continue
        platform = str(item.get("platform") or "").strip().lower()
        path = str(item.get("path") or "").strip()
        if not platform and path:
            for candidate in APPROVED_PR_REQUIRED_PROOF_PLATFORMS:
                if _artifact_matches_platform(path, candidate):
                    platform = candidate
                    break
        refs.append(
            {
                "platform": platform,
                "path": path,
                "url": url,
                "title": str(item.get("title") or "").strip(),
            }
        )
    return refs


def _safe_shareable_url(value: Any) -> str:
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


def _is_valid_pull_request_url(value: Any) -> bool:
    text = _safe_shareable_url(value)
    if not text:
        return False
    parsed = urlparse(text)
    host = str(parsed.hostname or "").strip().lower().rstrip(".")
    return (
        host == "github.com"
        and bool(re.fullmatch(r"/[^/]+/[^/]+/pull/\d+/?", parsed.path))
    )


def _is_local_shareable_host(hostname: str | None) -> bool:
    host = str(hostname or "").strip().lower().rstrip(".")
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ip_address(host)
    except ValueError:
        return "." not in host or host.endswith(LOCAL_SHAREABLE_HOST_SUFFIXES)
    return (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_unspecified
    )


def _missing_shareable_platforms(value: Any) -> list[str]:
    refs = _shareable_proof_refs(value)
    missing: list[str] = []
    for platform in APPROVED_PR_REQUIRED_PROOF_PLATFORMS:
        if not any(_shareable_ref_matches_platform(ref, platform) for ref in refs):
            missing.append(platform)
    return missing


def _shareable_platforms_without_local_artifact_match(value: Any, *, artifacts: list[str]) -> list[str]:
    refs = _shareable_proof_refs(value)
    local_paths = _visual_artifact_paths_by_platform(artifacts)
    mismatched: list[str] = []
    for platform in APPROVED_PR_REQUIRED_PROOF_PLATFORMS:
        platform_paths = local_paths.get(platform, set())
        if not platform_paths:
            continue
        if not any(
            _shareable_ref_matches_local_artifact(ref, platform, platform_paths)
            for ref in refs
        ):
            mismatched.append(platform)
    return mismatched


def _shareable_platforms_with_duplicate_urls(value: Any) -> list[str]:
    refs = _shareable_proof_refs(value)
    urls_by_platform: dict[str, str] = {}
    duplicates: list[str] = []
    for platform in APPROVED_PR_REQUIRED_PROOF_PLATFORMS:
        url = ""
        for ref in refs:
            if _shareable_ref_matches_platform(ref, platform):
                url = str(ref.get("url") or "").strip()
                break
        if not url:
            continue
        if url in urls_by_platform.values():
            duplicates.append(platform)
        urls_by_platform[platform] = url
    return duplicates


def _shareable_ref_matches_platform(ref: dict[str, str], platform: str) -> bool:
    ref_platform = str(ref.get("platform") or "").strip().lower()
    if ref_platform == platform:
        return True
    return _artifact_matches_platform(str(ref.get("path") or ""), platform)


def _shareable_ref_matches_local_artifact(
    ref: dict[str, str],
    platform: str,
    local_paths: set[str],
) -> bool:
    path = _proof_path_key(str(ref.get("path") or ""))
    if not path or path not in local_paths:
        return False
    ref_platform = str(ref.get("platform") or "").strip().lower()
    return not ref_platform or ref_platform == platform


def _visual_artifact_paths_by_platform(artifacts: list[str]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {
        platform: set() for platform in APPROVED_PR_REQUIRED_PROOF_PLATFORMS
    }
    for artifact in artifacts:
        path = str(artifact or "").strip()
        if not path or not _is_visual_proof_artifact(path):
            continue
        for platform in APPROVED_PR_REQUIRED_PROOF_PLATFORMS:
            if _artifact_matches_platform(path, platform):
                result[platform].add(_proof_path_key(path))
    return result


def _proof_path_key(value: str) -> str:
    path = str(value or "").strip()
    return str(Path(path)) if path else ""


def _is_linear_issue_url(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    parsed = urlparse(text)
    if parsed.scheme.lower() not in {"http", "https"}:
        return False
    host = str(parsed.hostname or "").strip().lower().rstrip(".")
    path = str(parsed.path or "").strip("/")
    return (host == "linear.app" or host.endswith(".linear.app")) and bool(path)


def _run_linear_issue_keys(run: Any) -> tuple[str, ...]:
    keys: list[str] = []
    for value in (
        getattr(run, "linear_identifier", ""),
        getattr(run, "linear_url", ""),
    ):
        match = LINEAR_ISSUE_KEY_RE.search(str(value or "").upper())
        if not match:
            continue
        key = match.group(0)
        if key not in keys:
            keys.append(key)
    return tuple(keys)


def _shareable_ref_text(ref: dict[str, str]) -> str:
    platform = str(ref.get("platform") or "").strip()
    label = "iOS" if platform == "ios" else platform.title() if platform else "Proof"
    return f"{label}: {ref.get('url', '')}"


def _proof_required_env_keys(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    candidates = tuple(
        str(key or "").strip()
        for key in value
        if str(key or "").strip()
    )
    invalid = set(_invalid_required_env_keys(candidates))
    keys: list[str] = []
    for key in candidates:
        if "=" in key or key in invalid or key in keys:
            continue
        keys.append(key)
    return keys


def _proof_target_status_text(proof: dict[str, Any]) -> str:
    parts: list[str] = []
    deep_link = _proof_target_deep_link(proof)
    if deep_link:
        parts.append(f"Proof target: {deep_link}")
    target = proof.get("proof_target")
    if isinstance(target, dict):
        expected_text = _normalized_proof_expected_text(target.get("expected_text"))
        if expected_text:
            parts.append(f"expected text: {expected_text}")
        screen = _proof_target_screen(proof)
        if screen:
            parts.append(f"screen: {screen}")
    required_env_keys = _proof_required_env_keys(proof.get("required_env_keys"))
    if required_env_keys:
        parts.append(f"required env keys: {', '.join(required_env_keys)}")
    if not parts:
        return ""
    return "\n" + "; ".join(parts)


def _final_pr_status_text(run: Any, pr_url: str, verification: dict[str, Any], proof_text: str) -> str:
    lines = [f"Draft PR is ready: {pr_url}"]
    linear = str(getattr(run, "linear_url", "") or getattr(run, "linear_identifier", "") or "").strip()
    if linear:
        lines.append(f"Linear: {linear}")
    base_status = _run_base_status_text(run)
    if base_status:
        lines.append(base_status)
    verification_status = _verification_status_text(verification)
    if verification_status:
        lines.append(f"Verification: {verification_status}")
    text = "\n".join(lines)
    return f"{text}{proof_text}"


def _run_base_status_text(run: Any) -> str:
    base_ref = str(getattr(run, "base_branch", "") or "").strip()
    base_commit = str(getattr(run, "base_commit", "") or "").strip()
    if base_ref and base_commit:
        return f"Base: {base_ref} @ {base_commit}"
    if base_ref:
        return f"Base: {base_ref}"
    if base_commit:
        return f"Base commit: {base_commit}"
    return ""


def _verification_status_text(verification: dict[str, Any]) -> str:
    summary = str(verification.get("summary") or "").strip()
    commands = [
        " ".join(str(command).split())
        for command in verification.get("commands") or []
        if str(command).strip()
    ]
    if commands and summary.lower() in {"", "verification passed", "verification passed."}:
        return "; ".join(commands)
    return summary


def _proof_target_deep_link(proof: dict[str, Any]) -> str:
    target = proof.get("proof_target")
    if not isinstance(target, dict):
        return ""
    value = str(target.get("deep_link") or "").strip()
    if not value or any(char.isspace() for char in value):
        return ""
    return value if "://" in value or value.startswith(("exp+", "http://", "https://")) else ""


def _proof_target_expected_text(proof: dict[str, Any]) -> str:
    return _normalized_proof_expected_text(_proof_target_expected_text_value(proof))


def _proof_target_expected_text_value(proof: dict[str, Any]) -> Any:
    target = proof.get("proof_target")
    if not isinstance(target, dict):
        return ""
    return target.get("expected_text")


def _proof_target_screen(proof: dict[str, Any]) -> str:
    target = proof.get("proof_target")
    if not isinstance(target, dict):
        return ""
    return " ".join(str(target.get("screen") or "").split())


def _worker_proof_target_deep_link(worker_result: dict[str, Any], *, config_deep_link: str = "") -> str:
    value = str(
        worker_result.get("proof_deep_link")
        or worker_result.get("deep_link")
        or config_deep_link
        or ""
    ).strip()
    return _proof_target_deep_link({"proof_target": {"deep_link": value}})


def _worker_proof_target_expected_text(worker_result: dict[str, Any]) -> str:
    return _normalized_proof_expected_text(_worker_proof_target_expected_text_value(worker_result))


def _worker_proof_target_expected_text_value(worker_result: dict[str, Any]) -> Any:
    return worker_result.get("proof_expected_text") or worker_result.get("expected_text") or ""


def _worker_proof_target_screen(worker_result: dict[str, Any]) -> str:
    return " ".join(
        str(
            worker_result.get("proof_screen")
            or worker_result.get("screen")
            or ""
        ).split()
    )


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
        return "missing proof target expected text"
    if text.casefold() in _GENERIC_PROOF_EXPECTED_TEXT_VALUES:
        return "proof target expected text is too generic"
    return ""


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


def _generic_proof_target_block_reason(value: Any) -> str:
    deep_link = str(value or "").strip()
    parsed = urlparse(deep_link)
    if parsed.scheme.casefold() in _EXPO_RUNTIME_PROOF_TARGET_SCHEMES:
        return "Expo runtime proof target is not enough"
    if parsed.scheme.casefold() in {"http", "https"} and _is_local_shareable_host(parsed.hostname):
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


def _worker_proof_target_update_fields(worker_result: dict[str, Any]) -> dict[str, str]:
    fields: dict[str, str] = {}
    deep_link = _worker_proof_target_deep_link(worker_result)
    if deep_link:
        fields["proof_deep_link"] = deep_link
    expected_text = _worker_proof_target_expected_text(worker_result)
    if expected_text:
        fields["proof_expected_text"] = expected_text
    screen = _worker_proof_target_screen(worker_result)
    if screen:
        fields["proof_screen"] = screen
    return fields
