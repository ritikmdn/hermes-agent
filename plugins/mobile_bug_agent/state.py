from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_GIT_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


@dataclass(frozen=True)
class MonicaRun:
    id: str
    platform: str
    channel_id: str
    thread_ts: str
    message_ts: str
    user_id: str
    request_text: str
    intent: str
    status: str
    raw_event: dict[str, Any] | None = None
    linear_identifier: str = ""
    linear_issue_id: str = ""
    linear_url: str = ""
    branch_name: str = ""
    base_branch: str = ""
    base_commit: str = ""
    proof_deep_link: str = ""
    proof_expected_text: str = ""
    proof_screen: str = ""
    pr_url: str = ""
    failure_reason: str = ""
    approved_by_user_id: str = ""
    created_at: str = ""
    updated_at: str = ""


class MonicaState:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._migrate()

    @classmethod
    def open(cls, path: str | Path) -> "MonicaState":
        return cls(Path(path))

    def _migrate(self) -> None:
        with self._lock:
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    thread_ts TEXT NOT NULL,
                    message_ts TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    request_text TEXT NOT NULL,
                    raw_event_json TEXT NOT NULL DEFAULT '{}',
                    intent TEXT NOT NULL,
                    status TEXT NOT NULL,
                    linear_identifier TEXT NOT NULL DEFAULT '',
                    linear_issue_id TEXT NOT NULL DEFAULT '',
                    linear_url TEXT NOT NULL DEFAULT '',
                    branch_name TEXT NOT NULL DEFAULT '',
                    base_branch TEXT NOT NULL DEFAULT '',
                    base_commit TEXT NOT NULL DEFAULT '',
                    proof_deep_link TEXT NOT NULL DEFAULT '',
                    proof_expected_text TEXT NOT NULL DEFAULT '',
                    proof_screen TEXT NOT NULL DEFAULT '',
                    pr_url TEXT NOT NULL DEFAULT '',
                    failure_reason TEXT NOT NULL DEFAULT '',
                    approved_by_user_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(platform, channel_id, thread_ts)
                )
                """
            )
            existing = {
                str(row["name"])
                for row in self._db.execute("PRAGMA table_info(runs)").fetchall()
            }
            required = {
                "linear_identifier": "TEXT NOT NULL DEFAULT ''",
                "raw_event_json": "TEXT NOT NULL DEFAULT '{}'",
                "linear_issue_id": "TEXT NOT NULL DEFAULT ''",
                "linear_url": "TEXT NOT NULL DEFAULT ''",
                "branch_name": "TEXT NOT NULL DEFAULT ''",
                "base_branch": "TEXT NOT NULL DEFAULT ''",
                "base_commit": "TEXT NOT NULL DEFAULT ''",
                "proof_deep_link": "TEXT NOT NULL DEFAULT ''",
                "proof_expected_text": "TEXT NOT NULL DEFAULT ''",
                "proof_screen": "TEXT NOT NULL DEFAULT ''",
                "pr_url": "TEXT NOT NULL DEFAULT ''",
                "failure_reason": "TEXT NOT NULL DEFAULT ''",
                "approved_by_user_id": "TEXT NOT NULL DEFAULT ''",
                "created_at": "TEXT NOT NULL DEFAULT ''",
                "updated_at": "TEXT NOT NULL DEFAULT ''",
            }
            for column, definition in required.items():
                if column not in existing:
                    self._db.execute(f"ALTER TABLE runs ADD COLUMN {column} {definition}")
            self._deduplicate_thread_runs()
            self._db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_monica_runs_thread_unique
                ON runs(platform, channel_id, thread_ts)
                """
            )
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_sync_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            self._db.commit()

    def _deduplicate_thread_runs(self) -> None:
        rows = self._db.execute("SELECT rowid AS monica_rowid, * FROM runs").fetchall()
        groups: dict[tuple[str, str, str], list[sqlite3.Row]] = {}
        for row in rows:
            key = (str(row["platform"]), str(row["channel_id"]), str(row["thread_ts"]))
            groups.setdefault(key, []).append(row)

        for grouped in groups.values():
            if len(grouped) <= 1:
                continue
            winner = max(grouped, key=_run_dedup_score)
            loser_rowids = [
                int(row["monica_rowid"])
                for row in grouped
                if int(row["monica_rowid"]) != int(winner["monica_rowid"])
            ]
            if not loser_rowids:
                continue
            placeholders = ", ".join("?" for _ in loser_rowids)
            self._db.execute(
                f"DELETE FROM runs WHERE rowid IN ({placeholders})",
                loser_rowids,
            )

    def create_run(
        self,
        *,
        platform: str,
        channel_id: str,
        thread_ts: str,
        message_ts: str,
        user_id: str,
        request_text: str,
        raw_event: dict[str, Any] | None = None,
        intent: str = "agentic_triage",
        status: str = "queued",
    ) -> MonicaRun:
        run, _created = self.create_run_once(
            platform=platform,
            channel_id=channel_id,
            thread_ts=thread_ts,
            message_ts=message_ts,
            user_id=user_id,
            request_text=request_text,
            raw_event=raw_event,
            intent=intent,
            status=status,
        )
        return run

    def create_run_once(
        self,
        *,
        platform: str,
        channel_id: str,
        thread_ts: str,
        message_ts: str,
        user_id: str,
        request_text: str,
        raw_event: dict[str, Any] | None = None,
        intent: str = "agentic_triage",
        status: str = "queued",
    ) -> tuple[MonicaRun, bool]:
        with self._lock:
            existing = self._find_run_locked(
                platform=platform,
                channel_id=channel_id,
                thread_ts=thread_ts,
            )
            if existing is not None:
                return existing, False

            run_id = uuid.uuid4().hex
            try:
                self._db.execute(
                    """
                    INSERT INTO runs (
                        id, platform, channel_id, thread_ts, message_ts, user_id,
                        request_text, raw_event_json, intent, status
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        platform,
                        channel_id,
                        thread_ts,
                        message_ts,
                        user_id,
                        request_text,
                        _json_dumps(raw_event or {}),
                        intent,
                        status,
                    ),
                )
            except sqlite3.IntegrityError:
                existing = self._find_run_locked(
                    platform=platform,
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                )
                if existing is None:
                    raise
                return existing, False
            self._db.commit()
            run = self.get_run(run_id)
            if run is None:  # pragma: no cover - sqlite insert/get invariant
                raise RuntimeError(f"failed to create Monica run {run_id}")
            return run, True

    def find_run(self, *, platform: str, channel_id: str, thread_ts: str) -> MonicaRun | None:
        with self._lock:
            return self._find_run_locked(
                platform=platform,
                channel_id=channel_id,
                thread_ts=thread_ts,
            )

    def _find_run_locked(self, *, platform: str, channel_id: str, thread_ts: str) -> MonicaRun | None:
        row = self._db.execute(
            """
            SELECT * FROM runs
            WHERE platform = ? AND channel_id = ? AND thread_ts = ?
            """,
            (platform, channel_id, thread_ts),
        ).fetchone()
        return self._row_to_run(row) if row else None

    def get_run(self, run_id: str) -> MonicaRun | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            return self._row_to_run(row) if row else None

    def list_runs(self, *, limit: int | None = None) -> list[MonicaRun]:
        with self._lock:
            query = "SELECT * FROM runs ORDER BY created_at DESC, id DESC"
            params: tuple[Any, ...] = ()
            if limit is not None and limit > 0:
                query += " LIMIT ?"
                params = (limit,)
            rows = self._db.execute(query, params).fetchall()
            return [self._row_to_run(row) for row in rows]

    def list_runtime_sync_blocking_runs(self) -> list[MonicaRun]:
        with self._lock:
            placeholders = ",".join("?" for _ in RUNTIME_SYNC_TERMINAL_STATUSES)
            rows = self._db.execute(
                f"SELECT * FROM runs WHERE status NOT IN ({placeholders})",
                RUNTIME_SYNC_TERMINAL_STATUSES,
            ).fetchall()
            runs = [self._row_to_run(row) for row in rows]
        return sorted(runs, key=lambda run: _STATUS_PRIORITY.get(run.status, 0), reverse=True)

    def is_idle_for_runtime_sync(self) -> bool:
        return not self.list_runtime_sync_blocking_runs()

    def record_runtime_sync(self, *, commit: str, synced_at: str | None = None) -> dict[str, str]:
        clean_commit = str(commit or "").strip()
        if not _GIT_COMMIT_RE.fullmatch(clean_commit):
            raise ValueError("runtime sync commit must be a git SHA")
        metadata = {
            "last_synced_commit": clean_commit,
            "last_synced_at": str(synced_at or _utc_now_iso()).strip(),
        }
        with self._lock:
            self._db.executemany(
                """
                INSERT INTO runtime_sync_metadata(key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                tuple(metadata.items()),
            )
            self._db.commit()
        return metadata

    def runtime_sync_metadata(self) -> dict[str, str]:
        with self._lock:
            rows = self._db.execute("SELECT key, value FROM runtime_sync_metadata").fetchall()
        values = {str(row["key"]): str(row["value"]) for row in rows}
        return {
            "last_synced_commit": values.get("last_synced_commit", ""),
            "last_synced_at": values.get("last_synced_at", ""),
        }

    def update_run(self, run_id: str, **fields: Any) -> MonicaRun:
        allowed = {
            "intent",
            "status",
            "message_ts",
            "user_id",
            "request_text",
            "raw_event",
            "raw_event_json",
            "linear_identifier",
            "linear_issue_id",
            "linear_url",
            "branch_name",
            "base_branch",
            "base_commit",
            "proof_deep_link",
            "proof_expected_text",
            "proof_screen",
            "pr_url",
            "failure_reason",
            "approved_by_user_id",
        }
        updates: dict[str, str] = {}
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "raw_event":
                updates["raw_event_json"] = _json_dumps(value if isinstance(value, dict) else {})
            elif key == "raw_event_json":
                updates[key] = str(value)
            else:
                updates[key] = str(value)
        if not updates:
            run = self.get_run(run_id)
            if run is None:
                raise KeyError(run_id)
            return run

        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = list(updates.values())
        values.append(run_id)
        with self._lock:
            self._db.execute(
                f"UPDATE runs SET {assignments}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                values,
            )
            self._db.commit()
            run = self.get_run(run_id)
            if run is None:
                raise KeyError(run_id)
            return run

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> MonicaRun:
        return MonicaRun(
            id=row["id"],
            platform=row["platform"],
            channel_id=row["channel_id"],
            thread_ts=row["thread_ts"],
            message_ts=row["message_ts"],
            user_id=row["user_id"],
            request_text=row["request_text"],
            intent=row["intent"],
            status=row["status"],
            raw_event=_json_loads(row["raw_event_json"]),
            linear_identifier=row["linear_identifier"],
            linear_issue_id=row["linear_issue_id"],
            linear_url=row["linear_url"],
            branch_name=row["branch_name"],
            base_branch=row["base_branch"],
            base_commit=row["base_commit"],
            proof_deep_link=row["proof_deep_link"],
            proof_expected_text=row["proof_expected_text"],
            proof_screen=row["proof_screen"],
            pr_url=row["pr_url"],
            failure_reason=row["failure_reason"],
            approved_by_user_id=row["approved_by_user_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def approve_fix(self, run_id: str, *, approved_by_user_id: str) -> MonicaRun:
        return self.update_run(
            run_id,
            status="approved",
            approved_by_user_id=approved_by_user_id,
            failure_reason="",
            branch_name="",
            base_branch="",
            base_commit="",
            proof_deep_link="",
            proof_expected_text="",
            proof_screen="",
            pr_url="",
        )

    def approve_fix_once(self, run_id: str, *, approved_by_user_id: str) -> tuple[MonicaRun, bool]:
        with self._lock:
            cursor = self._db.execute(
                """
                UPDATE runs
                SET status = ?,
                    approved_by_user_id = ?,
                    failure_reason = '',
                    branch_name = '',
                    base_branch = '',
                    base_commit = '',
                    proof_deep_link = '',
                    proof_expected_text = '',
                    proof_screen = '',
                    pr_url = '',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = ?
                """,
                ("approved", approved_by_user_id, run_id, "awaiting_fix_approval"),
            )
            changed = cursor.rowcount > 0
            self._db.commit()
            run = self.get_run(run_id)
            if run is None:
                raise KeyError(run_id)
            return run, changed


def _json_dumps(value: dict[str, Any]) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return "{}"


def _json_loads(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


_STATUS_PRIORITY = {
    "done": 100,
    "opening_pr": 90,
    "proof_blocked": 85,
    "proofing": 82,
    "verifying": 80,
    "fixing": 70,
    "approved": 60,
    "awaiting_fix_approval": 50,
    "linear_created": 45,
    "creating_linear": 40,
    "triaging": 30,
    "needs_clarification": 20,
    "failed": 15,
    "blocked": 10,
    "queued": 0,
}

RUNTIME_SYNC_TERMINAL_STATUSES = ("done", "blocked", "failed", "needs_clarification")
RUNTIME_SYNC_BLOCKING_STATUSES = (
    "queued",
    "triaging",
    "creating_linear",
    "linear_created",
    "awaiting_fix_approval",
    "approved",
    "fixing",
    "verifying",
    "proofing",
    "proof_blocked",
    "opening_pr",
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _run_dedup_score(row: sqlite3.Row) -> tuple[int, int, int, int, int, str, str, int]:
    status = str(row["status"] or "")
    has_pr = int(bool(str(row["pr_url"] or "").strip()))
    has_branch = int(bool(str(row["branch_name"] or "").strip()))
    has_linear = int(
        bool(
            str(row["linear_identifier"] or "").strip()
            or str(row["linear_issue_id"] or "").strip()
            or str(row["linear_url"] or "").strip()
        )
    )
    has_approval = int(bool(str(row["approved_by_user_id"] or "").strip()))
    return (
        _STATUS_PRIORITY.get(status, 0),
        has_pr,
        has_branch,
        has_linear,
        has_approval,
        str(row["updated_at"] or ""),
        str(row["created_at"] or ""),
        int(row["monica_rowid"]),
    )
