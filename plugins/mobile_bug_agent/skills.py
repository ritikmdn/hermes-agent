from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlparse

from .config import MonicaConfig, runtime_root
from .codex_worker import build_code_worker
from .intent import IntentClassifier
from .linear_client import LinearAttachmentPayload, LinearClient, LinearCommentPayload, LinearIssuePayload
from .pr_publisher import DraftPrPublisher, DraftPrPublisherError
from .proof import (
    ProofRunner,
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
)
from .repo_manager import RepoManager, configured_remote_base_ref
from .secrets import monica_slack_bot_token
from .slack_client import SlackClientError, SlackThreadClient
from .state import MonicaState
from .verifier import VerificationRunner

logger = logging.getLogger(__name__)
_APPROVED_PR_FIX_SCOPE_LABEL = "marketplace copy/design"
_APPROVED_PR_FIX_SURFACE_TERMS = (
    "marketplace",
    "pdp",
    "product detail",
    "offer detail",
    "offer card",
)
_APPROVED_PR_FIX_KIND_TERMS = (
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
_APPROVED_PR_FIX_EXCLUDED_TERMS = (
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
_GIT_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


class DefaultMonicaSkills:
    """Small v1 skill set used by the background loop.

    This is deliberately conservative. It gives Monica an agentic loop shape
    while defaulting to dry-run-safe behavior until real Linear/repo settings
    are configured.
    """

    def __init__(
        self,
        *,
        config: MonicaConfig,
        state: MonicaState,
        linear_client: Any | None = None,
        repo_manager: Any | None = None,
        codex_worker: Any | None = None,
        slack_client: Any | None = None,
        intent_classifier: Any | None = None,
        verifier: Any | None = None,
        proof_runner: Any | None = None,
        pr_publisher: Any | None = None,
    ) -> None:
        self.config = config
        self.state = state
        self._linear_client = linear_client
        self._repo_manager = repo_manager
        self._codex_worker = codex_worker
        self._slack_client = slack_client
        self._intent_classifier = intent_classifier
        self._verifier = verifier
        self._proof_runner = proof_runner
        self._pr_publisher = pr_publisher

    def read_slack_thread(self, run: Any) -> dict[str, Any]:
        raw_event = getattr(run, "raw_event", None)
        if isinstance(raw_event, dict) and raw_event.get("monica_simulated"):
            return self._fallback_thread_from_run(run)

        client = self._resolve_slack_client(run)
        if client is not None:
            try:
                context = client.read_thread(
                    channel_id=run.channel_id,
                    thread_ts=run.thread_ts,
                    limit=self.config.loop.max_thread_messages,
                )
                return context.to_dict() if hasattr(context, "to_dict") else dict(context)
            except Exception as exc:
                fallback = self._fallback_thread_from_run(run)
                fallback["context_errors"] = [f"Slack thread fetch failed: {exc}"]
                return fallback
        return self._fallback_thread_from_run(run)

    @staticmethod
    def _fallback_thread_from_run(run: Any) -> dict[str, Any]:
        raw = getattr(run, "raw_event", None)
        raw_event = raw if isinstance(raw, dict) else {}
        permalink = str(raw_event.get("permalink") or "")
        message_ts = str(raw_event.get("ts") or getattr(run, "message_ts", "") or "")
        user_id = str(raw_event.get("user") or raw_event.get("bot_id") or getattr(run, "user_id", "") or "")
        request_text = str(getattr(run, "request_text", "") or "").strip()
        files = []
        for item in raw_event.get("files") or []:
            if not isinstance(item, dict):
                continue
            files.append(
                {
                    "id": str(item.get("id") or ""),
                    "name": str(item.get("name") or item.get("title") or "Slack file"),
                    "mimetype": str(item.get("mimetype") or ""),
                    "url_private": str(item.get("url_private_download") or item.get("url_private") or ""),
                    "permalink": str(item.get("permalink_public") or item.get("permalink") or ""),
                }
            )
        for index, item in enumerate(raw_event.get("attachments") or [], start=1):
            if not isinstance(item, dict):
                continue
            image_url = str(
                item.get("image_url")
                or item.get("thumb_url")
                or item.get("url")
                or ""
            ).strip()
            if not image_url:
                continue
            files.append(
                {
                    "id": f"attachment-{getattr(run, 'message_ts', '') or 'message'}-{index}",
                    "name": str(
                        item.get("title")
                        or item.get("fallback")
                        or item.get("text")
                        or "Slack attachment"
                    ),
                    "mimetype": "image" if item.get("image_url") or item.get("thumb_url") else "",
                    "url_private": image_url,
                    "permalink": image_url,
                }
            )
        return {
            "channel_id": str(getattr(run, "channel_id", "") or ""),
            "thread_ts": str(getattr(run, "thread_ts", "") or ""),
            "permalink": permalink,
            "messages": [request_text],
            "message_details": [
                {
                    "user_id": user_id,
                    "text": request_text,
                    "ts": message_ts,
                    "permalink": permalink,
                }
            ],
            "files": files,
            "attachments": [item.get("name") or item.get("id") or "Slack file" for item in files],
        }

    def infer_user_intent(self, run: Any, thread: dict[str, Any]) -> dict[str, Any]:
        messages = [str(part) for part in thread.get("messages", [])]
        classifier = self._intent_classifier or IntentClassifier(config=self.config)
        result = classifier.classify(
            request_text=run.request_text,
            thread_text="\n".join(messages),
        )
        if hasattr(result, "to_dict"):
            return result.to_dict()
        return dict(result)

    def create_or_update_linear(
        self,
        run: Any,
        thread: dict[str, Any],
        intent: dict[str, Any],
    ) -> dict[str, Any]:
        title = _linear_title(intent.get("summary") or run.request_text)
        description = self._linear_description(run, thread, intent)
        if self.config.rollout_mode == "dry_run":
            return {
                "id": "",
                "identifier": "DRY-RUN",
                "url": "",
                "dry_run": True,
                "title": title,
                "description": description,
                "attachments": [
                    {
                        "title": attachment.title,
                        "url": attachment.url,
                        "subtitle": attachment.subtitle,
                    }
                    for attachment in self._linear_attachment_payloads("", thread)
                ],
            }

        client = self._linear_client or LinearClient(api_key=os.getenv("LINEAR_API_KEY", ""))
        issue = client.create_or_update_issue(
            LinearIssuePayload(
                team_id=self.config.linear.team_id,
                project_id=self.config.linear.project_id,
                label_ids=self.config.linear.label_ids,
                title=title,
                description=description,
            ),
            existing_issue_id=run.linear_issue_id,
        )
        attachments, attachment_errors = self._attach_linear_evidence(client, issue.id, thread)
        return {
            "id": issue.id,
            "identifier": issue.identifier,
            "url": issue.url,
            "dry_run": False,
            "title": title,
            "description": description,
            "attachments": attachments,
            "attachment_errors": attachment_errors,
        }

    def _linear_description(self, run: Any, thread: dict[str, Any], intent: dict[str, Any]) -> str:
        body = _thread_context_lines(thread)
        permalink = str(thread.get("permalink") or "").strip()
        files = thread.get("files") or []
        evidence = self._evidence_lines(files)
        context_errors = thread.get("context_errors") or []
        confidence = intent.get("confidence", "")
        open_questions = _markdown_list(intent.get("missing_questions"))
        sections = [
            "## Summary",
            str(intent.get("summary") or run.request_text),
            "",
            "## Observed Behavior",
            str(intent.get("observed_behavior") or intent.get("summary") or run.request_text),
            "",
            "## Expected Behavior",
            str(intent.get("expected_behavior") or "Not specified."),
            "",
            "## Reproduction Context",
            _markdown_list(intent.get("reproduction_steps")) or "- Not specified.",
            "",
            "## Platform And Build",
            "\n".join(
                [
                    f"- Platforms: {_join_values(intent.get('platforms')) or 'Not specified.'}",
                    f"- Device context: {intent.get('device_context') or 'Not specified.'}",
                    f"- Build context: {intent.get('build_context') or 'Not specified.'}",
                ]
            ),
            "",
            "## Slack Context",
            f"Slack thread: {permalink or 'unavailable'}",
            f"Reporter: {run.user_id or 'unknown'}",
            f"Channel: {run.channel_id}",
            "",
            "## Thread Context",
            body,
            "",
            "## Evidence",
            evidence or "- No files captured.",
        ]
        if open_questions:
            sections.extend(
                [
                    "",
                    "## Open Questions / Context Gaps",
                    open_questions,
                ]
            )
        if context_errors:
            sections.extend(
                [
                    "",
                    "## Slack Fetch Notes",
                    "\n".join(f"- {error}" for error in context_errors if str(error).strip()),
                ]
            )
        if confidence != "":
            sections.extend(
                [
                    "",
                    "## Monica Triage",
                    f"- Confidence: {confidence}",
                    f"- Reason: {intent.get('reason', '') or 'No reason provided.'}",
                ]
            )
        sections.extend(
            [
                "",
                "## Fix Status",
                self._fix_status_text(intent),
            ]
        )
        return "\n".join(sections)

    def ask_fix_approval(self, run: Any, issue: dict[str, Any]) -> None:
        issue_ref = issue.get("url") or issue.get("identifier") or "the Linear issue"
        approver_line = self._approver_line()
        suffix = f"\n{approver_line}" if approver_line else ""
        self.post_status(
            run,
            "I filed the mobile bug context and I am waiting for approval before code changes.\n"
            f"Issue: {issue_ref}\n"
            "Tag me in this thread with `approved, fix it` when you want me to start."
            f"{suffix}",
        )

    def run_internal_codex_worker(self, run: Any) -> dict[str, Any]:
        repo_manager = self._repo_manager or RepoManager(
            config=self.config.repo,
            workspace_root=runtime_root(self.config) / "workspace",
        )
        worker = self._codex_worker or build_code_worker(self.config)
        summary = run.request_text.splitlines()[0] if run.request_text else "mobile bug"
        worktree = repo_manager.prepare_worktree(
            linear_identifier=run.linear_identifier or run.id[:8],
            summary=summary,
        )
        base_ref = str(getattr(worktree, "base_ref", "") or "").strip()
        base_commit = str(getattr(worktree, "base_commit", "") or "").strip()
        if base_ref or base_commit:
            self.state.update_run(
                run.id,
                base_branch=base_ref,
                base_commit=base_commit,
            )
        thread = self.read_slack_thread(run)
        worker_result = worker.run(
            run=run,
            worktree=worktree,
            brief=self._fix_brief(run=run, worktree=worktree, thread=thread),
        )
        result = dict(worker_result)
        result["branch_name"] = worktree.branch_name
        result["worktree_path"] = str(worktree.path)
        if base_ref:
            result["base_ref"] = base_ref
        if base_commit:
            result["base_commit"] = base_commit
        git_changed = _worktree_has_git_changes(
            Path(str(worktree.path)),
            base_branch=self.config.repo.default_branch,
        )
        if git_changed is not None:
            result["changed"] = git_changed
        result["slack_permalink"] = str(thread.get("permalink") or "")
        result["evidence"] = _evidence_payloads(thread.get("files") or [])
        return result

    def run_verification(self, run: Any, worker_result: dict[str, Any]) -> dict[str, Any]:
        verifier = self._verifier or VerificationRunner(
            timeout_seconds=self.config.loop.timeout_minutes * 60,
        )
        worktree_path = worker_result.get("worktree_path") or worker_result.get("worktree")
        result = verifier.run(Path(str(worktree_path)), self.config.verification.commands)
        return result.to_dict()

    def run_proof(
        self,
        run: Any,
        worker_result: dict[str, Any],
        verification: dict[str, Any],
    ) -> dict[str, Any]:
        proof_runner = self._proof_runner or ProofRunner(config=self.config)
        worktree_path = worker_result.get("worktree_path") or worker_result.get("worktree")
        result = proof_runner.run(
            run=run,
            worktree=Path(str(worktree_path)),
            verification=verification,
            proof_target=_proof_target_from_worker_result(
                worker_result,
                config_deep_link=(
                    ""
                    if self.config.rollout_mode == "approved_pr"
                    else self.config.proof.deep_link
                ),
            ),
        )
        return result.to_dict()

    def share_proof_artifacts(self, run: Any, proof: dict[str, Any]) -> dict[str, Any]:
        client = self._resolve_slack_client(run)
        if client is None:
            return {
                "shareable_artifacts": [],
                "share_errors": ["Slack client is not configured for proof artifact upload."],
            }

        refs: list[dict[str, str]] = []
        errors: list[str] = []
        shareable_artifacts, manifest_errors = _shareable_visual_proof_artifact_paths(proof)
        errors.extend(manifest_errors)
        for artifact in shareable_artifacts:
            platform = _proof_artifact_platform(artifact)
            title = _proof_artifact_title(platform=platform, path=artifact)
            if not artifact.is_file():
                errors.append(f"{artifact}: file does not exist")
                continue
            try:
                uploaded = client.upload_thread_file(
                    channel_id=run.channel_id,
                    thread_ts=run.thread_ts,
                    file_path=str(artifact),
                    title=title,
                    initial_comment="Monica simulator proof artifact.",
                )
            except Exception as exc:
                errors.append(f"{artifact}: {exc}")
                continue
            url = _uploaded_file_url(uploaded)
            if not url:
                errors.append(f"{artifact}: Slack upload did not return a permalink")
                continue
            refs.append(
                {
                    "platform": platform,
                    "path": str(artifact),
                    "url": url,
                    "title": title,
                }
            )
        return {"shareable_artifacts": refs, "share_errors": errors}

    def open_draft_pr(
        self,
        run: Any,
        worker_result: dict[str, Any],
        verification: dict[str, Any],
    ) -> dict[str, Any]:
        publisher = self._pr_publisher or DraftPrPublisher()
        if self.config.rollout_mode == "approved_pr":
            if not _run_has_linear_issue(run):
                raise DraftPrPublisherError("Linear issue is required before draft PR publishing.")
            if not str(getattr(run, "linear_url", "") or "").strip():
                raise DraftPrPublisherError("Linear issue URL is required before draft PR publishing.")
            if not _is_linear_issue_url(getattr(run, "linear_url", "")):
                raise DraftPrPublisherError(
                    "Linear issue URL must be a Linear issue link before draft PR publishing."
                )
            if not _run_is_approved_pr_fix_scope(run):
                raise DraftPrPublisherError(
                    "Monica approved-PR publishing is limited to "
                    f"{_APPROVED_PR_FIX_SCOPE_LABEL} bugs."
                )
            proof_for_precheck = worker_result.get("proof")
            preproof_metadata_block_reason = ""
            if isinstance(proof_for_precheck, dict) and proof_for_precheck.get("passed"):
                preproof_metadata_block_reason = _approved_pr_publish_metadata_block_reason(
                    run,
                    worker_result,
                    self.config,
                )
            if preproof_metadata_block_reason:
                raise DraftPrPublisherError(preproof_metadata_block_reason)
            expected_manifest_branch = str(worker_result.get("branch_name") or run.branch_name or "")
            expected_manifest_worktree = str(
                worker_result.get("worktree_path") or worker_result.get("worktree") or ""
            )
            proof_block_reason = _approved_pr_publish_block_reason(
                worker_result.get("proof"),
                expected_target=_proof_target_from_worker_result(worker_result),
                expected_base_commit=str(worker_result.get("base_commit") or ""),
                expected_base_ref=str(worker_result.get("base_ref") or worker_result.get("base_branch") or ""),
                expected_branch_name=expected_manifest_branch,
                expected_worktree=expected_manifest_worktree,
                expected_run_id=str(getattr(run, "id", "") or ""),
                expected_linear_identifier=str(getattr(run, "linear_identifier", "") or ""),
                expected_linear_url=str(getattr(run, "linear_url", "") or ""),
                expected_setup_commands=self.config.proof.setup_commands,
                expected_commands=self.config.proof.commands,
                expected_required_env_keys=self.config.proof.required_env_keys,
            )
            if proof_block_reason:
                raise DraftPrPublisherError(proof_block_reason)
            approval_block_reason = _approved_pr_approval_block_reason(run, self.config)
            if approval_block_reason:
                raise DraftPrPublisherError(approval_block_reason)
            if worker_result.get("changed") is not True:
                raise DraftPrPublisherError("code changes are required before draft PR publishing.")
            if verification.get("passed") is not True:
                raise DraftPrPublisherError("verification must pass before draft PR publishing.")
            verification_block_reason = _approved_pr_verification_block_reason(verification, self.config)
            if verification_block_reason:
                raise DraftPrPublisherError(verification_block_reason)
            metadata_block_reason = _approved_pr_publish_metadata_block_reason(run, worker_result, self.config)
            if metadata_block_reason:
                raise DraftPrPublisherError(metadata_block_reason)
            proof_config_block_reason = _approved_pr_configured_proof_commands_block_reason(self.config)
            if proof_config_block_reason:
                raise DraftPrPublisherError(proof_config_block_reason)
        branch_name = str(worker_result.get("branch_name") or run.branch_name or "")
        worktree_path = str(worker_result.get("worktree_path") or worker_result.get("worktree") or "")
        title = f"[{run.linear_identifier or 'Monica'}] {run.request_text.splitlines()[0][:90]}"
        body = self._pr_body(run=run, worker_result=worker_result, verification=verification)
        if self.config.rollout_mode == "approved_pr":
            self._record_linear_approved_pr_context(
                run=run,
                worker_result=worker_result,
                verification=verification,
            )
        url = publisher.publish(
            worktree=worktree_path,
            branch_name=branch_name,
            base_branch=self.config.repo.default_branch,
            title=title,
            body=body,
        )
        if self.config.rollout_mode == "approved_pr" and not _is_valid_pull_request_url(url):
            raise DraftPrPublisherError("publisher must return a valid draft PR URL.")
        return {"url": url}

    def post_status(self, run: Any, text: str) -> bool:
        client = self._resolve_slack_client(run)
        if client is None:
            return False
        try:
            client.post_thread_reply(channel_id=run.channel_id, thread_ts=run.thread_ts, text=text)
            return True
        except Exception as exc:
            logger.warning(
                "Slack status post failed run_id=%s channel_id=%s thread_ts=%s error=%s",
                getattr(run, "id", ""),
                getattr(run, "channel_id", ""),
                getattr(run, "thread_ts", ""),
                exc,
            )
            return False

    def _record_linear_approved_pr_context(
        self,
        *,
        run: Any,
        worker_result: dict[str, Any],
        verification: dict[str, Any],
    ) -> None:
        issue_id = str(getattr(run, "linear_issue_id", "") or "").strip()
        if not issue_id:
            return
        client = self._linear_client
        if client is None:
            api_key = os.getenv("LINEAR_API_KEY", "").strip()
            if not api_key:
                return
            client = LinearClient(api_key=api_key)
        create_comment = getattr(client, "create_comment", None)
        if not callable(create_comment):
            return
        body = _approved_pr_linear_context_body(worker_result=worker_result, verification=verification)
        if not body:
            return
        try:
            create_comment(LinearCommentPayload(issue_id=issue_id, body=body))
        except Exception as exc:
            logger.warning(
                "Linear approved-PR context comment failed run_id=%s issue_id=%s error=%s",
                getattr(run, "id", ""),
                issue_id,
                exc,
            )

    def _fix_status_text(self, intent: dict[str, Any]) -> str:
        if not intent.get("wants_fix"):
            return "Ticket-only triage."
        if self.config.rollout_mode == "approved_pr":
            return "Awaiting explicit tagged approval before code changes."
        if self.config.rollout_mode == "local_fix_only":
            return "Awaiting explicit tagged approval before local code changes; no push or PR will run."
        if self.config.rollout_mode == "dry_run":
            return "Dry run only; no code changes will run."
        return "Code fixes are disabled in the current Monica rollout mode."

    def _approver_line(self) -> str:
        approvers = [f"<@{user_id}>" for user_id in self.config.slack.approver_user_ids if user_id]
        if not approvers:
            return ""
        return f"Allowed approvers: {', '.join(approvers)}."

    def _fix_brief(self, *, run: Any, worktree: Any, thread: dict[str, Any]) -> str:
        messages = [str(message).strip() for message in thread.get("messages", []) if str(message).strip()]
        message_lines = "\n".join(f"- {message}" for message in messages)
        evidence = self._evidence_lines(thread.get("files") or [])
        context_errors = thread.get("context_errors") or []
        verification = "\n".join(
            f"- {command}" for command in self.config.verification.commands if command.strip()
        )
        permalink = str(thread.get("permalink") or "").strip()
        proof_target_section: list[str] = []
        if self.config.rollout_mode == "approved_pr":
            proof_target_section = [
                "",
                "## Required Proof Target",
                (
                    "Before draft PR publishing, Monica must capture iOS and Android proof "
                    "on the exact fixed screen. End the worker summary with both lines:"
                ),
                "- Monica proof deep link: <url>",
                "- Monica proof expected text: <text visible on the fixed screen>",
                "- Monica proof screen: <route or screen name> (optional when known)",
                (
                    "These are required before Monica can capture iOS and Android proof "
                    "and open a draft PR."
                ),
            ]
        return "\n".join(
            [
                "# Monica Fix Brief",
                "",
                f"Run ID: {run.id}",
                f"Linear: {run.linear_identifier or 'unlinked'}",
                f"Linear URL: {run.linear_url or 'unavailable'}",
                f"Branch: {worktree.branch_name}",
                f"Base: {getattr(worktree, 'base_ref', '') or 'unavailable'}"
                + (
                    f" @ {getattr(worktree, 'base_commit', '')}"
                    if getattr(worktree, "base_commit", "")
                    else ""
                ),
                f"Worktree: {worktree.path}",
                f"Slack thread: channel={run.channel_id} thread_ts={run.thread_ts}",
                f"Slack permalink: {permalink or 'unavailable'}",
                "",
                "## User Request",
                run.request_text,
                "",
                "## Slack Thread Context",
                message_lines or "- No thread context captured.",
                "",
                "## Evidence",
                evidence or "- No files captured.",
                "",
                "## Slack Fetch Notes",
                "\n".join(f"- {error}" for error in context_errors if str(error).strip())
                or "- None.",
                "",
                "## Verification Commands",
                verification or "- No verification commands configured.",
                *proof_target_section,
                "",
                "## Instructions",
                (
                    "Investigate the React Native mobile app bug, make the smallest safe fix, "
                    "and leave the worktree ready for verification. If the app exposes a safe "
                    "dev-client deep link or route for the affected screen, end the worker summary "
                    "with `Monica proof deep link: <url>` and `Monica proof expected text: <text "
                    "visible on the fixed screen>`, plus `Monica proof screen: <route or screen "
                    "name>` when the exact screen name is known, so simulator proof can exercise "
                    "and verify the right page."
                ),
            ]
        )

    def _resolve_slack_client(self, run: Any) -> Any | None:
        if self._slack_client is not None:
            return self._slack_client
        token = monica_slack_bot_token()
        if not token:
            return None
        try:
            return SlackThreadClient.from_token(
                token=token,
                monica_user_ids=self.config.slack.bot_user_ids,
                download_dir=runtime_root(self.config) / "attachments" / run.id,
                download_attachments=self.config.slack.download_attachments,
                max_attachment_bytes=self.config.loop.max_attachment_bytes,
            )
        except SlackClientError:
            return None

    @staticmethod
    def _evidence_lines(files: Any) -> str:
        lines: list[str] = []
        for item in files:
            if not isinstance(item, dict):
                lines.append(f"- {item}")
                continue
            label = item.get("local_path") or item.get("name") or item.get("id") or "Slack file"
            mimetype = item.get("mimetype") or "unknown type"
            error = item.get("error") or ""
            url = _evidence_url(item)
            suffix = f" ({mimetype})"
            if url:
                suffix += f": {url}"
            if error:
                suffix += f" - access note: {error}"
            lines.append(f"- {label}{suffix}")
        return "\n".join(lines)

    def _attach_linear_evidence(
        self,
        client: Any,
        issue_id: str,
        thread: dict[str, Any],
    ) -> tuple[list[dict[str, str]], list[str]]:
        attachments: list[dict[str, str]] = []
        errors: list[str] = []
        for payload in self._linear_attachment_payloads(issue_id, thread):
            try:
                attachment = client.create_attachment(payload)
            except Exception as exc:
                errors.append(f"{payload.title}: {exc}")
                continue
            attachments.append(
                {
                    "id": getattr(attachment, "id", ""),
                    "title": getattr(attachment, "title", payload.title),
                    "url": getattr(attachment, "url", payload.url),
                }
            )
        return attachments, errors

    @staticmethod
    def _linear_attachment_payloads(
        issue_id: str,
        thread: dict[str, Any],
    ) -> list[LinearAttachmentPayload]:
        payloads: list[LinearAttachmentPayload] = []
        for item in thread.get("files") or []:
            if not isinstance(item, dict):
                continue
            url = str(
                item.get("permalink_public")
                or item.get("permalink")
                or item.get("url_private_download")
                or item.get("url_private")
                or item.get("url")
                or ""
            ).strip()
            url = _safe_evidence_url(url)
            if not url:
                continue
            title = str(
                item.get("name") or item.get("title") or item.get("id") or "Slack file"
            ).strip()
            subtitle = str(item.get("mimetype") or item.get("filetype") or "Slack evidence").strip()
            payloads.append(
                LinearAttachmentPayload(
                    issue_id=issue_id,
                    title=title or "Slack file",
                    url=url,
                    subtitle=subtitle,
                )
            )
        return payloads

    @staticmethod
    def _pr_body(*, run: Any, worker_result: dict[str, Any], verification: dict[str, Any]) -> str:
        slack_link = str(worker_result.get("slack_permalink") or "").strip()
        slack_value = slack_link or f"channel={run.channel_id} thread={run.thread_ts}"
        evidence = _pr_evidence_lines(worker_result.get("evidence") or [])
        proof = _pr_proof_lines(worker_result.get("proof"))
        base = _pr_base_line(worker_result)
        return "\n".join(
            _without_empty_sections(
                [
                    "## Monica Summary",
                    str(worker_result.get("summary") or "Monica completed the requested fix."),
                    "",
                    "## Links",
                    f"- Linear: {run.linear_url or run.linear_identifier or 'unavailable'}",
                    f"- Slack: {slack_value}",
                    f"- {base}" if base else "",
                    "",
                    "## Evidence" if evidence else "",
                    evidence,
                    "",
                    "## Verification",
                    str(verification.get("summary") or ""),
                    "",
                    "```",
                    str(verification.get("output") or "")[:12000],
                    "```",
                    "",
                    "## Proof" if proof else "",
                    proof,
                ]
            )
        )


def _markdown_list(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        return ""
    return "\n".join(f"- {str(item).strip()}" for item in values if str(item).strip())


def _join_values(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(item).strip() for item in value if str(item).strip())
    return ""


def _linear_title(summary: Any, *, limit: int = 120) -> str:
    body = " ".join(str(summary or "").split()) or "Tagged bug report"
    title = f"[Mobile] {body}"
    if len(title) <= limit:
        return title
    return title[: max(0, limit - 3)].rstrip() + "..."


def _evidence_payloads(files: Any) -> list[dict[str, str]]:
    payloads: list[dict[str, str]] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        url = str(
            item.get("permalink_public")
            or item.get("permalink")
            or item.get("url_private_download")
            or item.get("url_private")
            or item.get("url")
            or item.get("local_path")
            or ""
        ).strip()
        url = _safe_evidence_url(url)
        name = str(item.get("name") or item.get("title") or item.get("id") or "Slack file").strip()
        mimetype = str(item.get("mimetype") or item.get("filetype") or "").strip()
        if name or url:
            payloads.append({"name": name or "Slack file", "mimetype": mimetype, "url": url})
    return payloads


def _evidence_url(item: dict[str, Any]) -> str:
    return _safe_evidence_url(
        item.get("permalink_public")
        or item.get("permalink")
        or item.get("url_private_download")
        or item.get("url_private")
        or item.get("url")
        or ""
    )


def _safe_evidence_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    if _is_local_evidence_host(parsed.hostname):
        return ""
    return text


def _safe_proof_shareable_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    if _is_unshareable_proof_host(parsed.hostname):
        return ""
    return text


def _is_valid_pull_request_url(value: Any) -> bool:
    text = _safe_evidence_url(value)
    if not text:
        return False
    parsed = urlparse(text)
    host = str(parsed.hostname or "").strip().lower().rstrip(".")
    return (
        host == "github.com"
        and bool(re.fullmatch(r"/[^/]+/[^/]+/pull/\d+/?", parsed.path))
    )


def _is_local_evidence_host(hostname: str | None) -> bool:
    host = str(hostname or "").strip().lower().rstrip(".")
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ip_address(host)
    except ValueError:
        return host.endswith(_LOCAL_EVIDENCE_HOST_SUFFIXES)
    return (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_unspecified
    )


def _is_unshareable_proof_host(hostname: str | None) -> bool:
    host = str(hostname or "").strip().lower().rstrip(".")
    if not host:
        return False
    if _is_local_evidence_host(host):
        return True
    try:
        ip_address(host)
    except ValueError:
        return "." not in host
    return False


def _pr_evidence_lines(evidence: Any) -> str:
    lines: list[str] = []
    for item in evidence:
        if not isinstance(item, dict):
            text = str(item).strip()
            if text:
                lines.append(f"- {text}")
            continue
        name = str(item.get("name") or "Slack file").strip()
        mimetype = str(item.get("mimetype") or "").strip()
        url = _safe_evidence_url(item.get("url") or "")
        label = f"{name} ({mimetype})" if mimetype else name
        lines.append(f"- {label}: {url}" if url else f"- {label}")
    return "\n".join(lines)


def _pr_base_line(worker_result: dict[str, Any]) -> str:
    base_ref = str(worker_result.get("base_ref") or worker_result.get("base_branch") or "").strip()
    base_commit = str(worker_result.get("base_commit") or "").strip()
    if base_ref and base_commit:
        return f"Base: {base_ref} @ {base_commit}"
    if base_ref:
        return f"Base: {base_ref}"
    if base_commit:
        return f"Base commit: {base_commit}"
    return ""


def _approved_pr_linear_context_body(
    *,
    worker_result: dict[str, Any],
    verification: dict[str, Any],
) -> str:
    lines: list[str] = ["Monica approved-PR context"]
    branch_name = str(worker_result.get("branch_name") or "").strip()
    if branch_name:
        lines.append(f"Branch: {branch_name}")
    if base := _pr_base_line(worker_result):
        lines.append(base)
    verification_summary = str(verification.get("summary") or "").strip()
    if verification_summary:
        lines.append(f"Verification: {verification_summary}")
    verification_output = _trim_linear_comment_value(verification.get("output"), limit=4000)
    if verification_output:
        lines.extend(["Verification output:", "```", verification_output, "```"])
    proof = worker_result.get("proof")
    if isinstance(proof, dict):
        target = proof.get("proof_target")
        if isinstance(target, dict):
            deep_link = str(target.get("deep_link") or "").strip()
            if deep_link:
                lines.append(f"Proof target: {deep_link}")
            expected_text = " ".join(str(target.get("expected_text") or "").split())
            if expected_text:
                lines.append(f"Expected text: {expected_text}")
            screen = " ".join(str(target.get("screen") or "").split())
            if screen:
                lines.append(f"Screen: {screen}")
        required_env_keys = _proof_required_env_keys(proof.get("required_env_keys"))
        if required_env_keys:
            lines.append(f"Required env keys: {', '.join(required_env_keys)}")
        lines.extend(_linear_proof_shareable_lines(proof.get("shareable_artifacts")))
    return "\n".join(lines).strip()


def _linear_proof_shareable_lines(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    lines: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        url = _safe_proof_shareable_url(item.get("url") or item.get("permalink") or "")
        if not url:
            continue
        platform = str(item.get("platform") or "").strip().lower()
        path = str(item.get("path") or "").strip()
        if not platform and path:
            platform = _proof_artifact_platform(Path(path))
        label = "iOS proof" if platform == "ios" else "Android proof" if platform == "android" else "Proof"
        lines.append(f"{label}: {url}")
    return lines


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


def _trim_linear_comment_value(value: Any, *, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _pr_proof_lines(proof: Any) -> str:
    if not isinstance(proof, dict):
        return ""
    lines: list[str] = []
    summary = str(proof.get("summary") or "").strip()
    if summary:
        lines.append(summary)
    platforms = [
        str(platform).strip()
        for platform in proof.get("platforms") or []
        if str(platform).strip()
    ]
    if platforms:
        lines.append(f"Platforms: {', '.join(platforms)}")
    required_env_keys = _proof_required_env_keys(proof.get("required_env_keys"))
    if required_env_keys:
        lines.append(f"Required env keys: {', '.join(required_env_keys)}")
    setup_commands = _proof_command_lines(proof.get("setup_commands"))
    if setup_commands:
        lines.append("Setup commands:")
        lines.extend(setup_commands)
    proof_commands = _proof_command_lines(proof.get("commands"))
    if proof_commands:
        lines.append("Proof commands:")
        lines.extend(proof_commands)
    target = proof.get("proof_target")
    if isinstance(target, dict):
        deep_link = str(target.get("deep_link") or "").strip()
        if deep_link:
            lines.append(f"Target: {deep_link}")
        expected_text = " ".join(str(target.get("expected_text") or "").split())
        if expected_text:
            lines.append(f"Expected text: {expected_text}")
        screen = " ".join(str(target.get("screen") or "").split())
        if screen:
            lines.append(f"Screen: {screen}")
    shareable = _proof_shareable_artifact_lines(proof.get("shareable_artifacts"))
    if shareable:
        lines.extend(shareable)
    artifacts = [str(path).strip() for path in proof.get("artifacts") or [] if str(path).strip()]
    if artifacts:
        if shareable:
            lines.append(f"Local artifacts (debug): {', '.join(artifacts)}")
        else:
            lines.extend(f"- {path}" for path in artifacts)
    return "\n".join(lines)


def _proof_target_from_worker_result(
    worker_result: dict[str, Any],
    *,
    config_deep_link: str = "",
) -> dict[str, str]:
    deep_link = str(
        worker_result.get("proof_deep_link")
        or worker_result.get("deep_link")
        or config_deep_link
        or ""
    ).strip()
    expected_text = str(
        worker_result.get("proof_expected_text")
        or worker_result.get("expected_text")
        or ""
    ).strip()
    screen = str(
        worker_result.get("proof_screen")
        or worker_result.get("screen")
        or ""
    ).strip()
    target: dict[str, str] = {}
    if deep_link:
        target["deep_link"] = deep_link
    if _usable_proof_expected_text(expected_text):
        target["expected_text"] = expected_text
    if screen:
        target["screen"] = " ".join(screen.split())
    return target


def _usable_proof_expected_text(value: Any) -> bool:
    text = " ".join(str(value or "").split())
    return bool(
        text
        and _proof_expected_text_unusable_key(text)
        not in _UNUSABLE_PROOF_EXPECTED_TEXT_VALUES
    )


def _proof_expected_text_unusable_key(value: str) -> str:
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


_UNUSABLE_PROOF_EXPECTED_TEXT_VALUES = {
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


def _proof_command_lines(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [f"- {command}" for command in (str(item).strip() for item in value) if command]


def _run_has_linear_issue(run: Any) -> bool:
    return any(
        str(getattr(run, field, "") or "").strip()
        for field in ("linear_identifier", "linear_issue_id", "linear_url")
    )


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


def _run_is_approved_pr_fix_scope(run: Any) -> bool:
    text = _normalized_scope_text(str(getattr(run, "request_text", "") or ""))
    has_surface = any(term in text for term in _APPROVED_PR_FIX_SURFACE_TERMS)
    has_kind = any(term in text for term in _APPROVED_PR_FIX_KIND_TERMS)
    has_excluded_kind = _has_approved_pr_excluded_term(text)
    return has_surface and has_kind and not has_excluded_kind


def _has_approved_pr_excluded_term(text: str) -> bool:
    normalized = _normalized_scope_text(text)
    for term in _APPROVED_PR_FIX_EXCLUDED_TERMS:
        if term == "crash":
            if re.search(r"\bcrash(?:es|ed|ing)?\b", normalized):
                return True
            continue
        if re.search(rf"\b{re.escape(term)}\b", normalized):
            return True
    return False


def _normalized_scope_text(value: str) -> str:
    return " ".join(str(value or "").lower().replace("-", " ").replace("_", " ").split())


def _approved_pr_approval_block_reason(run: Any, config: MonicaConfig) -> str:
    approved_by = str(getattr(run, "approved_by_user_id", "") or "").strip()
    if not approved_by:
        return "explicit approver approval is required before draft PR publishing."
    configured_approvers = {
        str(user_id).strip()
        for user_id in config.slack.approver_user_ids
        if str(user_id).strip()
    }
    if not configured_approvers or approved_by not in configured_approvers:
        return "configured approver approval is required before draft PR publishing."
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
        return "verification command evidence is required before draft PR publishing."
    if configured.issubset(reported):
        return ""
    output = _normalized_command_text(verification.get("output") or "")
    if not all(command in output for command in configured):
        return "verification command evidence is required before draft PR publishing."
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


def _approved_pr_publish_metadata_block_reason(
    run: Any,
    worker_result: dict[str, Any],
    config: MonicaConfig,
) -> str:
    branch_name = str(worker_result.get("branch_name") or getattr(run, "branch_name", "") or "").strip()
    if not branch_name:
        return "branch is required before draft PR publishing."
    branch_prefix = str(config.repo.branch_prefix or "").strip().rstrip("/")
    if branch_prefix and not branch_name.startswith(f"{branch_prefix}/"):
        return "Monica branch prefix is required before draft PR publishing."
    linear_identifier = str(getattr(run, "linear_identifier", "") or "").strip()
    if linear_identifier and linear_identifier not in branch_name:
        return "Linear issue identifier is required in the branch before draft PR publishing."
    worktree_path = str(worker_result.get("worktree_path") or worker_result.get("worktree") or "").strip()
    if not worktree_path:
        return "worktree is required before draft PR publishing."
    base_ref = str(worker_result.get("base_ref") or worker_result.get("base_branch") or "").strip()
    base_commit = str(worker_result.get("base_commit") or "").strip()
    if not base_ref or not _looks_like_git_commit(base_commit):
        return "base commit metadata is required before draft PR publishing."
    expected_base_ref = configured_remote_base_ref(config.repo.default_branch)
    if base_ref != expected_base_ref:
        return "base ref must match the configured default branch before draft PR publishing."
    return ""


def _looks_like_git_commit(value: str) -> bool:
    return bool(_GIT_COMMIT_RE.fullmatch(str(value or "").strip()))


def _approved_pr_configured_proof_commands_block_reason(config: MonicaConfig) -> str:
    if config.rollout_mode != "approved_pr":
        return ""
    if not _normalized_command_values(config.proof.setup_commands):
        return "configured proof setup commands are required before draft PR publishing."
    if not _normalized_command_values(config.proof.commands):
        return "configured proof commands are required before draft PR publishing."
    if _placeholder_proof_setup_commands(config.proof.setup_commands):
        return "configured proof setup commands cannot be placeholder before draft PR publishing."
    if _placeholder_proof_commands(config.proof.commands):
        return "configured proof commands cannot be placeholder before draft PR publishing."
    if _noop_only_proof_setup_commands(config.proof.setup_commands):
        return "configured proof setup commands cannot be no-op before draft PR publishing."
    if _noop_only_proof_commands(config.proof.commands):
        return "configured proof commands cannot be no-op before draft PR publishing."
    if not _normalized_required_env_key_values(config.proof.required_env_keys):
        return "configured proof required env keys are required before draft PR publishing."
    invalid_required_env_keys = _invalid_required_env_keys(config.proof.required_env_keys)
    if invalid_required_env_keys:
        return (
            "configured proof required env keys are invalid before draft PR publishing: "
            f"{', '.join(invalid_required_env_keys)}."
        )
    return ""


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
_TEXT_PROOF_SUFFIXES = {".html", ".json", ".log", ".txt", ".xml"}
_TEXT_PROOF_READ_LIMIT_BYTES = 1_000_000
_PROOF_MANIFEST_NAME = "monica-proof-manifest.json"


def _proof_artifact_paths(value: Any) -> list[Path]:
    if not isinstance(value, (list, tuple, set)):
        return []
    paths: list[Path] = []
    for item in value:
        path = Path(str(item or "").strip())
        if str(path):
            paths.append(path)
    return paths


def _visual_proof_artifact_paths(value: Any) -> list[Path]:
    return [
        path
        for path in _proof_artifact_paths(value)
        if path.suffix.lower() in _VISUAL_PROOF_SUFFIXES
    ]


def _shareable_visual_proof_artifact_paths(proof: dict[str, Any]) -> tuple[list[Path], list[str]]:
    artifact_paths = _proof_artifact_paths(proof.get("artifacts") if isinstance(proof, dict) else ())
    manifest_path = _proof_manifest_artifact_path(artifact_paths)
    if manifest_path is not None:
        manifest_artifacts, manifest_errors = _manifest_proof_artifact_paths(manifest_path)
        visual_artifacts = [
            path
            for path in manifest_artifacts
            if path.suffix.lower() in _VISUAL_PROOF_SUFFIXES
        ]
        if manifest_artifacts and not visual_artifacts and not manifest_errors:
            manifest_errors = [
                f"{manifest_path}: proof manifest does not list visual proof artifacts"
            ]
        return visual_artifacts, manifest_errors
    return [
        path
        for path in artifact_paths
        if path.suffix.lower() in _VISUAL_PROOF_SUFFIXES
    ], []


def _manifest_proof_artifact_paths(manifest_path: Path) -> tuple[list[Path], list[str]]:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], [f"{manifest_path}: proof manifest could not be read"]
    if not isinstance(payload, dict):
        return [], [f"{manifest_path}: proof manifest is not an object"]
    raw_artifacts = payload.get("proof_artifacts")
    if not isinstance(raw_artifacts, (list, tuple)):
        return [], [f"{manifest_path}: proof manifest does not list proof artifacts"]
    paths: list[Path] = []
    errors: list[str] = []
    seen: set[str] = set()
    for value in raw_artifacts:
        text = str(value or "").strip()
        if not text:
            continue
        path = Path(text)
        if not path.is_absolute():
            path = manifest_path.parent / path
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if not _path_is_under_dir(path, manifest_path.parent):
            errors.append(f"{path}: proof artifact is outside proof manifest directory")
            continue
        paths.append(path)
    if not paths and not errors:
        errors.append(f"{manifest_path}: proof manifest does not list proof artifacts")
    return paths, errors


def _proof_manifest_artifact_path(paths: list[Path]) -> Path | None:
    for path in paths:
        if path.name == _PROOF_MANIFEST_NAME:
            return path
    return None


def _proof_artifacts_outside_manifest_dir(
    paths: list[Path],
    *,
    manifest_path: Path,
) -> list[str]:
    manifest_dir = manifest_path.parent
    manifest_key = _proof_path_key(str(manifest_path))
    outside: list[str] = []
    for path in paths:
        if _proof_path_key(str(path)) == manifest_key:
            continue
        artifact_path = path.expanduser()
        if not artifact_path.is_absolute():
            artifact_path = manifest_dir / artifact_path
        if _path_is_under_dir(artifact_path, manifest_dir):
            continue
        label = path.name or str(path)
        if label not in outside:
            outside.append(label)
    return outside


def _path_is_under_dir(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _proof_artifact_platform(path: Path) -> str:
    haystack = " ".join((path.name, path.stem, *path.parts[-3:])).lower()
    if any(token in haystack for token in ("ios", "iphone", "ipad")):
        return "ios"
    if "android" in haystack:
        return "android"
    return ""


def _proof_artifact_title(*, platform: str, path: Path) -> str:
    label = "iOS" if platform == "ios" else "Android" if platform == "android" else "mobile"
    return f"Monica {label} proof: {path.name}"


def _uploaded_file_url(uploaded: Any) -> str:
    if isinstance(uploaded, dict):
        return _safe_evidence_url(
            uploaded.get("permalink_public")
            or uploaded.get("permalink")
            or uploaded.get("url")
            or ""
        )
    return _safe_evidence_url(
        getattr(uploaded, "permalink", "")
        or getattr(uploaded, "url", "")
        or ""
    )


def _proof_shareable_artifact_lines(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    lines: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        url = _safe_proof_shareable_url(item.get("url") or item.get("permalink") or "")
        if not url:
            continue
        platform = str(item.get("platform") or "").strip().lower()
        path = str(item.get("path") or "").strip()
        if not platform and path:
            platform = _proof_artifact_platform(Path(path))
        label = "iOS" if platform == "ios" else "Android" if platform == "android" else "Proof"
        lines.append(f"- {label}: {url}")
    return lines


def _approved_pr_publish_block_reason(
    proof: Any,
    *,
    expected_target: Mapping[str, str] | None = None,
    expected_base_commit: str = "",
    expected_base_ref: str = "",
    expected_branch_name: str = "",
    expected_worktree: str = "",
    expected_run_id: str = "",
    expected_linear_identifier: str = "",
    expected_linear_url: str = "",
    expected_setup_commands: Any = (),
    expected_commands: Any = (),
    expected_required_env_keys: Any = (),
) -> str:
    if not isinstance(proof, dict) or not proof.get("passed"):
        return "proof evidence is required before draft PR publishing."
    target = proof.get("proof_target")
    if not isinstance(target, dict) or not _valid_proof_deep_link(target.get("deep_link")):
        return "proof target deep link is required before draft PR publishing."
    if not _usable_proof_expected_text(target.get("expected_text")):
        return "proof target expected text is required before draft PR publishing."
    if not isinstance(expected_target, Mapping) or not _valid_proof_deep_link(expected_target.get("deep_link")):
        return "worker proof target deep link is required before draft PR publishing."
    if not _usable_proof_expected_text(expected_target.get("expected_text")):
        return "worker proof target expected text is required before draft PR publishing."
    generic_target_reason = _generic_proof_target_block_reason(expected_target.get("deep_link"))
    if generic_target_reason:
        return generic_target_reason
    generic_target_reason = _generic_proof_target_block_reason(target.get("deep_link"))
    if generic_target_reason:
        return generic_target_reason
    mismatch_reason = _proof_target_mismatch_reason(target=target, expected_target=expected_target)
    if mismatch_reason:
        return mismatch_reason
    if not _proof_command_lines(proof.get("setup_commands")):
        return "proof setup commands are required before draft PR publishing."
    if not _proof_command_lines(proof.get("commands")):
        return "proof commands are required before draft PR publishing."
    secret_setup_assignments = _inline_secret_env_assignments(
        tuple(_normalized_command_values(proof.get("setup_commands")))
    )
    if secret_setup_assignments:
        return (
            "proof setup commands must not inline secret env assignment(s) before "
            "draft PR publishing: "
            f"{', '.join(secret_setup_assignments)}."
        )
    secret_proof_assignments = _inline_secret_env_assignments(
        tuple(_normalized_command_values(proof.get("commands")))
    )
    if secret_proof_assignments:
        return (
            "proof commands must not inline secret env assignment(s) before "
            "draft PR publishing: "
            f"{', '.join(secret_proof_assignments)}."
        )
    configured_setup = _normalized_command_values(expected_setup_commands)
    if configured_setup and _normalized_command_values(proof.get("setup_commands")) != configured_setup:
        return "proof setup commands do not match configured proof setup commands before draft PR publishing."
    configured_commands = _normalized_command_values(expected_commands)
    if configured_commands and _normalized_command_values(proof.get("commands")) != configured_commands:
        return "proof commands do not match configured proof commands before draft PR publishing."
    configured_required_env_keys = _normalized_required_env_key_values(
        expected_required_env_keys
    )
    if (
        configured_required_env_keys
        and _normalized_required_env_key_values(proof.get("required_env_keys"))
        != configured_required_env_keys
    ):
        return "proof required env keys do not match configured proof required env keys before draft PR publishing."

    artifact_paths = _proof_artifact_paths(proof.get("artifacts"))
    artifacts = [
        path
        for path in artifact_paths
        if path.suffix.lower() in _VISUAL_PROOF_SUFFIXES
    ]
    missing_artifacts = [
        platform
        for platform in ("ios", "android")
        if not any(_proof_artifact_platform(path) == platform for path in artifacts)
    ]
    if missing_artifacts:
        return f"iOS and Android visual proof artifacts are required before draft PR publishing: missing {', '.join(missing_artifacts)}."
    missing_files = _missing_local_visual_proof_files(artifacts)
    if missing_files:
        return f"proof artifact files are required before draft PR publishing: missing {', '.join(missing_files)}."
    empty_files = _empty_local_visual_proof_files(artifacts)
    if empty_files:
        return f"empty proof artifact files are not valid before draft PR publishing: {', '.join(empty_files)}."
    auth_fallback_platforms = _auth_fallback_platforms(
        artifacts=tuple(str(path) for path in artifact_paths),
        platforms=("ios", "android"),
    )
    if auth_fallback_platforms:
        return (
            "auth/onboarding proof fallback observed before draft PR publishing: "
            f"{', '.join(auth_fallback_platforms)}."
        )
    non_target_platforms = _non_target_screen_platforms(
        artifacts=tuple(str(path) for path in artifact_paths),
        platforms=("ios", "android"),
    )
    if non_target_platforms:
        return (
            "non-target app screen observed before draft PR publishing: "
            f"{', '.join(non_target_platforms)}."
        )
    missing_route_platforms = _missing_target_route_platforms(
        artifacts=tuple(str(path) for path in artifact_paths),
        platforms=("ios", "android"),
    )
    if missing_route_platforms:
        return (
            "proof target route evidence artifacts are required before draft PR publishing: "
            f"missing {', '.join(missing_route_platforms)}."
        )
    mismatched_route_platforms = _mismatched_target_route_platforms(
        artifacts=tuple(str(path) for path in artifact_paths),
        platforms=("ios", "android"),
        proof_target=target,
    )
    if mismatched_route_platforms:
        return (
            "proof target route does not match proof target before draft PR publishing: "
            f"{', '.join(mismatched_route_platforms)}."
        )
    manifest_path = _proof_manifest_artifact_path(artifact_paths)
    if manifest_path is None:
        return "proof manifest artifact is required before draft PR publishing."
    if not manifest_path.is_file():
        return "proof manifest artifact file is required before draft PR publishing."
    manifest_block_reason = _proof_manifest_base_commit_block_reason(
        manifest_path,
        expected_base_commit=expected_base_commit,
        expected_base_ref=expected_base_ref,
        expected_target=target,
        expected_setup_commands=proof.get("setup_commands"),
        expected_commands=proof.get("commands"),
        expected_required_env_keys=proof.get("required_env_keys"),
        expected_platforms=proof.get("platforms"),
        expected_artifacts=[
            str(path)
            for path in artifact_paths
            if path.name != _PROOF_MANIFEST_NAME
        ],
        expected_branch_name=expected_branch_name,
        expected_worktree=expected_worktree,
        expected_run_id=expected_run_id,
        expected_linear_identifier=expected_linear_identifier,
        expected_linear_url=expected_linear_url,
    )
    if manifest_block_reason:
        return manifest_block_reason
    outside_manifest_dir = _proof_artifacts_outside_manifest_dir(
        artifact_paths,
        manifest_path=manifest_path,
    )
    if outside_manifest_dir:
        return (
            "proof artifacts must stay under proof manifest directory before draft PR publishing: "
            f"{', '.join(outside_manifest_dir)}."
        )

    missing_target_evidence = _missing_target_text_evidence_platforms(
        artifacts=artifact_paths,
        expected_text=str(target.get("expected_text") or ""),
    )
    if missing_target_evidence:
        return (
            "proof target evidence artifacts are required before draft PR publishing: "
            f"missing {', '.join(missing_target_evidence)}."
        )

    missing_shareable = _missing_shareable_proof_platforms(proof.get("shareable_artifacts"))
    if missing_shareable:
        return f"shareable proof links are required before draft PR publishing: missing {', '.join(missing_shareable)}."
    mismatched_shareable = _shareable_proof_platforms_without_local_artifact_match(
        proof.get("shareable_artifacts"),
        artifacts=artifacts,
    )
    if mismatched_shareable:
        return (
            "shareable proof links must match local proof artifacts before draft PR publishing: "
            f"{', '.join(mismatched_shareable)}."
        )
    duplicate_shareable = _shareable_proof_platforms_with_duplicate_urls(proof.get("shareable_artifacts"))
    if duplicate_shareable:
        return (
            "duplicate shareable proof links are not enough before draft PR publishing: "
            f"{', '.join(duplicate_shareable)}."
        )
    return ""


def _missing_local_visual_proof_files(artifacts: list[Path]) -> list[str]:
    missing: list[str] = []
    for platform in ("ios", "android"):
        platform_artifacts = [
            path
            for path in artifacts
            if _proof_artifact_platform(path) == platform
        ]
        if platform_artifacts and not any(path.is_file() for path in platform_artifacts):
            missing.append(platform)
    return missing


def _empty_local_visual_proof_files(artifacts: list[Path]) -> list[str]:
    empty: list[str] = []
    for platform in ("ios", "android"):
        platform_artifacts = [
            path
            for path in artifacts
            if _proof_artifact_platform(path) == platform
        ]
        if platform_artifacts and not any(_path_has_bytes(path) for path in platform_artifacts):
            empty.append(platform)
    return empty


def _path_has_bytes(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _missing_target_text_evidence_platforms(*, artifacts: list[Path], expected_text: str) -> list[str]:
    expected = _normalize_text_for_match(expected_text)
    if not expected:
        return []
    text_artifacts = [
        path
        for path in artifacts
        if path.suffix.lower() in _TEXT_PROOF_SUFFIXES
        and path.name != _PROOF_MANIFEST_NAME
    ]
    missing: list[str] = []
    for platform in ("ios", "android"):
        platform_artifacts = [
            path
            for path in text_artifacts
            if _proof_artifact_platform(path) == platform
        ]
        if not any(
            expected in _normalize_text_for_match(_read_text_proof_artifact(path))
            for path in platform_artifacts
        ):
            missing.append(platform)
    return missing


def _read_text_proof_artifact(path: Path) -> str:
    try:
        return path.read_bytes()[:_TEXT_PROOF_READ_LIMIT_BYTES].decode(
            "utf-8",
            errors="replace",
        )
    except Exception:
        return ""


def _proof_manifest_base_commit_block_reason(
    manifest_path: Path,
    *,
    expected_base_commit: str,
    expected_base_ref: str,
    expected_target: Mapping[str, Any],
    expected_setup_commands: Any,
    expected_commands: Any,
    expected_required_env_keys: Any,
    expected_platforms: Any,
    expected_artifacts: Any,
    expected_branch_name: str,
    expected_worktree: str,
    expected_run_id: str,
    expected_linear_identifier: str,
    expected_linear_url: str,
) -> str:
    expected_commit = str(expected_base_commit or "").strip()
    expected_ref = str(expected_base_ref or "").strip()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return "proof manifest is not readable before draft PR publishing."
    if not isinstance(payload, dict):
        return "proof manifest is not readable before draft PR publishing."
    expected_manifest_run_id = str(expected_run_id or "").strip()
    actual_run_id = str(payload.get("run_id") or "").strip()
    if expected_manifest_run_id and actual_run_id != expected_manifest_run_id:
        return "proof manifest run id does not match Monica run before draft PR publishing."
    actual = str(payload.get("base_commit") or "").strip()
    if expected_commit and actual != expected_commit:
        return "proof manifest base commit does not match worker base commit before draft PR publishing."
    actual_ref = str(payload.get("base_ref") or payload.get("base_branch") or "").strip()
    if expected_ref and actual_ref != expected_ref:
        return "proof manifest base ref does not match worker base ref before draft PR publishing."
    expected_identifier = str(expected_linear_identifier or "").strip()
    actual_identifier = str(payload.get("linear_identifier") or "").strip()
    if expected_identifier and actual_identifier != expected_identifier:
        return "proof manifest Linear issue does not match Monica run before draft PR publishing."
    expected_url = str(expected_linear_url or "").strip()
    actual_url = str(payload.get("linear_url") or "").strip()
    if expected_url and actual_url != expected_url:
        return "proof manifest Linear issue does not match Monica run before draft PR publishing."
    manifest_target = payload.get("proof_target")
    if not isinstance(manifest_target, dict):
        return "proof manifest target does not match proof target before draft PR publishing."
    if _normalized_proof_target(manifest_target) != _normalized_proof_target(expected_target):
        return "proof manifest target does not match proof target before draft PR publishing."
    if _normalized_command_values(payload.get("setup_commands")) != _normalized_command_values(
        expected_setup_commands
    ):
        return "proof manifest setup commands do not match proof setup commands before draft PR publishing."
    if _normalized_command_values(payload.get("commands")) != _normalized_command_values(expected_commands):
        return "proof manifest commands do not match proof commands before draft PR publishing."
    invalid_configured_required_env_keys = _invalid_required_env_keys(
        tuple(_normalized_required_env_key_values(expected_required_env_keys))
    )
    invalid_manifest_required_env_keys = _invalid_required_env_keys(
        tuple(_normalized_required_env_key_values(payload.get("required_env_keys")))
    )
    if invalid_manifest_required_env_keys and not invalid_configured_required_env_keys:
        return (
            "proof manifest required env keys are invalid before draft PR publishing: "
            f"{', '.join(invalid_manifest_required_env_keys)}."
        )
    if _normalized_required_env_key_values(payload.get("required_env_keys")) != _normalized_required_env_key_values(
        expected_required_env_keys
    ):
        return "proof manifest required env keys do not match proof required env keys before draft PR publishing."
    if _normalized_platform_values(payload.get("platforms")) != _normalized_platform_values(expected_platforms):
        return "proof manifest platforms do not match proof platforms before draft PR publishing."
    if _normalized_artifact_values(payload.get("proof_artifacts")) != _normalized_artifact_values(
        expected_artifacts
    ):
        return "proof manifest artifacts do not match proof artifacts before draft PR publishing."
    expected_branch = str(expected_branch_name or "").strip()
    actual_branch = str(payload.get("branch_name") or "").strip()
    if expected_branch and actual_branch != expected_branch:
        return "proof manifest branch does not match worker branch before draft PR publishing."
    expected_worktree_path = str(expected_worktree or "").strip()
    actual_worktree = str(payload.get("worktree") or payload.get("worktree_path") or "").strip()
    if expected_worktree_path and actual_worktree != expected_worktree_path:
        return "proof manifest worktree does not match worker worktree before draft PR publishing."
    return ""


def _normalized_proof_target(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        "deep_link": str(value.get("deep_link") or "").strip(),
        "expected_text": " ".join(str(value.get("expected_text") or "").split()),
        "screen": " ".join(str(value.get("screen") or "").split()),
    }


def _normalize_text_for_match(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _normalized_command_values(value: Any) -> list[str]:
    if isinstance(value, str):
        candidates = (value,)
    elif isinstance(value, (list, tuple)):
        candidates = value
    else:
        return []
    return [" ".join(str(item or "").split()) for item in candidates if str(item).strip()]


def _normalized_required_env_key_values(value: Any) -> list[str]:
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


def _normalized_platform_values(value: Any) -> list[str]:
    if isinstance(value, str):
        candidates = (value,)
    elif isinstance(value, (list, tuple)):
        candidates = value
    else:
        return []
    return sorted({str(item or "").strip().lower() for item in candidates if str(item).strip()})


def _normalized_artifact_values(value: Any) -> list[str]:
    if isinstance(value, str):
        candidates = (value,)
    elif isinstance(value, (list, tuple)):
        candidates = value
    else:
        return []
    return sorted({_proof_path_key(str(item)) for item in candidates if str(item).strip()})


def _valid_proof_deep_link(value: Any) -> bool:
    deep_link = str(value or "").strip()
    if not deep_link or any(char.isspace() for char in deep_link):
        return False
    return "://" in deep_link or deep_link.startswith(("exp+", "http://", "https://"))


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
_LOCAL_EVIDENCE_HOST_SUFFIXES = (".internal", ".lan", ".local")


def _generic_proof_target_block_reason(value: Any) -> str:
    deep_link = str(value or "").strip()
    parsed = urlparse(deep_link)
    if parsed.scheme.casefold() in _EXPO_RUNTIME_PROOF_TARGET_SCHEMES:
        return "Expo runtime proof target is not enough before draft PR publishing."
    if parsed.scheme.casefold() in {"http", "https"} and _is_unshareable_proof_host(parsed.hostname):
        return "local proof target is not enough before draft PR publishing."
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
        return "Expo Dev Client proof target is not enough before draft PR publishing."
    if not tokens or all(token in _GENERIC_PROOF_TARGET_TOKENS for token in tokens):
        return "generic proof target is not enough before draft PR publishing."
    if any(token in _AUTH_PROOF_TARGET_TOKENS for token in tokens):
        return "auth/onboarding proof target is not enough before draft PR publishing."
    return ""


def _proof_target_mismatch_reason(
    *,
    target: Mapping[str, Any],
    expected_target: Mapping[str, str] | None,
) -> str:
    if not expected_target:
        return ""
    expected_deep_link = str(expected_target.get("deep_link") or "").strip()
    actual_deep_link = str(target.get("deep_link") or "").strip()
    if expected_deep_link and actual_deep_link != expected_deep_link:
        return "proof target does not match the worker-requested deep link before draft PR publishing."
    expected_text = " ".join(str(expected_target.get("expected_text") or "").split())
    actual_text = " ".join(str(target.get("expected_text") or "").split())
    if expected_text and actual_text != expected_text:
        return "proof target does not match the worker-requested expected text before draft PR publishing."
    expected_screen = " ".join(str(expected_target.get("screen") or "").split())
    actual_screen = " ".join(str(target.get("screen") or "").split())
    if expected_screen and actual_screen != expected_screen:
        return "proof target does not match the worker-requested screen before draft PR publishing."
    return ""


def _missing_shareable_proof_platforms(value: Any) -> list[str]:
    refs = value if isinstance(value, (list, tuple)) else []
    missing: list[str] = []
    for platform in ("ios", "android"):
        found = False
        for item in refs:
            if not isinstance(item, dict):
                continue
            url = _safe_proof_shareable_url(item.get("url") or item.get("permalink") or "")
            if not url:
                continue
            ref_platform = str(item.get("platform") or "").strip().lower()
            path = str(item.get("path") or "").strip()
            if ref_platform == platform or (path and _proof_artifact_platform(Path(path)) == platform):
                found = True
                break
        if not found:
            missing.append(platform)
    return missing


def _shareable_proof_platforms_without_local_artifact_match(
    value: Any,
    *,
    artifacts: list[Path],
) -> list[str]:
    refs = value if isinstance(value, (list, tuple)) else []
    local_paths = _visual_proof_artifact_paths_by_platform(artifacts)
    mismatched: list[str] = []
    for platform in ("ios", "android"):
        platform_paths = local_paths.get(platform, set())
        if not platform_paths:
            continue
        if not any(
            _shareable_proof_ref_matches_local_artifact(item, platform, platform_paths)
            for item in refs
            if isinstance(item, dict)
        ):
            mismatched.append(platform)
    return mismatched


def _shareable_proof_platforms_with_duplicate_urls(value: Any) -> list[str]:
    refs = value if isinstance(value, (list, tuple)) else []
    urls_by_platform: dict[str, str] = {}
    duplicates: list[str] = []
    for platform in ("ios", "android"):
        url = ""
        for item in refs:
            if not isinstance(item, dict):
                continue
            candidate_url = _safe_proof_shareable_url(item.get("url") or item.get("permalink") or "")
            if not candidate_url:
                continue
            ref_platform = str(item.get("platform") or "").strip().lower()
            path = str(item.get("path") or "").strip()
            if ref_platform == platform or (path and _proof_artifact_platform(Path(path)) == platform):
                url = candidate_url
                break
        if not url:
            continue
        if url in urls_by_platform.values():
            duplicates.append(platform)
        urls_by_platform[platform] = url
    return duplicates


def _shareable_proof_ref_matches_local_artifact(
    item: Mapping[str, Any],
    platform: str,
    local_paths: set[str],
) -> bool:
    url = _safe_proof_shareable_url(item.get("url") or item.get("permalink") or "")
    if not url:
        return False
    path = _proof_path_key(str(item.get("path") or ""))
    if not path or path not in local_paths:
        return False
    ref_platform = str(item.get("platform") or "").strip().lower()
    return not ref_platform or ref_platform == platform


def _visual_proof_artifact_paths_by_platform(artifacts: list[Path]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {"ios": set(), "android": set()}
    for path in artifacts:
        platform = _proof_artifact_platform(path)
        if platform in result:
            result[platform].add(_proof_path_key(str(path)))
    return result


def _proof_path_key(value: str) -> str:
    path = str(value or "").strip()
    return str(Path(path)) if path else ""


def _worktree_has_git_changes(worktree_path: Path, *, base_branch: str = "main") -> bool | None:
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(worktree_path),
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    if proc.stdout.strip():
        return True

    base_ref = configured_remote_base_ref(base_branch)
    try:
        diff = subprocess.run(
            ["git", "diff", "--name-only", f"{base_ref}...HEAD"],
            cwd=str(worktree_path),
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if diff.returncode != 0:
        return False
    return bool(diff.stdout.strip())


def _without_empty_sections(parts: list[str]) -> list[str]:
    cleaned: list[str] = []
    for index, part in enumerate(parts):
        if part != "":
            cleaned.append(part)
            continue
        previous = parts[index - 1] if index > 0 else ""
        next_part = parts[index + 1] if index + 1 < len(parts) else ""
        if previous == "## Evidence" or next_part == "## Verification":
            continue
        cleaned.append(part)
    return cleaned


def _thread_context_lines(thread: dict[str, Any]) -> str:
    detail_lines = []
    for item in thread.get("message_details") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        user = str(item.get("user_id") or "unknown").strip() or "unknown"
        ts = str(item.get("ts") or "").strip()
        permalink = str(item.get("permalink") or "").strip()
        prefix = f"{user} at {ts}" if ts else user
        suffix = f" ({permalink})" if permalink else ""
        detail_lines.append(f"- {prefix}: {text}{suffix}")
    if detail_lines:
        return "\n".join(detail_lines)

    messages = thread.get("messages") or []
    fallback = "\n".join(f"- {message}" for message in messages if str(message).strip())
    return fallback or "- No thread context captured."
