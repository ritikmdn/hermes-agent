from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import MonicaConfig, runtime_root
from .codex_worker import build_code_worker
from .intent import IntentClassifier
from .linear_client import LinearAttachmentPayload, LinearClient, LinearIssuePayload
from .pr_publisher import DraftPrPublisher
from .proof import ProofRunner
from .repo_manager import RepoManager
from .secrets import monica_slack_bot_token
from .slack_client import SlackClientError, SlackThreadClient
from .state import MonicaState
from .verifier import VerificationRunner

logger = logging.getLogger(__name__)


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
        thread = self.read_slack_thread(run)
        worker_result = worker.run(
            run=run,
            worktree=worktree,
            brief=self._fix_brief(run=run, worktree=worktree, thread=thread),
        )
        result = dict(worker_result)
        result["branch_name"] = worktree.branch_name
        result["worktree_path"] = str(worktree.path)
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
            proof_target=_proof_target_from_worker_result(worker_result),
        )
        return result.to_dict()

    def open_draft_pr(
        self,
        run: Any,
        worker_result: dict[str, Any],
        verification: dict[str, Any],
    ) -> dict[str, Any]:
        publisher = self._pr_publisher or DraftPrPublisher()
        branch_name = str(worker_result.get("branch_name") or run.branch_name or "")
        worktree_path = str(worker_result.get("worktree_path") or worker_result.get("worktree") or "")
        title = f"[{run.linear_identifier or 'Monica'}] {run.request_text.splitlines()[0][:90]}"
        body = self._pr_body(run=run, worker_result=worker_result, verification=verification)
        url = publisher.publish(
            worktree=worktree_path,
            branch_name=branch_name,
            base_branch=self.config.repo.default_branch,
            title=title,
            body=body,
        )
        return {"url": url}

    def post_status(self, run: Any, text: str) -> None:
        client = self._resolve_slack_client(run)
        if client is None:
            return
        try:
            client.post_thread_reply(channel_id=run.channel_id, thread_ts=run.thread_ts, text=text)
        except Exception as exc:
            logger.warning(
                "Slack status post failed run_id=%s channel_id=%s thread_ts=%s error=%s",
                getattr(run, "id", ""),
                getattr(run, "channel_id", ""),
                getattr(run, "thread_ts", ""),
                exc,
            )
            return

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
        return "\n".join(
            [
                "# Monica Fix Brief",
                "",
                f"Run ID: {run.id}",
                f"Linear: {run.linear_identifier or 'unlinked'}",
                f"Linear URL: {run.linear_url or 'unavailable'}",
                f"Branch: {worktree.branch_name}",
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
                "",
                "## Instructions",
                (
                    "Investigate the React Native mobile app bug, make the smallest safe fix, "
                    "and leave the worktree ready for verification. If the app exposes a safe "
                    "dev-client deep link or route for the affected screen, include it in the "
                    "worker summary as `Monica proof deep link: <url>` so simulator proof can "
                    "exercise the right page."
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
        return "\n".join(
            _without_empty_sections(
                [
                    "## Monica Summary",
                    str(worker_result.get("summary") or "Monica completed the requested fix."),
                    "",
                    "## Links",
                    f"- Linear: {run.linear_url or run.linear_identifier or 'unavailable'}",
                    f"- Slack: {slack_value}",
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
    scheme = urlparse(text).scheme.lower()
    return text if scheme in {"http", "https"} else ""


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
    target = proof.get("proof_target")
    if isinstance(target, dict):
        deep_link = str(target.get("deep_link") or "").strip()
        if deep_link:
            lines.append(f"Target: {deep_link}")
    artifacts = [str(path).strip() for path in proof.get("artifacts") or [] if str(path).strip()]
    if artifacts:
        lines.extend(f"- {path}" for path in artifacts)
    return "\n".join(lines)


def _proof_target_from_worker_result(worker_result: dict[str, Any]) -> dict[str, str]:
    deep_link = str(
        worker_result.get("proof_deep_link")
        or worker_result.get("deep_link")
        or ""
    ).strip()
    target: dict[str, str] = {}
    if deep_link:
        target["deep_link"] = deep_link
    return target


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

    base_ref = f"origin/{(base_branch or 'main').strip() or 'main'}"
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
