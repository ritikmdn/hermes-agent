from __future__ import annotations

import sqlite3

import pytest

from plugins.mobile_bug_agent.state import MonicaState


def test_state_can_store_full_run_metadata(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U123",
        request_text="@monica fix checkout crash",
    )

    state.update_run(
        run.id,
        status="done",
        linear_identifier="MOB-42",
        linear_url="https://linear.app/acme/issue/MOB-42",
        branch_name="monica/MOB-42-checkout-crash",
        base_branch="origin/dev",
        base_commit="abc123base",
        proof_deep_link="elixir-card://marketplace/offer/fitness-first",
        proof_expected_text="Fitness First",
        pr_url="https://github.com/acme/mobile/pull/123",
    )

    saved = state.get_run(run.id)
    assert saved is not None
    assert saved.status == "done"
    assert saved.linear_identifier == "MOB-42"
    assert saved.base_branch == "origin/dev"
    assert saved.base_commit == "abc123base"
    assert saved.proof_deep_link == "elixir-card://marketplace/offer/fitness-first"
    assert saved.proof_expected_text == "Fitness First"
    assert saved.pr_url.endswith("/123")


def test_state_approval_clears_stale_fix_and_proof_metadata(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U123",
        request_text="@monica marketplace PDP copy is wrong",
    )
    state.update_run(
        run.id,
        status="awaiting_fix_approval",
        branch_name="monica/MOB-123-old-copy-fix",
        base_branch="origin/dev",
        base_commit="abc1234",
        proof_deep_link="elixir-card://marketplace/offer/old-offer",
        proof_expected_text="Old Offer",
        proof_screen="/OldPdpScreen",
        pr_url="https://github.com/acme/mobile/pull/123",
        failure_reason="old failure",
    )

    approved = state.approve_fix(run.id, approved_by_user_id="U_APPROVER")

    assert approved.status == "approved"
    assert approved.approved_by_user_id == "U_APPROVER"
    assert approved.branch_name == ""
    assert approved.base_branch == ""
    assert approved.base_commit == ""
    assert approved.proof_deep_link == ""
    assert approved.proof_expected_text == ""
    assert approved.proof_screen == ""
    assert approved.pr_url == ""
    assert approved.failure_reason == ""


def test_state_approve_once_clears_stale_fix_and_proof_metadata(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U123",
        request_text="@monica marketplace PDP copy is wrong",
    )
    state.update_run(
        run.id,
        status="awaiting_fix_approval",
        branch_name="monica/MOB-123-old-copy-fix",
        base_branch="origin/dev",
        base_commit="abc1234",
        proof_deep_link="elixir-card://marketplace/offer/old-offer",
        proof_expected_text="Old Offer",
        proof_screen="/OldPdpScreen",
        pr_url="https://github.com/acme/mobile/pull/123",
        failure_reason="old failure",
    )

    approved, changed = state.approve_fix_once(run.id, approved_by_user_id="U_APPROVER")

    assert changed is True
    assert approved.status == "approved"
    assert approved.approved_by_user_id == "U_APPROVER"
    assert approved.branch_name == ""
    assert approved.base_branch == ""
    assert approved.base_commit == ""
    assert approved.proof_deep_link == ""
    assert approved.proof_expected_text == ""
    assert approved.proof_screen == ""
    assert approved.pr_url == ""
    assert approved.failure_reason == ""


