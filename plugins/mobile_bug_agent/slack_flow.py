from __future__ import annotations

import logging
import re
import shutil
from collections.abc import Callable
from typing import Any

from gateway.config import Platform

from .config import MonicaConfig
from .readiness import check_monica_readiness
from .state import MonicaState

_SLACK_MENTION_RE = re.compile(r"<@([A-Z0-9_]+)(?:\|[^>]+)?>")
_SLACK_USER_ID_RE = re.compile(r"^[UW][A-Z0-9_]{2,}$")
_SLACK_CHANNEL_ID_RE = re.compile(r"^[CDG][A-Z0-9_]{2,}$")
_SLACK_BLOCK_KIT_PAYLOAD_MARKER = "[Slack Block Kit payload for this message]"
_CHATGPT_FOOTER_RE = re.compile(r"\s*\*Sent using\*\s+ChatGPT\s*$", re.I)
logger = logging.getLogger(__name__)
_APPROVAL_RE = re.compile(
    r"\b("
    r"approved"
    r"|approve"
    r"|approval granted"
    r"|(?:yes|yep|yeah|sure|ok|okay),?\s+(?:fix it|start(?: the fix)?|take(?: the fix| it)?|open a draft pr|create a draft pr)"
    r"|go ahead"
    r"|proceed"
    r"|start the fix"
    r"|take the fix"
    r"|take it"
    r"|open a draft pr"
    r"|create a draft pr"
    r"|ship it"
    r")\b",
    re.I,
)
_NEGATED_APPROVAL_RE = re.compile(
    r"\b("
    r"not approved"
    r"|not approve"
    r"|do not approve"
    r"|don't approve"
    r"|not go ahead"
    r"|do not go ahead"
    r"|don't go ahead"
    r"|not proceed"
    r"|do not proceed"
    r"|don't proceed"
    r"|do not start the fix"
    r"|don't start the fix"
    r"|do not take the fix"
    r"|don't take the fix"
    r"|do not ship it"
    r"|don't ship it"
    r"|not ship it"
    r")\b",
    re.I,
)
_QUESTION_APPROVAL_RE = re.compile(
    r"\b("
    r"should\s+i\s+approve"
    r"|should\s+we\s+approve"
    r"|should\s+you\s+approve"
    r"|can\s+i\s+approve"
    r"|can\s+we\s+approve"
    r"|can\s+you\s+approve"
    r"|do\s+i\s+approve"
    r"|do\s+we\s+approve"
    r"|would\s+you\s+approve"
    r"|is\s+this\s+approved"
    r")\b",
    re.I,
)
_APPROVAL_PHRASE_QUESTION_RE = re.compile(
    r"\b("
    r"approve"
    r"|approved"
    r"|go ahead"
    r"|proceed"
    r"|start(?: the fix)?"
    r"|take(?: the fix| it)?"
    r"|open a draft pr"
    r"|create a draft pr"
    r"|ship it"
    r")\s*\?\s*$",
    re.I,
)
_CANCEL_RE = re.compile(
    r"("
    r"^\s*(?:cancel|stop)\s*[.!?]*$"
    r"|^\s*(?:please\s+)?(?:cancel|stop)\s+"
    r"(?:monica|it|for now|this\s+(?:run|loop|fix)|the\s+(?:run|loop|fix))\s*[.!?]*$"
    r"|\b(?:do not fix|don't fix|hold off)\b"
    r")",
    re.I,
)
_CANCEL_QUESTION_RE = re.compile(r"^\s*(?:cancel|stop)\s*\?\s*$", re.I)
_NEGATED_CANCEL_RE = re.compile(
    r"\b("
    r"not cancel"
    r"|not stop"
    r"|do not cancel"
    r"|do not stop"
    r"|don't cancel"
    r"|don't stop"
    r")\b",
    re.I,
)
_NOOP_RE = re.compile(
    r"\b("
    r"thanks?"
    r"|thank you"
    r"|fixed now"
    r"|resolved"
    r"|all good"
    r"|looks good"
    r")\b",
    re.I,
)


