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
_RUNTIME_SYNC_LEASE_NAME = "hermes_update"
_RUNTIME_SYNC_LEASE_TTL_SECONDS = 30 * 60


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


@dataclass(frozen=True)
class RuntimeSyncLease:
    name: str
    lease_id: str
    owner_id: str
    owner_pid: int
    owner_host: str
    project_root: str
    pre_update_commit: str
    started_at: str
    expires_at: str
    args: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "lease_id": self.lease_id,
            "owner_id": self.owner_id,
            "owner_pid": self.owner_pid,
            "owner_host": self.owner_host,
            "project_root": self.project_root,
            "pre_update_commit": self.pre_update_commit,
            "started_at": self.started_at,
            "expires_at": self.expires_at,
            "args": dict(self.args),
        }


@dataclass(frozen=True)
class MonicaLoopLease:
    lease_id: str
    run_id: str
    owner_id: str
    owner_kind: str
    pid: int
    status_at_acquire: str
    last_status: str
    acquired_at: float
    heartbeat_at: float
    expires_at: float
    released_at: float | None = None
    release_reason: str = ""


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
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_sync_lease (
                    name TEXT PRIMARY KEY,
                    lease_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    owner_pid INTEGER NOT NULL DEFAULT 0,
                    owner_host TEXT NOT NULL DEFAULT '',
                    project_root TEXT NOT NULL DEFAULT '',
                    pre_update_commit TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    args_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS loop_leases (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    owner_kind TEXT NOT NULL DEFAULT 'gateway',
                    pid INTEGER NOT NULL DEFAULT 0,
                    status_at_acquire TEXT NOT NULL,
                    last_status TEXT NOT NULL,
                    acquired_at REAL NOT NULL,
                    heartbeat_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    released_at REAL,
                    release_reason TEXT NOT NULL DEFAULT ''
                )
                """
            )
            self._db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_monica_loop_leases_one_active
                ON loop_leases(run_id)
                WHERE released_at IS NULL
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
            runs = self._list_runtime_sync_blocking_runs_locked()
        return sorted(runs, key=lambda run: _STATUS_PRIORITY.get(run.status, 0), reverse=True)

    def is_idle_for_runtime_sync(self) -> bool:
        return not self.list_runtime_sync_blocking_runs()

    def try_acquire_runtime_sync_lease(
        self,
        *,
        owner_id: str,
        owner_pid: int = 0,
        owner_host: str = "",
        project_root: str = "",
        pre_update_commit: str,
        started_at: str | None = None,
        expires_at: str | None = None,
        args: dict[str, Any] | None = None,
    ) -> tuple[RuntimeSyncLease | None, str]:
        clean_commit = str(pre_update_commit or "").strip()
        if not _GIT_COMMIT_RE.fullmatch(clean_commit):
            return None, "commit_unavailable"
        with self._lock:
            if self._current_runtime_sync_lease_locked() is not None:
                return None, "runtime_sync_in_progress"
            if self._list_runtime_sync_blocking_runs_locked():
                return None, "monica_active"
            lease_id = uuid.uuid4().hex
            started = str(started_at or _utc_now_iso()).strip()
            expires = str(expires_at or _runtime_sync_lease_expires_at(started)).strip()
            self._db.execute(
                """
                INSERT INTO runtime_sync_lease (
                    name, lease_id, owner_id, owner_pid, owner_host, project_root,
                    pre_update_commit, started_at, expires_at, args_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _RUNTIME_SYNC_LEASE_NAME,
                    lease_id,
                    str(owner_id or "").strip(),
                    int(owner_pid or 0),
                    str(owner_host or "").strip(),
                    str(project_root or "").strip(),
                    clean_commit,
                    started,
                    expires,
                    _json_dumps(args or {}),
                ),
            )
            self._db.commit()
            lease = self._current_runtime_sync_lease_locked()
            if lease is None:  # pragma: no cover - sqlite insert/get invariant
                raise RuntimeError("failed to acquire Monica runtime sync lease")
            return lease, ""

    def current_runtime_sync_lease(self) -> RuntimeSyncLease | None:
        with self._lock:
            return self._current_runtime_sync_lease_locked()

    def runtime_sync_gate(self) -> dict[str, Any]:
        lease = self.current_runtime_sync_lease()
        if lease is None:
            return {"open": True, "reason": "", "lease": None}
        return {
            "open": False,
            "reason": "runtime_sync_in_progress",
            "lease": lease.to_dict(),
        }

    def complete_runtime_sync_lease(
        self,
        *,
        lease_id: str,
        post_update_commit: str,
        completed_at: str | None = None,
    ) -> dict[str, str]:
        clean_commit = str(post_update_commit or "").strip()
        if not _GIT_COMMIT_RE.fullmatch(clean_commit):
            raise ValueError("runtime sync commit must be a git SHA")
        with self._lock:
            lease = self._current_runtime_sync_lease_locked()
            if lease is None or lease.lease_id != str(lease_id or "").strip():
                raise KeyError("runtime sync lease not found")
            completed = str(completed_at or _utc_now_iso()).strip()
            metadata = {
                "last_synced_commit": clean_commit,
                "last_synced_at": completed,
                "last_sync_status": "recorded",
                "last_sync_failure_reason": "",
                "last_sync_lease_id": lease.lease_id,
                "last_sync_started_at": lease.started_at,
                "last_sync_completed_at": completed,
                "last_sync_pre_update_commit": lease.pre_update_commit,
                "last_sync_post_update_commit": clean_commit,
                "last_sync_project_root": lease.project_root,
            }
            self._write_runtime_sync_metadata_locked(metadata)
            self._delete_runtime_sync_lease_locked(lease.lease_id)
            self._db.commit()
            return metadata

    def record_runtime_sync_failure(
        self,
        *,
        lease_id: str = "",
        reason: str,
        completed_at: str | None = None,
    ) -> dict[str, str]:
        with self._lock:
            lease = self._current_runtime_sync_lease_locked()
            clean_lease_id = str(lease_id or "").strip()
            if clean_lease_id and lease is not None and lease.lease_id != clean_lease_id:
                raise KeyError("runtime sync lease not found")
            completed = str(completed_at or _utc_now_iso()).strip()
            metadata = {
                "last_sync_status": "failed",
                "last_sync_failure_reason": str(reason or "").strip() or "unknown",
                "last_sync_lease_id": lease.lease_id if lease else clean_lease_id,
                "last_sync_started_at": lease.started_at if lease else "",
                "last_sync_completed_at": completed,
                "last_sync_pre_update_commit": lease.pre_update_commit if lease else "",
                "last_sync_post_update_commit": "",
                "last_sync_project_root": lease.project_root if lease else "",
            }
            self._write_runtime_sync_metadata_locked(metadata)
            if lease is not None:
                self._delete_runtime_sync_lease_locked(lease.lease_id)
            self._db.commit()
            return metadata

    def reap_stale_runtime_sync_lease(
        self,
        *,
        now: str | float | datetime | None = None,
    ) -> RuntimeSyncLease | None:
        cutoff = _timestamp_from_value(now)
        if cutoff is None:
            cutoff = datetime.now(timezone.utc).timestamp()
        completed = _iso_from_timestamp(cutoff)
        with self._lock:
            lease = self._current_runtime_sync_lease_locked()
            if lease is None:
                return None
            expires_at = _timestamp_from_value(lease.expires_at)
            if expires_at is None or expires_at > cutoff:
                return None
            metadata = {
                "last_sync_status": "failed",
                "last_sync_failure_reason": "lease_expired",
                "last_sync_lease_id": lease.lease_id,
                "last_sync_started_at": lease.started_at,
                "last_sync_completed_at": completed,
                "last_sync_pre_update_commit": lease.pre_update_commit,
                "last_sync_post_update_commit": "",
                "last_sync_project_root": lease.project_root,
            }
            self._write_runtime_sync_metadata_locked(metadata)
            self._delete_runtime_sync_lease_locked(lease.lease_id)
            self._db.commit()
            return lease

    def acquire_loop_lease(
        self,
        run_id: str,
        *,
        owner_id: str,
        owner_kind: str = "gateway",
        pid: int = 0,
        acquired_at: float | None = None,
        ttl_seconds: float = 900.0,
    ) -> tuple[MonicaLoopLease | None, str]:
        with self._lock:
            run = self.get_run(run_id)
            if run is None:
                return None, "run_not_found"
            existing = self._current_loop_lease_locked(run_id)
            if existing is not None:
                return None, "loop_already_active"
            now = float(acquired_at if acquired_at is not None else datetime.now(timezone.utc).timestamp())
            lease_id = uuid.uuid4().hex
            self._db.execute(
                """
                INSERT INTO loop_leases (
                    id, run_id, owner_id, owner_kind, pid, status_at_acquire,
                    last_status, acquired_at, heartbeat_at, expires_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lease_id,
                    run.id,
                    str(owner_id or "").strip(),
                    str(owner_kind or "gateway").strip() or "gateway",
                    int(pid or 0),
                    run.status,
                    run.status,
                    now,
                    now,
                    now + float(ttl_seconds or 0),
                ),
            )
            self._db.commit()
            lease = self._current_loop_lease_locked(run.id)
            if lease is None:  # pragma: no cover - sqlite insert/get invariant
                raise RuntimeError(f"failed to acquire Monica loop lease for {run.id}")
            return lease, ""

    def current_loop_lease(self, run_id: str) -> MonicaLoopLease | None:
        with self._lock:
            return self._current_loop_lease_locked(run_id)

    def release_loop_lease(self, lease_id: str, *, reason: str = "") -> bool:
        with self._lock:
            cursor = self._db.execute(
                """
                UPDATE loop_leases
                SET released_at = ?,
                    release_reason = ?
                WHERE id = ? AND released_at IS NULL
                """,
                (
                    datetime.now(timezone.utc).timestamp(),
                    str(reason or "").strip(),
                    str(lease_id or "").strip(),
                ),
            )
            self._db.commit()
            return cursor.rowcount > 0

    def reap_stale_loop_leases(self, *, now: float | None = None) -> list[MonicaLoopLease]:
        cutoff = float(now if now is not None else datetime.now(timezone.utc).timestamp())
        with self._lock:
            rows = self._db.execute(
                """
                SELECT * FROM loop_leases
                WHERE released_at IS NULL AND expires_at <= ?
                ORDER BY expires_at ASC, id ASC
                """,
                (cutoff,),
            ).fetchall()
            leases = [self._row_to_loop_lease(row) for row in rows]
            for lease in leases:
                self._db.execute(
                    """
                    UPDATE loop_leases
                    SET released_at = ?,
                        release_reason = ?
                    WHERE id = ? AND released_at IS NULL
                    """,
                    (cutoff, "stale_reaped", lease.lease_id),
                )
            self._db.commit()
            return leases

    def record_runtime_sync(self, *, commit: str, synced_at: str | None = None) -> dict[str, str]:
        clean_commit = str(commit or "").strip()
        if not _GIT_COMMIT_RE.fullmatch(clean_commit):
            raise ValueError("runtime sync commit must be a git SHA")
        metadata = {
            "last_synced_commit": clean_commit,
            "last_synced_at": str(synced_at or _utc_now_iso()).strip(),
        }
        with self._lock:
            self._write_runtime_sync_metadata_locked(metadata)
            self._db.commit()
        return metadata

    def runtime_sync_metadata(self) -> dict[str, str]:
        with self._lock:
            values = self._runtime_sync_metadata_values_locked()
        return {
            "last_synced_commit": values.get("last_synced_commit", ""),
            "last_synced_at": values.get("last_synced_at", ""),
        }

    def runtime_sync_health(self) -> dict[str, Any]:
        with self._lock:
            values = self._runtime_sync_metadata_values_locked()
            lease = self._current_runtime_sync_lease_locked()
        return {
            "last_synced_commit": values.get("last_synced_commit", ""),
            "last_synced_at": values.get("last_synced_at", ""),
            "last_sync_status": values.get("last_sync_status", ""),
            "last_sync_failure_reason": values.get("last_sync_failure_reason", ""),
            "last_sync_lease_id": values.get("last_sync_lease_id", ""),
            "last_sync_started_at": values.get("last_sync_started_at", ""),
            "last_sync_completed_at": values.get("last_sync_completed_at", ""),
            "last_sync_pre_update_commit": values.get("last_sync_pre_update_commit", ""),
            "last_sync_post_update_commit": values.get("last_sync_post_update_commit", ""),
            "last_sync_project_root": values.get("last_sync_project_root", ""),
            "lease": lease.to_dict() if lease else None,
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
            if "status" in updates:
                now = datetime.now(timezone.utc).timestamp()
                self._db.execute(
                    """
                    UPDATE loop_leases
                    SET last_status = ?,
                        heartbeat_at = ?,
                        expires_at = CASE
                            WHEN expires_at < ? THEN ?
                            ELSE expires_at
                        END
                    WHERE run_id = ? AND released_at IS NULL
                    """,
                    (updates["status"], now, now, now, run_id),
                )
            self._db.commit()
            run = self.get_run(run_id)
            if run is None:
                raise KeyError(run_id)
            return run

    def _list_runtime_sync_blocking_runs_locked(self) -> list[MonicaRun]:
        placeholders = ",".join("?" for _ in RUNTIME_SYNC_TERMINAL_STATUSES)
        rows = self._db.execute(
            f"SELECT * FROM runs WHERE status NOT IN ({placeholders})",
            RUNTIME_SYNC_TERMINAL_STATUSES,
        ).fetchall()
        return [self._row_to_run(row) for row in rows]

    def _current_runtime_sync_lease_locked(self) -> RuntimeSyncLease | None:
        row = self._db.execute(
            "SELECT * FROM runtime_sync_lease WHERE name = ?",
            (_RUNTIME_SYNC_LEASE_NAME,),
        ).fetchone()
        return self._row_to_runtime_sync_lease(row) if row else None

    def _delete_runtime_sync_lease_locked(self, lease_id: str) -> None:
        self._db.execute(
            "DELETE FROM runtime_sync_lease WHERE name = ? AND lease_id = ?",
            (_RUNTIME_SYNC_LEASE_NAME, str(lease_id or "").strip()),
        )

    def _current_loop_lease_locked(self, run_id: str) -> MonicaLoopLease | None:
        row = self._db.execute(
            """
            SELECT * FROM loop_leases
            WHERE run_id = ? AND released_at IS NULL
            ORDER BY acquired_at DESC, id DESC
            LIMIT 1
            """,
            (str(run_id or "").strip(),),
        ).fetchone()
        return self._row_to_loop_lease(row) if row else None

    def _write_runtime_sync_metadata_locked(self, metadata: dict[str, str]) -> None:
        self._db.executemany(
            """
            INSERT INTO runtime_sync_metadata(key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            tuple((str(key), str(value)) for key, value in metadata.items()),
        )

    def _runtime_sync_metadata_values_locked(self) -> dict[str, str]:
        rows = self._db.execute("SELECT key, value FROM runtime_sync_metadata").fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

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

    @staticmethod
    def _row_to_runtime_sync_lease(row: sqlite3.Row) -> RuntimeSyncLease:
        return RuntimeSyncLease(
            name=str(row["name"]),
            lease_id=str(row["lease_id"]),
            owner_id=str(row["owner_id"]),
            owner_pid=int(row["owner_pid"] or 0),
            owner_host=str(row["owner_host"] or ""),
            project_root=str(row["project_root"] or ""),
            pre_update_commit=str(row["pre_update_commit"] or ""),
            started_at=str(row["started_at"] or ""),
            expires_at=str(row["expires_at"] or ""),
            args=_json_loads(str(row["args_json"] or "{}")),
        )

    @staticmethod
    def _row_to_loop_lease(row: sqlite3.Row) -> MonicaLoopLease:
        released = row["released_at"]
        return MonicaLoopLease(
            lease_id=str(row["id"]),
            run_id=str(row["run_id"]),
            owner_id=str(row["owner_id"]),
            owner_kind=str(row["owner_kind"] or "gateway"),
            pid=int(row["pid"] or 0),
            status_at_acquire=str(row["status_at_acquire"] or ""),
            last_status=str(row["last_status"] or ""),
            acquired_at=float(row["acquired_at"] or 0.0),
            heartbeat_at=float(row["heartbeat_at"] or 0.0),
            expires_at=float(row["expires_at"] or 0.0),
            released_at=float(released) if released is not None else None,
            release_reason=str(row["release_reason"] or ""),
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


def _timestamp_from_value(value: str | float | datetime | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        return float(value)
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _iso_from_timestamp(value: float) -> str:
    return (
        datetime.fromtimestamp(float(value), timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _runtime_sync_lease_expires_at(started_at: str) -> str:
    started = _timestamp_from_value(started_at)
    if started is None:
        started = datetime.now(timezone.utc).timestamp()
    return _iso_from_timestamp(started + _RUNTIME_SYNC_LEASE_TTL_SECONDS)


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
