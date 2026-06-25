"""Monica mobile bug agent plugin.

Monica is intentionally mention-gated and loop-driven: the Slack mention wakes
the agentic workflow, then the workflow decides the next useful step.
"""

from __future__ import annotations

import logging
import os
import re
import socket
import subprocess
import threading
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from gateway.config import Platform

from .config import load_monica_config, runtime_root
from .cli import mobile_bug_agent_command, register_cli
from .loop import MonicaLoop
from .skills import DefaultMonicaSkills
from .slack_flow import MonicaSlackFlow
from .state import MonicaState

logger = logging.getLogger(__name__)
_SLACK_MENTION_RE = re.compile(r"<@([A-Z0-9_]+)(?:\|[^>]+)?>")
_GIT_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


@lru_cache(maxsize=1)
def _runtime() -> MonicaSlackFlow:
    config = load_monica_config()
    state_path = runtime_root(config) / "state.sqlite"
    state = MonicaState.open(state_path)
    skills = DefaultMonicaSkills(config=config, state=state)
    loop = MonicaLoop(config=config, state=state, skills=skills)

    def launch(run_id: str) -> None:
        thread = threading.Thread(
            target=_run_loop_safely,
            args=(loop, run_id),
            name=f"monica-loop-{run_id[:8]}",
            daemon=True,
        )
        thread.start()

    return MonicaSlackFlow(config=config, state=state, loop_launcher=launch)


def _run_loop_safely(loop: MonicaLoop, run_id: str) -> None:
    try:
        loop.run(run_id)
    except Exception as exc:  # pragma: no cover - defensive gateway isolation
        logger.warning("Monica loop %s failed: %s", run_id, exc, exc_info=True)
        try:
            loop.state.update_run(run_id, status="failed", failure_reason=str(exc))
        except Exception:
            logger.debug("Failed to mark Monica run %s failed", run_id, exc_info=True)


def _on_pre_gateway_dispatch(event: Any, gateway: Any = None, **_: Any) -> dict[str, Any] | None:
    try:
        return _runtime().handle_gateway_event(event)
    except Exception as exc:  # pragma: no cover - defensive gateway isolation
        _runtime.cache_clear()
        logger.warning("Monica gateway hook unavailable: %s", exc, exc_info=True)
        if _is_slack_monica_mention_fallback(event):
            return {
                "action": "skip_reply",
                "reason": "monica_runtime_unavailable",
                "text": (
                    "Monica could not start because her runtime is not configured correctly. "
                    "Run `hermes mobile-bug-agent doctor` on the host for details."
                ),
            }
        return None


def _on_gateway_startup(gateway: Any = None, **_: Any) -> dict[str, str]:
    thread = threading.Thread(
        target=_recover_runtime_on_gateway_startup,
        name="monica-gateway-startup-recovery",
        daemon=True,
    )
    thread.start()
    return {"action": "started"}


def _recover_runtime_on_gateway_startup() -> None:
    try:
        flow = _runtime()
        _recover_on_gateway_startup(
            config=flow.config,
            state=flow.state,
            loop_launcher=flow.loop_launcher,
        )
    except Exception:
        logger.warning("Monica gateway startup recovery failed", exc_info=True)


def _recover_on_gateway_startup(
    *,
    config: Any,
    state: MonicaState,
    loop_launcher: Any,
    sync_approvals: Any | None = None,
    now: str | float | datetime | None = None,
) -> dict[str, Any]:
    state.reap_stale_runtime_sync_lease(now=now)
    if not state.runtime_sync_gate().get("open", True):
        return {"skipped": "runtime_sync_in_progress", "launched": 0}
    approval_sync_exit_code: int | None = None
    if any(run.status == "awaiting_fix_approval" for run in state.list_runs(limit=100)):
        if sync_approvals is None:
            from .cli import run_sync_approvals_command

            sync_approvals = run_sync_approvals_command
        try:
            approval_sync_exit_code = int(
                sync_approvals(
                    config=config,
                    state=state,
                    limit=50,
                )
            )
        except Exception as exc:
            logger.warning("Monica startup approval sync failed: %s", exc, exc_info=True)
            approval_sync_exit_code = 1
    launched = _recover_pending_runs_on_gateway_startup(
        state=state,
        loop_launcher=loop_launcher,
    )
    result: dict[str, Any] = dict(launched)
    if approval_sync_exit_code is not None:
        result["approval_sync_exit_code"] = approval_sync_exit_code
    return result


def _recover_pending_runs_on_gateway_startup(
    *,
    state: MonicaState,
    loop_launcher: Any,
    now: float | None = None,
) -> dict[str, int]:
    state.reap_stale_loop_leases(now=now)
    launched = 0
    candidates = [
        run
        for run in state.list_runs(limit=100)
        if run.status in {"approved", "queued", "triaging"}
        and state.current_loop_lease(run.id) is None
    ]
    candidates.sort(key=lambda run: (run.status != "approved", run.created_at, run.id))
    for run in candidates:
        if run.status == "triaging":
            state.update_run(run.id, status="queued", failure_reason="")
        loop_launcher(run.id)
        launched += 1
    return {"launched": launched}


