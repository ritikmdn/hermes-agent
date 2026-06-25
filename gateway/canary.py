"""Gateway startup canaries and runtime fingerprints."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any

from gateway.agentic_router import route_pre_gateway_hooks


GATEWAY_AGENTIC_CONTRACT_VERSION = "2026-06-09.agentic-gateway.v1"

_ROOT = Path(__file__).resolve().parents[1]
_FINGERPRINT_FILES = (
    "gateway/run.py",
    "gateway/agentic_router.py",
    "gateway/decision_ledger.py",
    "gateway/canary.py",
    "gateway/platforms/base.py",
    "gateway/platforms/slack.py",
    "agent/conversation_loop.py",
    "hermes_cli/plugins.py",
    "profile-distributions/elixir-analytics/profile_plugins/elixir-analytics-runner/__init__.py",
)


def _git_head(root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=str(root),
            text=True,
            capture_output=True,
            timeout=1,
            check=False,
        )
    except Exception:
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _git_dirty_files(root: Path) -> list[str]:
    try:
        completed = subprocess.run(
            ["git", "status", "--short", "--", *_FINGERPRINT_FILES],
            cwd=str(root),
            text=True,
            capture_output=True,
            timeout=1,
            check=False,
        )
    except Exception:
        return []
    if completed.returncode != 0:
        return []
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def build_runtime_fingerprint(root: Path | None = None) -> dict[str, Any]:
    root = root or _ROOT
    digest = hashlib.sha256()
    file_digests: dict[str, str] = {}
    for rel_path in _FINGERPRINT_FILES:
        path = root / rel_path
        if not path.exists():
            file_digests[rel_path] = "missing"
            digest.update(f"{rel_path}:missing\n".encode("utf-8"))
            continue
        data = path.read_bytes()
        file_hash = hashlib.sha256(data).hexdigest()[:16]
        file_digests[rel_path] = file_hash
        digest.update(rel_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return {
        "contract_version": GATEWAY_AGENTIC_CONTRACT_VERSION,
        "source": str(root),
        "git_head": _git_head(root),
        "dirty_files": _git_dirty_files(root),
        "digest": digest.hexdigest()[:24],
        "files": file_digests,
    }


def detect_stale_runtime(
    *,
    expected: dict[str, Any],
    actual: dict[str, Any],
) -> str | None:
    expected_digest = str(expected.get("digest") or "")
    actual_digest = str(actual.get("digest") or "")
    if not expected_digest or not actual_digest or expected_digest == actual_digest:
        return None
    return (
        "Gateway runtime fingerprint mismatch: "
        f"expected digest {expected_digest} from {expected.get('source') or 'expected source'}, "
        f"but running digest is {actual_digest} from {actual.get('source') or 'runtime source'}. "
        "Restart the gateway from the intended checkout before trusting live behavior."
    )


def run_pre_gateway_contract_canary() -> dict[str, Any]:
    decision = route_pre_gateway_hooks(
        [
            {
                "action": "respond",
                "text": "Which option should I use?",
                "reason": "contract_canary_conversational_respond",
            }
        ]
    )
    blocked_reason = (
        decision.hook_decisions[0].blocked_reason
        if decision.hook_decisions
        else None
    )
    ok = (
        decision.route == "agent"
        and decision.response_text is None
        and blocked_reason == "conversational_respond_not_allowed"
    )
    return {
        "ok": ok,
        "route": decision.route,
        "blocked_reason": blocked_reason,
        "contract_version": GATEWAY_AGENTIC_CONTRACT_VERSION,
    }