def is_approval_text(text: str) -> bool:
    value = str(text or "")
    if _NEGATED_APPROVAL_RE.search(value):
        return False
    if (
        _QUESTION_APPROVAL_RE.search(value)
        or _APPROVAL_PHRASE_QUESTION_RE.search(value)
        or (value.strip().endswith("?") and _APPROVAL_RE.search(value))
    ):
        return False
    return bool(_APPROVAL_RE.search(value))
_TERMINAL_NOOP_RE = re.compile(
    r"\b("
    r"fixed now"
    r"|resolved"
    r"|all good"
    r"|looks good"
    r")\b",
    re.I,
)
_ACTION_HINT_RE = re.compile(
    r"\b("
    r"crash"
    r"|bug"
    r"|issue"
    r"|repro"
    r"|reproduce"
    r"|regression"
    r"|broken"
    r"|failing"
    r"|still"
    r"|again"
    r"|update"
    r"|linear"
    r"|ticket"
    r"|pr"
    r")\b",
    re.I,
)
_NEW_WORK_HINT_RE = re.compile(
    r"\b("
    r"still"
    r"|again"
    r"|update"
    r"|repro"
    r"|reproduce"
    r"|reproduction"
    r"|detail"
    r"|context"
    r"|screenshot"
    r"|attachment"
    r"|attached"
    r"|regression"
    r"|broken"
    r"|failing"
    r"|not\s+fixed"
    r"|isn'?t\s+fixed"
    r"|is\s+not\s+fixed"
    r"|linear"
    r"|ticket"
    r"|pr"
    r")\b",
    re.I,
)