def _on_pre_update(project_root: str = "", **_: Any) -> dict[str, str] | None:
    try:
        config = load_monica_config()
        state = MonicaState.open(runtime_root(config) / "state.sqlite")
        now = _utc_now_iso()
        state.reap_stale_runtime_sync_lease(now=now)
        active_runs = state.list_runtime_sync_blocking_runs()
    except Exception as exc:
        logger.debug("Monica pre-update idle check unavailable: %s", exc, exc_info=True)
        return {
            "action": "block",
            "message": (
                "Monica idle state is unavailable, so `hermes update` was not started. "
                "Run `hermes mobile-bug-agent doctor` on the host before updating Hermes."
            ),
        }
    if not active_runs:
        commit = _current_hermes_commit(project_root)
        lease, reason = state.try_acquire_runtime_sync_lease(
            owner_id="hermes-update",
            owner_pid=os.getpid(),
            owner_host=socket.gethostname(),
            project_root=str(project_root or Path(__file__).resolve().parents[2]),
            pre_update_commit=commit,
            started_at=now,
        )
        if lease is not None:
            return None
        if reason == "runtime_sync_in_progress":
            return {
                "action": "block",
                "message": (
                    "Monica runtime sync is already in progress. "
                    "Wait for the current Hermes update to finish before starting another update."
                ),
            }
        if reason == "commit_unavailable":
            return {
                "action": "block",
                "message": (
                    "Monica could not identify the current Hermes commit, so `hermes update` "
                    "was not started. Run `git rev-parse --short=8 HEAD` in the Hermes checkout "
                    "and retry once the repository is readable."
                ),
            }
        active_runs = state.list_runtime_sync_blocking_runs()
        if not active_runs:
            return {
                "action": "block",
                "message": (
                    "Monica could not acquire the runtime sync lease, so `hermes update` "
                    "was not started. Run `hermes mobile-bug-agent doctor` on the host."
                ),
            }
    labels = ", ".join(_active_run_label(run) for run in active_runs[:5])
    if len(active_runs) > 5:
        labels += f", +{len(active_runs) - 5} more"
    run_word = "run" if len(active_runs) == 1 else "runs"
    return {
        "action": "block",
        "message": (
            f"Monica is active ({len(active_runs)} {run_word}: {labels}). "
            "Wait until Monica is idle before running `hermes update`."
        ),
    }


def _on_post_update(project_root: str = "", **_: Any) -> dict[str, str] | None:
    try:
        config = load_monica_config()
        state = MonicaState.open(runtime_root(config) / "state.sqlite")
        lease = state.current_runtime_sync_lease()
        active_runs = state.list_runtime_sync_blocking_runs()
        if active_runs:
            if lease is not None:
                state.record_runtime_sync_failure(
                    lease_id=lease.lease_id,
                    reason="monica_active",
                    completed_at=_utc_now_iso(),
                )
            labels = ", ".join(_active_run_label(run) for run in active_runs[:5])
            if len(active_runs) > 5:
                labels += f", +{len(active_runs) - 5} more"
            return {
                "action": "skipped",
                "reason": "monica_active",
                "active_run_count": str(len(active_runs)),
                "active_runs": labels,
            }
        commit = _current_hermes_commit(project_root)
        if not _looks_like_git_commit(commit):
            if lease is not None:
                state.record_runtime_sync_failure(
                    lease_id=lease.lease_id,
                    reason="commit_unavailable",
                    completed_at=_utc_now_iso(),
                )
            return {"action": "skipped", "reason": "commit_unavailable"}
        if lease is not None:
            metadata = state.complete_runtime_sync_lease(
                lease_id=lease.lease_id,
                post_update_commit=commit,
                completed_at=_utc_now_iso(),
            )
        else:
            metadata = {
                **state.record_runtime_sync(
                    commit=commit,
                    synced_at=_utc_now_iso(),
                ),
                "last_sync_status": "recorded",
            }
    except Exception as exc:
        logger.debug("Monica post-update sync record unavailable: %s", exc, exc_info=True)
        return None
    return {"action": "recorded", **metadata}


def _current_hermes_commit(project_root: str = "") -> str:
    root = Path(project_root or Path(__file__).resolve().parents[2])
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short=8", "HEAD"],
            cwd=str(root),
            text=True,
            capture_output=True,
            check=False,
            timeout=2,
        )
    except Exception:
        return ""
    return str(proc.stdout or "").strip() if proc.returncode == 0 else ""


def _looks_like_git_commit(value: str) -> bool:
    return bool(_GIT_COMMIT_RE.fullmatch(str(value or "").strip()))


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _active_run_label(run: Any) -> str:
    identifier = str(getattr(run, "linear_identifier", "") or getattr(run, "id", "") or "").strip()
    status = str(getattr(run, "status", "") or "").strip()
    if identifier and status:
        return f"{identifier}/{status}"
    return identifier or status or "unknown"


def _is_slack_monica_mention_fallback(event: Any) -> bool:
    source = getattr(event, "source", None)
    if getattr(source, "platform", None) != Platform.SLACK:
        return False
    raw = getattr(event, "raw_message", None)
    if _is_slack_direct_message_fallback(event):
        return True
    text = str(raw.get("text") if isinstance(raw, dict) else getattr(event, "text", "") or "")
    mentioned_ids = set(_SLACK_MENTION_RE.findall(text))
    is_app_mention = isinstance(raw, dict) and str(raw.get("type") or "") == "app_mention"
    try:
        config = load_monica_config()
    except Exception:
        return bool(is_app_mention)
    configured_ids = set(config.slack.bot_user_ids)
    if configured_ids:
        return bool(configured_ids & mentioned_ids)
    return bool(is_app_mention)


def _is_slack_direct_message_fallback(event: Any) -> bool:
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


def register(ctx) -> None:
    ctx.register_cli_command(
        name="mobile-bug-agent",
        help="Inspect and operate Monica mobile bug loops",
        setup_fn=register_cli,
        handler_fn=mobile_bug_agent_command,
        description="Operator CLI for Monica Slack-to-Linear mobile bug loops.",
    )
    ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch)
    ctx.register_hook("gateway_startup", _on_gateway_startup)
    ctx.register_hook("pre_update", _on_pre_update)
    ctx.register_hook("post_update", _on_post_update)