def test_state_records_runtime_sync_metadata(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")

    saved = state.record_runtime_sync(commit="abc12345", synced_at="2026-06-13T10:00:00Z")

    assert saved == {
        "last_synced_commit": "abc12345",
        "last_synced_at": "2026-06-13T10:00:00Z",
    }
    reopened = MonicaState.open(tmp_path / "monica.sqlite")
    assert reopened.runtime_sync_metadata() == saved


def test_state_rejects_invalid_runtime_sync_commit(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")

    with pytest.raises(ValueError, match="runtime sync commit must be a git SHA"):
        state.record_runtime_sync(commit="not-a-sha", synced_at="2026-06-13T10:00:00Z")

    assert state.runtime_sync_metadata() == {
        "last_synced_commit": "",
        "last_synced_at": "",
    }


def test_state_acquires_runtime_sync_lease_when_idle(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")

    lease, reason = state.try_acquire_runtime_sync_lease(
        owner_id="hermes-update",
        owner_pid=1234,
        owner_host="host",
        project_root="/repo/hermes",
        pre_update_commit="dead1234",
        started_at="2026-06-15T10:00:00Z",
        expires_at="2026-06-15T10:15:00Z",
    )

    assert reason == ""
    assert lease is not None
    assert lease.owner_id == "hermes-update"
    assert lease.pre_update_commit == "dead1234"
    assert state.runtime_sync_gate()["open"] is False
    assert state.runtime_sync_gate()["reason"] == "runtime_sync_in_progress"


def test_state_defaults_runtime_sync_lease_expiry_after_start(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")

    lease, reason = state.try_acquire_runtime_sync_lease(
        owner_id="hermes-update",
        owner_pid=1234,
        owner_host="host",
        project_root="/repo/hermes",
        pre_update_commit="dead1234",
        started_at="2026-06-15T10:00:00Z",
    )

    assert reason == ""
    assert lease is not None
    assert lease.started_at == "2026-06-15T10:00:00Z"
    assert lease.expires_at == "2026-06-15T10:30:00Z"


def test_state_refuses_runtime_sync_lease_when_active_runs_exist(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U123",
        request_text="@monica marketplace PDP copy is wrong",
    )
    state.update_run(run.id, status="proofing", linear_identifier="MOB-42")

    lease, reason = state.try_acquire_runtime_sync_lease(
        owner_id="hermes-update",
        owner_pid=1234,
        owner_host="host",
        project_root="/repo/hermes",
        pre_update_commit="dead1234",
        started_at="2026-06-15T10:00:00Z",
        expires_at="2026-06-15T10:15:00Z",
    )

    assert lease is None
    assert reason == "monica_active"
    assert state.current_runtime_sync_lease() is None


def test_state_completes_runtime_sync_lease_and_records_metadata(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    lease, reason = state.try_acquire_runtime_sync_lease(
        owner_id="hermes-update",
        owner_pid=1234,
        owner_host="host",
        project_root="/repo/hermes",
        pre_update_commit="dead1234",
        started_at="2026-06-15T10:00:00Z",
        expires_at="2026-06-15T10:15:00Z",
    )
    assert lease is not None
    assert reason == ""

    metadata = state.complete_runtime_sync_lease(
        lease_id=lease.lease_id,
        post_update_commit="abc12345",
        completed_at="2026-06-15T10:03:00Z",
    )

    assert metadata["last_synced_commit"] == "abc12345"
    assert metadata["last_sync_status"] == "recorded"
    assert metadata["last_sync_pre_update_commit"] == "dead1234"
    assert metadata["last_sync_post_update_commit"] == "abc12345"
    assert state.current_runtime_sync_lease() is None
    assert state.runtime_sync_gate()["open"] is True


def test_state_records_runtime_sync_failure_and_releases_lease(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    lease, reason = state.try_acquire_runtime_sync_lease(
        owner_id="hermes-update",
        owner_pid=1234,
        owner_host="host",
        project_root="/repo/hermes",
        pre_update_commit="dead1234",
        started_at="2026-06-15T10:00:00Z",
        expires_at="2026-06-15T10:15:00Z",
    )
    assert lease is not None
    assert reason == ""

    metadata = state.record_runtime_sync_failure(
        lease_id=lease.lease_id,
        reason="commit_unavailable",
        completed_at="2026-06-15T10:03:00Z",
    )

    assert metadata["last_sync_status"] == "failed"
    assert metadata["last_sync_failure_reason"] == "commit_unavailable"
    assert state.current_runtime_sync_lease() is None


def test_state_reaps_expired_runtime_sync_lease_and_records_failure(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    lease, reason = state.try_acquire_runtime_sync_lease(
        owner_id="hermes-update",
        owner_pid=1234,
        owner_host="host",
        project_root="/repo/hermes",
        pre_update_commit="dead1234",
        started_at="2026-06-15T10:00:00Z",
        expires_at="2026-06-15T10:15:00Z",
    )
    assert lease is not None
    assert reason == ""

    reaped = state.reap_stale_runtime_sync_lease(now="2026-06-15T10:16:00Z")

    assert reaped is not None
    assert reaped.lease_id == lease.lease_id
    assert state.current_runtime_sync_lease() is None
    health = state.runtime_sync_health()
    assert health["last_sync_status"] == "failed"
    assert health["last_sync_failure_reason"] == "lease_expired"
    assert health["last_sync_lease_id"] == lease.lease_id
    assert health["last_sync_completed_at"] == "2026-06-15T10:16:00Z"


def test_state_acquires_and_releases_loop_lease(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U123",
        request_text="@monica marketplace PDP copy is wrong",
    )

    lease, reason = state.acquire_loop_lease(
        run.id,
        owner_id="gateway-1",
        owner_kind="gateway",
        pid=1234,
        acquired_at=1781520000.0,
        ttl_seconds=300,
    )

    assert reason == ""
    assert lease is not None
    assert lease.run_id == run.id
    assert lease.owner_id == "gateway-1"
    assert state.current_loop_lease(run.id) == lease

    released = state.release_loop_lease(lease.lease_id, reason="done")

    assert released is True
    assert state.current_loop_lease(run.id) is None


def test_state_refuses_second_live_loop_lease(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U123",
        request_text="@monica marketplace PDP copy is wrong",
    )
    first, reason = state.acquire_loop_lease(
        run.id,
        owner_id="gateway-1",
        acquired_at=1781520000.0,
        ttl_seconds=300,
    )
    assert first is not None
    assert reason == ""

    second, reason = state.acquire_loop_lease(
        run.id,
        owner_id="gateway-2",
        acquired_at=1781520001.0,
        ttl_seconds=300,
    )

    assert second is None
    assert reason == "loop_already_active"
    assert state.current_loop_lease(run.id) == first


def test_state_reaps_expired_loop_lease(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U123",
        request_text="@monica marketplace PDP copy is wrong",
    )
    lease, reason = state.acquire_loop_lease(
        run.id,
        owner_id="gateway-1",
        acquired_at=1781520000.0,
        ttl_seconds=1,
    )
    assert lease is not None
    assert reason == ""

    reaped = state.reap_stale_loop_leases(now=1781520300.0)

    assert [item.lease_id for item in reaped] == [lease.lease_id]
    assert state.current_loop_lease(run.id) is None


def test_state_update_run_touches_active_loop_lease_status(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U123",
        request_text="@monica marketplace PDP copy is wrong",
    )
    lease, reason = state.acquire_loop_lease(
        run.id,
        owner_id="gateway-1",
        acquired_at=1781520000.0,
        ttl_seconds=300,
    )
    assert lease is not None
    assert reason == ""

    state.update_run(run.id, status="fixing")

    updated_lease = state.current_loop_lease(run.id)
    assert updated_lease is not None
    assert updated_lease.last_status == "fixing"
    assert updated_lease.heartbeat_at >= lease.heartbeat_at


def test_state_persists_raw_slack_payload(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U123",
        request_text="@monica fix checkout crash",
        raw_event={
            "permalink": "https://slack/thread",
            "files": [{"id": "F1", "name": "crash.png", "mimetype": "image/png"}],
        },
    )

    saved = state.get_run(run.id)

    assert saved is not None
    assert saved.raw_event == {
        "files": [{"id": "F1", "mimetype": "image/png", "name": "crash.png"}],
        "permalink": "https://slack/thread",
    }


def test_state_migrates_existing_run_table(tmp_path):
    db_path = tmp_path / "old.sqlite"
    db = sqlite3.connect(db_path)
    db.execute(
        """
        CREATE TABLE runs (
            id TEXT PRIMARY KEY,
            platform TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            thread_ts TEXT NOT NULL,
            message_ts TEXT NOT NULL,
            user_id TEXT NOT NULL,
            request_text TEXT NOT NULL,
            intent TEXT NOT NULL,
            status TEXT NOT NULL,
            UNIQUE(platform, channel_id, thread_ts)
        )
        """
    )
    db.execute(
        """
        INSERT INTO runs (
            id, platform, channel_id, thread_ts, message_ts, user_id,
            request_text, intent, status
        ) VALUES ('run-id', 'slack', 'C123', 'T1', 'T1', 'U1', '@monica bug', 'agentic_triage', 'queued')
        """
    )
    db.commit()
    db.close()

    state = MonicaState.open(db_path)
    run = state.get_run("run-id")

    assert run is not None
    assert run.linear_identifier == ""
    assert run.approved_by_user_id == ""
    assert run.base_branch == ""
    assert run.base_commit == ""
    assert run.proof_deep_link == ""
    assert run.proof_expected_text == ""
    assert run.raw_event == {}


def test_state_migration_adds_unique_thread_index_to_legacy_table(tmp_path):
    db_path = tmp_path / "old-without-index.sqlite"
    db = sqlite3.connect(db_path)
    db.execute(
        """
        CREATE TABLE runs (
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
            pr_url TEXT NOT NULL DEFAULT '',
            failure_reason TEXT NOT NULL DEFAULT '',
            approved_by_user_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    db.execute(
        """
        INSERT INTO runs (
            id, platform, channel_id, thread_ts, message_ts, user_id,
            request_text, raw_event_json, intent, status
        ) VALUES ('run-id', 'slack', 'C123', 'T1', 'T1', 'U1', '@monica bug', '{}', 'agentic_triage', 'queued')
        """
    )
    db.commit()
    db.close()

    state = MonicaState.open(db_path)

    with sqlite3.connect(db_path) as migrated:
        indexes = migrated.execute("PRAGMA index_list(runs)").fetchall()
        unique_indexes = {row[1] for row in indexes if row[2]}

    assert "idx_monica_runs_thread_unique" in unique_indexes
    run, created = state.create_run_once(
        platform="slack",
        channel_id="C123",
        thread_ts="T1",
        message_ts="T2",
        user_id="U2",
        request_text="@monica same thread",
    )
    assert created is False
    assert run.id == "run-id"


def test_state_migration_collapses_duplicate_legacy_thread_rows_before_indexing(tmp_path):
    db_path = tmp_path / "old-with-duplicates.sqlite"
    db = sqlite3.connect(db_path)
    db.execute(
        """
        CREATE TABLE runs (
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
            pr_url TEXT NOT NULL DEFAULT '',
            failure_reason TEXT NOT NULL DEFAULT '',
            approved_by_user_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    db.execute(
        """
        INSERT INTO runs (
            id, platform, channel_id, thread_ts, message_ts, user_id,
            request_text, raw_event_json, intent, status
        ) VALUES ('run-empty', 'slack', 'C123', 'T1', 'T1', 'U1', '@monica bug', '{}', 'agentic_triage', 'queued')
        """
    )
    db.execute(
        """
        INSERT INTO runs (
            id, platform, channel_id, thread_ts, message_ts, user_id,
            request_text, raw_event_json, intent, status,
            linear_identifier, linear_issue_id, linear_url, branch_name, pr_url
        ) VALUES (
            'run-done', 'slack', 'C123', 'T1', 'T2', 'U2', '@monica same bug',
            '{}', 'agentic_triage', 'done',
            'MOB-42', 'issue-id', 'https://linear.app/acme/issue/MOB-42',
            'monica/MOB-42-checkout-crash', 'https://github.com/acme/mobile/pull/42'
        )
        """
    )
    db.commit()
    db.close()

    state = MonicaState.open(db_path)

    runs = state.list_runs()
    assert [run.id for run in runs] == ["run-done"]
    assert runs[0].linear_identifier == "MOB-42"
    assert runs[0].pr_url.endswith("/42")
    run, created = state.create_run_once(
        platform="slack",
        channel_id="C123",
        thread_ts="T1",
        message_ts="T3",
        user_id="U3",
        request_text="@monica duplicate attempt",
    )
    assert created is False
    assert run.id == "run-done"


def test_state_lists_runtime_sync_blocking_runs(tmp_path):
    state = MonicaState.open(tmp_path / "monica.sqlite")
    for status in (
        "queued",
        "triaging",
        "awaiting_fix_approval",
        "approved",
        "fixing",
        "verifying",
        "proofing",
        "opening_pr",
        "proof_blocked",
        "needs_clarification",
        "blocked",
        "failed",
        "done",
    ):
        run = state.create_run(
            platform="slack",
            channel_id=f"C_{status}",
            thread_ts=status,
            message_ts=status,
            user_id="U1",
            request_text=f"@monica {status}",
        )
        state.update_run(run.id, status=status)

    blocking = state.list_runtime_sync_blocking_runs()

    assert [run.status for run in blocking] == [
        "opening_pr",
        "proof_blocked",
        "proofing",
        "verifying",
        "fixing",
        "approved",
        "awaiting_fix_approval",
        "triaging",
        "queued",
    ]
    assert not state.is_idle_for_runtime_sync()