class MonicaSlackFlow:
    def __init__(
        self,
        *,
        config: MonicaConfig,
        state: MonicaState,
        loop_launcher: Callable[[str], None],
        approval_readiness_checker: Callable[[], tuple[bool, str]] | None = None,
    ) -> None:
        self.config = config
        self.state = state
        self.loop_launcher = loop_launcher
        self.approval_readiness_checker = (
            approval_readiness_checker or self._default_approval_readiness
        )

    def handle_gateway_event(self, event: Any) -> dict[str, str] | None:
        source = getattr(event, "source", None)
        if source is None:
            return None

        platform = getattr(source, "platform", None)
        if platform != Platform.SLACK:
            return None

        channel_id = str(getattr(source, "chat_id", "") or "")
        allowed = set(self.config.slack.allowed_channels)

        if getattr(source, "is_bot", False):
            return None

        raw = getattr(event, "raw_message", None)
        if isinstance(raw, dict):
            subtype = str(raw.get("subtype") or "")
            if subtype in {"bot_message", "message_deleted", "message_changed"}:
                return None

        if not self._is_monica_mention(event):
            return None

        if not self.config.enabled:
            return {"action": "skip", "reason": "monica_disabled"}

        if allowed:
            invalid_channels = self._invalid_allowed_channels()
            if invalid_channels:
                return {
                    "action": "skip_reply",
                    "reason": "monica_allowed_channels_invalid",
                    "text": (
                        "I cannot start Monica here yet. "
                        "mobile_bug_agent.slack.allowed_channels must contain Slack channel IDs "
                        f"like C123 or G123, not names like {invalid_channels[0]}."
                    ),
                }

        is_direct_message = self._is_direct_message(event)
        if allowed and channel_id not in allowed and not is_direct_message:
            return {
                "action": "skip_reply",
                "reason": "monica_channel_not_allowed",
                "text": (
                    "I cannot run Monica in this channel. "
                    "Ask an admin to add this channel to mobile_bug_agent.slack.allowed_channels."
                ),
            }

        if self.config.rollout_mode in {"linear_only", "local_fix_only", "approved_pr"} and not allowed:
            return {
                "action": "skip_reply",
                "reason": "monica_allowed_channels_required",
                "text": (
                    "I cannot start Monica here yet. "
                    "Configure mobile_bug_agent.slack.allowed_channels before enabling real side effects."
                ),
            }

        if self.config.rollout_mode in {"linear_only", "local_fix_only", "approved_pr"} and not self.config.slack.bot_user_ids:
            return {
                "action": "skip_reply",
                "reason": "monica_bot_user_ids_required",
                "text": (
                    "I cannot start Monica here yet. "
                    "Configure mobile_bug_agent.slack.bot_user_ids with Monica's Slack user ID "
                    "before enabling real side effects."
                ),
            }
        if self.config.rollout_mode in {"linear_only", "local_fix_only", "approved_pr"}:
            bot_id_values = self._bot_id_user_ids()
            if bot_id_values:
                return {
                    "action": "skip_reply",
                    "reason": "monica_bot_user_ids_invalid",
                    "text": (
                        "I cannot start Monica here yet. "
                        "mobile_bug_agent.slack.bot_user_ids must contain Slack mention user IDs "
                        f"like U123, not bot_id values like {bot_id_values[0]}."
                    ),
                }
            handle_style_bot_ids = [
                user_id
                for user_id in self.config.slack.bot_user_ids
                if str(user_id).strip().startswith("@")
            ]
            if handle_style_bot_ids:
                return {
                    "action": "skip_reply",
                    "reason": "monica_bot_user_ids_invalid",
                    "text": (
                        "I cannot start Monica here yet. "
                        "mobile_bug_agent.slack.bot_user_ids must contain Slack mention user IDs "
                        f"like U123, not handles like {handle_style_bot_ids[0]}."
                    ),
                }
            invalid_bot_user_ids = self._invalid_bot_user_ids()
            if invalid_bot_user_ids:
                return {
                    "action": "skip_reply",
                    "reason": "monica_bot_user_ids_invalid",
                    "text": (
                        "I cannot start Monica here yet. "
                        "mobile_bug_agent.slack.bot_user_ids must contain Slack mention user IDs "
                        f"like U123, not invalid values like {invalid_bot_user_ids[0]}."
                    ),
                }

        request_text = self._request_text(event)
        thread_ts = self._thread_ts(event)
        message_ts = str(getattr(event, "message_id", "") or "")
        if isinstance(raw, dict):
            message_ts = str(raw.get("ts") or message_ts)

        existing = self.state.find_run(
            platform="slack",
            channel_id=channel_id,
            thread_ts=thread_ts,
        )
        if existing is not None:
            user_id = str(getattr(source, "user_id", "") or "")
            if self._is_cancel(request_text):
                if existing.status not in {"done", "blocked", "failed"}:
                    cancelled = self.state.update_run(
                        existing.id,
                        status="blocked",
                        failure_reason=f"cancelled by {user_id or 'unknown Slack user'}",
                    )
                    _log_slack_flow("cancelled", cancelled, cancelled_by=user_id)
                return {"action": "skip", "reason": "monica_loop_cancelled"}
            if existing.status == "awaiting_fix_approval":
                if self._is_approval(request_text) and self._is_allowed_approver(user_id):
                    ready, reason = self.approval_readiness_checker()
                    if not ready:
                        return {
                            "action": "skip_reply",
                            "reason": "monica_loop_approval_not_ready",
                            "text": self._approval_not_ready_text(reason),
                        }
                    approve_once = getattr(self.state, "approve_fix_once", None)
                    if callable(approve_once):
                        approved, changed = approve_once(existing.id, approved_by_user_id=user_id)
                    else:
                        approved = self.state.approve_fix(existing.id, approved_by_user_id=user_id)
                        changed = True
                    if not changed:
                        return {"action": "skip", "reason": "monica_loop_already_active"}
                    _log_slack_flow("approved", approved, approved_by=user_id)
                    self.loop_launcher(approved.id)
                    return {"action": "skip", "reason": "monica_loop_approved"}
                if self._is_approval(request_text):
                    return {
                        "action": "skip_reply",
                        "reason": "monica_loop_approval_denied",
                        "text": self._approval_denied_text(),
                    }
                if self._is_context_update_retag(request_text):
                    rerun = self._requeue_existing_run(
                        existing=existing,
                        raw=raw,
                        message_ts=message_ts,
                        user_id=user_id,
                        request_text=request_text,
                    )
                    self.loop_launcher(rerun.id)
                    return {"action": "skip", "reason": "monica_loop_requeued"}
            if existing.status in {"done", "blocked", "failed", "needs_clarification"}:
                if (
                    existing.status in {"done", "blocked", "failed"}
                    and existing.pr_url
                    and self._is_approval(request_text)
                ):
                    return {"action": "skip", "reason": "monica_loop_already_done"}
                if (
                    existing.status in {"done", "blocked", "failed"}
                    and not existing.pr_url
                    and self.config.rollout_mode in {"local_fix_only", "approved_pr"}
                    and self._has_linear_issue(existing)
                    and self._is_approval(request_text)
                ):
                    if not self._is_allowed_approver(user_id):
                        return {
                            "action": "skip_reply",
                            "reason": "monica_loop_approval_denied",
                            "text": self._approval_denied_text(),
                        }
                    ready, reason = self.approval_readiness_checker()
                    if not ready:
                        return {
                            "action": "skip_reply",
                            "reason": "monica_loop_approval_not_ready",
                            "text": self._approval_not_ready_text(reason),
                        }
                    approved = self.state.update_run(
                        existing.id,
                        status="approved",
                        approved_by_user_id=user_id,
                        failure_reason="",
                        branch_name="",
                        base_branch="",
                        base_commit="",
                        proof_deep_link="",
                        proof_expected_text="",
                        proof_screen="",
                        pr_url="",
                    )
                    _log_slack_flow("approved", approved, approved_by=user_id)
                    self.loop_launcher(approved.id)
                    return {"action": "skip", "reason": "monica_loop_approved"}
                if self._is_noop_retag(request_text):
                    return {"action": "skip", "reason": "monica_loop_noop"}
                rerun = self._requeue_existing_run(
                    existing=existing,
                    raw=raw,
                    message_ts=message_ts,
                    user_id=user_id,
                    request_text=request_text,
                )
                self.loop_launcher(rerun.id)
                return {"action": "skip", "reason": "monica_loop_requeued"}
            return {"action": "skip", "reason": "monica_loop_already_active"}

        create_once = getattr(self.state, "create_run_once", None)
        if callable(create_once):
            run, created = create_once(
                platform="slack",
                channel_id=channel_id,
                thread_ts=thread_ts,
                message_ts=message_ts,
                user_id=str(getattr(source, "user_id", "") or ""),
                request_text=request_text,
                raw_event=raw if isinstance(raw, dict) else {},
            )
        else:
            run = self.state.create_run(
                platform="slack",
                channel_id=channel_id,
                thread_ts=thread_ts,
                message_ts=message_ts,
                user_id=str(getattr(source, "user_id", "") or ""),
                request_text=request_text,
                raw_event=raw if isinstance(raw, dict) else {},
            )
            created = True

        if not created:
            return {"action": "skip", "reason": "monica_loop_already_active"}

        self.loop_launcher(run.id)
        return {"action": "skip", "reason": "monica_loop_queued"}

    def _is_monica_mention(self, event: Any) -> bool:
        if self._is_direct_message(event):
            return True

        raw_text = self._raw_text(event)
        raw = getattr(event, "raw_message", None)
        is_app_mention = isinstance(raw, dict) and str(raw.get("type") or "") == "app_mention"
        configured_ids = set(self.config.slack.bot_user_ids)
        if configured_ids:
            mentioned_ids = set(_SLACK_MENTION_RE.findall(raw_text))
            if configured_ids & mentioned_ids:
                return True
            if self.config.rollout_mode in {"linear_only", "local_fix_only", "approved_pr"}:
                if (
                    self._bot_id_user_ids()
                    or self._invalid_bot_user_ids()
                    or self._handle_style_bot_user_ids()
                ):
                    return is_app_mention
                return False
            return False

        return is_app_mention

    def _is_direct_message(self, event: Any) -> bool:
        source = getattr(event, "source", None)
        raw = getattr(event, "raw_message", None)
        if isinstance(raw, dict):
            channel_type = str(raw.get("channel_type") or "")
            if channel_type == "im":
                return True
            if channel_type == "mpim":
                return False

        channel_id = str(getattr(source, "chat_id", "") or "")
        chat_type = str(getattr(source, "chat_type", "") or "")
        return chat_type == "dm" and channel_id.startswith("D")

    def _invalid_bot_user_ids(self) -> list[str]:
        return [
            user_id
            for user_id in self.config.slack.bot_user_ids
            if not str(user_id).strip().startswith("@")
            and not str(user_id).upper().startswith("B")
            and not _is_slack_user_id(user_id)
        ]

    def _handle_style_bot_user_ids(self) -> list[str]:
        return [
            user_id
            for user_id in self.config.slack.bot_user_ids
            if str(user_id).strip().startswith("@")
        ]

    def _bot_id_user_ids(self) -> list[str]:
        return [
            user_id
            for user_id in self.config.slack.bot_user_ids
            if str(user_id).upper().startswith("B")
        ]

    def _invalid_allowed_channels(self) -> list[str]:
        return [
            channel
            for channel in self.config.slack.allowed_channels
            if not _is_slack_channel_id(channel)
        ]

    def _request_text(self, event: Any) -> str:
        text = str(getattr(event, "text", "") or "").strip()
        if text:
            return _clean_request_text(text)
        raw_text = self._raw_text(event)
        return _clean_request_text(raw_text)

    def _raw_text(self, event: Any) -> str:
        raw = getattr(event, "raw_message", None)
        if isinstance(raw, dict):
            return str(raw.get("text") or "")
        return str(getattr(event, "text", "") or "")

    def _thread_ts(self, event: Any) -> str:
        source = getattr(event, "source", None)
        raw = getattr(event, "raw_message", None)
        if isinstance(raw, dict):
            thread_ts = str(raw.get("thread_ts") or raw.get("ts") or "")
            if thread_ts:
                return thread_ts
        return str(getattr(source, "thread_id", "") or getattr(event, "message_id", "") or "")

    def _is_allowed_approver(self, user_id: str) -> bool:
        allowed = set(self.config.slack.approver_user_ids)
        return bool(allowed) and user_id in allowed

    @staticmethod
    def _has_linear_issue(run: Any) -> bool:
        return bool(
            str(getattr(run, "linear_identifier", "") or "").strip()
            or str(getattr(run, "linear_issue_id", "") or "").strip()
            or str(getattr(run, "linear_url", "") or "").strip()
        )

    def _approval_denied_text(self) -> str:
        approvers = [f"<@{user_id}>" for user_id in self.config.slack.approver_user_ids if user_id]
        if not approvers:
            return (
                "I cannot start the fix from this approval. "
                "No Monica approver is configured for code changes."
            )
        return (
            "I cannot start the fix from this approval. "
            "A configured Monica approver must tag me to approve code changes. "
            f"Allowed approvers: {', '.join(approvers)}."
        )

    def _approval_not_ready_text(self, reason: str) -> str:
        mode_label = "approved-PR mode" if self.config.rollout_mode == "approved_pr" else "code-fix mode"
        clean_reason = str(reason or "").strip() or "code rollout is not ready"
        return (
            "I cannot start the fix from this approval because Monica is not ready "
            f"for {mode_label}: {clean_reason}"
        )

    def _default_approval_readiness(self) -> tuple[bool, str]:
        if self.config.rollout_mode not in {"local_fix_only", "approved_pr"}:
            return True, ""
        report = check_monica_readiness(config=self.config, which=shutil.which)
        if report.ready:
            return True, ""
        return False, report.first_approval_failure()

    def _requeue_existing_run(
        self,
        *,
        existing: Any,
        raw: Any,
        message_ts: str,
        user_id: str,
        request_text: str,
    ) -> Any:
        rerun = self.state.update_run(
            existing.id,
            status="queued",
            message_ts=message_ts,
            user_id=user_id,
            request_text=request_text,
            raw_event=raw if isinstance(raw, dict) else {},
            branch_name="",
            base_branch="",
            base_commit="",
            proof_deep_link="",
            proof_expected_text="",
            proof_screen="",
            pr_url="",
            failure_reason="",
            approved_by_user_id="",
        )
        _log_slack_flow("requeued", rerun, requeued_by=user_id)
        return rerun

    @staticmethod
    def _is_approval(text: str) -> bool:
        return is_approval_text(text)

    @staticmethod
    def _is_cancel(text: str) -> bool:
        if _NEGATED_CANCEL_RE.search(text):
            return False
        if _CANCEL_QUESTION_RE.search(text) or (text.strip().endswith("?") and _CANCEL_RE.search(text)):
            return False
        return bool(_CANCEL_RE.search(text))

    @staticmethod
    def _is_noop_retag(text: str) -> bool:
        if not _NOOP_RE.search(text):
            return False
        if _TERMINAL_NOOP_RE.search(text):
            return not _NEW_WORK_HINT_RE.search(text)
        return not _ACTION_HINT_RE.search(text)

    @staticmethod
    def _is_context_update_retag(text: str) -> bool:
        if (
            _NEGATED_APPROVAL_RE.search(text)
            or _NEGATED_CANCEL_RE.search(text)
            or _QUESTION_APPROVAL_RE.search(text)
            or _APPROVAL_PHRASE_QUESTION_RE.search(text)
            or _APPROVAL_RE.search(text)
        ):
            return False
        return bool(_NEW_WORK_HINT_RE.search(text))


def _clean_request_text(text: str) -> str:
    clean = str(text or "").strip()
    marker_index = clean.find(_SLACK_BLOCK_KIT_PAYLOAD_MARKER)
    if marker_index >= 0:
        clean = clean[:marker_index].rstrip()
    clean = _CHATGPT_FOOTER_RE.sub("", clean).strip()
    return _SLACK_MENTION_RE.sub("", clean).strip()


def _log_slack_flow(
    event: str,
    run: Any,
    *,
    cancelled_by: str = "",
    approved_by: str = "",
    requeued_by: str = "",
) -> None:
    logger.info(
        "monica_slack_flow event=%s run_id=%s status=%s channel_id=%s thread_ts=%s "
        "linear_identifier=%s linear_url=%s branch_name=%s pr_url=%s failure_reason=%s "
        "cancelled_by=%s approved_by=%s requeued_by=%s",
        event,
        getattr(run, "id", ""),
        getattr(run, "status", ""),
        getattr(run, "channel_id", ""),
        getattr(run, "thread_ts", ""),
        getattr(run, "linear_identifier", ""),
        getattr(run, "linear_url", ""),
        getattr(run, "branch_name", ""),
        getattr(run, "pr_url", ""),
        getattr(run, "failure_reason", ""),
        cancelled_by,
        approved_by,
        requeued_by,
    )


def _is_slack_user_id(value: str) -> bool:
    return bool(_SLACK_USER_ID_RE.fullmatch(str(value or "").strip()))


def _is_slack_channel_id(value: str) -> bool:
    return bool(_SLACK_CHANNEL_ID_RE.fullmatch(str(value or "").strip()))
