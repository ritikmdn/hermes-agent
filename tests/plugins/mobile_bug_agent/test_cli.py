from __future__ import annotations

import copy
import json
import re
from argparse import ArgumentParser
from types import SimpleNamespace
from typing import Any

from plugins.mobile_bug_agent import cli
from plugins.mobile_bug_agent.cli import (
    _open_state,
    run_approve_command,
    run_configure_approved_pr_command,
    run_configure_linear_only_command,
    run_configure_local_fix_only_command,
    run_doctor_command,
    run_linear_metadata_command,
    run_retry_command,
    run_setup_plan_command,
    run_simulate_command,
    run_show_command,
    run_slack_manifest_command,
    run_slack_metadata_command,
    run_status_command,
    run_sync_approvals_command,
)
from plugins.mobile_bug_agent.config import (
    LinearConfig,
    LoopConfig,
    MonicaConfig,
    ProofConfig,
    RepoConfig,
    RuntimeConfig,
    SlackConfig,
    VerificationConfig,
    WorkerConfig,
    config_from_mapping,
)
from plugins.mobile_bug_agent.state import MonicaState, RUNTIME_SYNC_BLOCKING_STATUSES


def test_status_lists_recent_runs(tmp_path, capsys):
    state = MonicaState.open(tmp_path / "state.sqlite")
    state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="T1",
        message_ts="T1",
        user_id="U1",
        request_text="@monica checkout crash",
    )

    exit_code = run_status_command(state=state, limit=5)

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "checkout crash" in out


def test_status_lists_rollout_context_for_recent_runs(tmp_path, capsys):
    state = MonicaState.open(tmp_path / "state.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U1",
        request_text="@monica checkout crash",
    )
    state.update_run(
        run.id,
        status="failed",
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        branch_name="monica/MOB-123-checkout-crash",
    )

    exit_code = run_status_command(state=state, limit=5)

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "slack:C123/1710000000.000100" in out
    assert "linear:MOB-123" in out
    assert "branch:monica/MOB-123-checkout-crash" in out
    assert "url:https://linear.app/acme/issue/MOB-123" in out


def test_status_json_lists_recent_runs(tmp_path, capsys):
    state = MonicaState.open(tmp_path / "state.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U1",
        request_text="@monica checkout crash",
    )
    state.update_run(
        run.id,
        status="awaiting_fix_approval",
        linear_identifier="MOB-123",
        linear_url="https://linear.app/acme/issue/MOB-123",
        branch_name="monica/MOB-123-checkout-crash",
        base_branch="origin/dev",
        base_commit="abc123base",
        proof_deep_link="elixir-card://marketplace/offer/fitness-first",
        proof_expected_text="Fitness First",
    )

    exit_code = run_status_command(
        state=state,
        limit=5,
        json_output=True,
        current_commit="fed67890",
    )

    out = capsys.readouterr().out
    payload = json.loads(out.strip().splitlines()[-1])
    assert exit_code == 0
    assert payload["count"] == 1
    assert payload["runs"][0]["id"] == run.id
    assert payload["runs"][0]["status"] == "awaiting_fix_approval"
    assert payload["runs"][0]["slack"]["channel_id"] == "C123"
    assert payload["runs"][0]["linear"]["identifier"] == "MOB-123"
    assert payload["runs"][0]["branch_name"] == "monica/MOB-123-checkout-crash"
    assert payload["runs"][0]["base"] == {
        "ref": "origin/dev",
        "commit": "abc123base",
    }
    assert payload["runs"][0]["proof_target"] == {
        "deep_link": "elixir-card://marketplace/offer/fitness-first",
        "expected_text": "Fitness First",
    }
    assert payload["runs"][0]["request_text"] == "@monica checkout crash"


def test_status_json_reports_runtime_sync_guard(tmp_path, capsys):
    state = MonicaState.open(tmp_path / "state.sqlite")
    state.record_runtime_sync(commit="abc12345", synced_at="2026-06-13T10:00:00Z")
    active = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U1",
        request_text="@monica checkout crash",
    )
    state.update_run(active.id, status="proofing", linear_identifier="MOB-123")
    waiting = state.create_run(
        platform="slack",
        channel_id="C456",
        thread_ts="1710000001.000100",
        message_ts="1710000001.000100",
        user_id="U2",
        request_text="@monica another bug",
    )
    state.update_run(waiting.id, status="awaiting_fix_approval", linear_identifier="MOB-456")

    exit_code = run_status_command(
        state=state,
        limit=5,
        json_output=True,
        current_commit="fed67890",
    )

    out = capsys.readouterr().out
    payload = json.loads(out.strip().splitlines()[-1])
    assert exit_code == 0
    assert payload["runtime_sync"]["idle"] is False
    assert payload["runtime_sync"]["current_commit"] == "fed67890"
    assert payload["runtime_sync"]["stale"] is True
    assert payload["runtime_sync"]["last_synced_commit"] == "abc12345"
    assert payload["runtime_sync"]["last_synced_at"] == "2026-06-13T10:00:00Z"
    assert payload["runtime_sync"]["blocking_statuses"] == list(RUNTIME_SYNC_BLOCKING_STATUSES)
    assert payload["runtime_sync"]["active_run_count"] == 2
    assert payload["runtime_sync"]["active_runs"][0]["id"] == active.id
    assert payload["runtime_sync"]["active_runs"][0]["status"] == "proofing"
    assert payload["runtime_sync"]["active_runs"][1]["id"] == waiting.id
    assert payload["runtime_sync"]["active_runs"][1]["status"] == "awaiting_fix_approval"


def test_status_json_reports_runtime_sync_lease(tmp_path, capsys):
    state = MonicaState.open(tmp_path / "state.sqlite")
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

    exit_code = run_status_command(
        state=state,
        limit=5,
        json_output=True,
        current_commit="dead1234",
    )

    out = capsys.readouterr().out
    payload = json.loads(out.strip().splitlines()[-1])
    assert exit_code == 0
    assert payload["runtime_sync"]["idle"] is True
    assert payload["runtime_sync"]["ready_for_monica_work"] is False
    assert payload["runtime_sync"]["lease"]["active"] is True
    assert payload["runtime_sync"]["lease"]["id"] == lease.lease_id
    assert payload["runtime_sync"]["lease"]["pre_update_commit"] == "dead1234"


def test_status_text_reports_runtime_sync_lease(tmp_path, capsys):
    state = MonicaState.open(tmp_path / "state.sqlite")
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

    exit_code = run_status_command(state=state, limit=5, current_commit="dead1234")

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Runtime sync: updating" in out
    assert f"lease:{lease.lease_id}" in out


def test_status_json_reports_gateway_health_and_commit(tmp_path, capsys):
    state = MonicaState.open(tmp_path / "state.sqlite")

    exit_code = run_status_command(
        state=state,
        limit=5,
        json_output=True,
        gateway_status={
            "gateway_state": "running",
            "pid": 1234,
            "start_time": 100.0,
            "active_agents": 2,
            "updated_at": "2026-06-13T00:00:00Z",
        },
        current_commit="abc12345",
        now=160.0,
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["health"] == {
        "hermes_commit": "abc12345",
        "gateway": {
            "state": "running",
            "pid": 1234,
            "active_agents": 2,
            "start_time": 100.0,
            "uptime_seconds": 60,
            "updated_at": "2026-06-13T00:00:00Z",
            "updated_age_seconds": 0,
            "stale": False,
        },
    }


def test_status_json_tolerates_malformed_gateway_active_agents(tmp_path, capsys):
    state = MonicaState.open(tmp_path / "state.sqlite")

    exit_code = run_status_command(
        state=state,
        limit=5,
        json_output=True,
        gateway_status={
            "gateway_state": "running",
            "pid": 1234,
            "start_time": 100.0,
            "active_agents": "not-an-int",
            "updated_at": "2026-06-13T00:00:00Z",
        },
        current_commit="abc12345",
        now=160.0,
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["health"]["gateway"]["active_agents"] == 0


def test_status_text_reports_health_and_runtime_sync(tmp_path, capsys):
    state = MonicaState.open(tmp_path / "state.sqlite")
    state.record_runtime_sync(commit="c0ffee12", synced_at="2026-06-13T10:00:00Z")
    active = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U1",
        request_text="@monica marketplace PDP copy bug",
    )
    state.update_run(active.id, status="proofing", linear_identifier="MOB-123")

    exit_code = run_status_command(
        state=state,
        limit=5,
        gateway_status={
            "gateway_state": "running",
            "pid": 1234,
            "start_time": 100.0,
            "active_agents": 2,
            "updated_at": "2026-06-13T00:00:00Z",
        },
        current_commit="abc12345",
        now=160.0,
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Hermes commit: abc12345" in out
    assert "Gateway: running pid:1234 uptime:60s active_agents:2" in out
    assert "Runtime sync: blocked active_runs:1 last_commit:c0ffee12 last_synced:2026-06-13T10:00:00Z" in out
    assert "stale:true" in out
    assert "blocked_by:MOB-123/proofing" in out
    assert "marketplace PDP copy bug" in out


def test_show_missing_run_returns_error(tmp_path, capsys):
    state = MonicaState.open(tmp_path / "state.sqlite")

    exit_code = run_show_command(state=state, run_id="missing")

    assert exit_code == 1
    assert "missing" in capsys.readouterr().out


def test_show_json_outputs_full_run(tmp_path, capsys):
    state = MonicaState.open(tmp_path / "state.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="T1",
        message_ts="T1",
        user_id="U1",
        request_text="@monica checkout crash",
        raw_event={"type": "app_mention", "event_id": "E123"},
    )
    state.update_run(
        run.id,
        status="failed",
        failure_reason="verification failed",
        approved_by_user_id="U_APPROVER",
        pr_url="https://github.com/acme/mobile/pull/123",
    )

    exit_code = run_show_command(state=state, run_id=run.id, json_output=True)

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["id"] == run.id
    assert payload["status"] == "failed"
    assert payload["failure_reason"] == "verification failed"
    assert payload["approved_by_user_id"] == "U_APPROVER"
    assert payload["pr_url"] == "https://github.com/acme/mobile/pull/123"
    assert payload["raw_event"] == {"type": "app_mention", "event_id": "E123"}


def test_retry_refuses_completed_runs(tmp_path, capsys, monkeypatch):
    state = MonicaState.open(tmp_path / "state.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="T1",
        message_ts="T1",
        user_id="U1",
        request_text="@monica checkout crash",
    )
    state.update_run(
        run.id,
        status="done",
        pr_url="https://github.com/acme/mobile/pull/123",
        approved_by_user_id="U1",
    )
    launched: list[str] = []
    monkeypatch.setattr(cli, "_run_loop", lambda run_id, *, state: launched.append(run_id))

    exit_code = run_retry_command(state=state, run_id=run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert exit_code == 1
    assert updated.status == "done"
    assert updated.pr_url == "https://github.com/acme/mobile/pull/123"
    assert launched == []
    assert "not retryable" in capsys.readouterr().out


def test_retry_refuses_cancelled_runs(tmp_path, capsys, monkeypatch):
    state = MonicaState.open(tmp_path / "state.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="T1",
        message_ts="T1",
        user_id="U1",
        request_text="@monica checkout crash",
    )
    state.update_run(
        run.id,
        status="blocked",
        failure_reason="cancelled by U_APPROVER",
    )
    launched: list[str] = []
    monkeypatch.setattr(cli, "_run_loop", lambda run_id, *, state: launched.append(run_id))

    exit_code = run_retry_command(state=state, run_id=run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert exit_code == 1
    assert updated.status == "blocked"
    assert updated.failure_reason == "cancelled by U_APPROVER"
    assert launched == []
    assert "was cancelled from Slack" in capsys.readouterr().out


def test_retry_refuses_during_runtime_sync_lease(tmp_path, capsys, monkeypatch):
    state = MonicaState.open(tmp_path / "state.sqlite")
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
    run = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="T1",
        message_ts="T1",
        user_id="U1",
        request_text="@monica checkout crash",
    )
    state.update_run(
        run.id,
        status="failed",
        failure_reason="opening_pr_failed: gh pr create failed",
        approved_by_user_id="U_APPROVER",
    )
    launched: list[str] = []
    monkeypatch.setattr(cli, "_run_loop", lambda run_id, *, state: launched.append(run_id))

    exit_code = run_retry_command(state=state, run_id=run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert exit_code == 1
    assert updated.status == "failed"
    assert launched == []
    assert "Hermes is updating" in capsys.readouterr().out


def test_retry_failed_approved_run_resumes_from_approval_gate(tmp_path, capsys, monkeypatch):
    state = MonicaState.open(tmp_path / "state.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="T1",
        message_ts="T1",
        user_id="U1",
        request_text="@monica checkout crash",
    )
    state.update_run(
        run.id,
        status="failed",
        failure_reason="opening_pr_failed: gh pr create failed",
        approved_by_user_id="U_APPROVER",
    )
    launched: list[str] = []
    monkeypatch.setattr(cli, "_run_loop", lambda run_id, *, state: launched.append(run_id))

    exit_code = run_retry_command(state=state, run_id=run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert exit_code == 0
    assert updated.status == "approved"
    assert updated.failure_reason == ""
    assert updated.approved_by_user_id == "U_APPROVER"
    assert launched == [run.id]
    assert f"Retried Monica run {run.id}" in capsys.readouterr().out


def test_retry_failed_opening_pr_run_with_proof_metadata_resumes_from_proof_gate(
    tmp_path,
    capsys,
    monkeypatch,
):
    state = MonicaState.open(tmp_path / "state.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="T1",
        message_ts="T1",
        user_id="U1",
        request_text="@monica checkout crash",
    )
    state.update_run(
        run.id,
        status="failed",
        failure_reason="opening_pr_failed: body must include verification evidence.",
        branch_name="monica/MOB-123-checkout-crash",
        base_branch="origin/dev",
        base_commit="abc1234",
        proof_deep_link="elixir-card://marketplace/SearchScreen",
        proof_expected_text="Search wellness products",
        approved_by_user_id="U_APPROVER",
    )
    launched: list[str] = []

    def fake_loop(run_id: str, *, state: MonicaState) -> None:
        relaunched = state.get_run(run_id)
        assert relaunched is not None
        assert relaunched.status == "proof_blocked"
        assert relaunched.failure_reason == ""
        assert relaunched.branch_name == "monica/MOB-123-checkout-crash"
        assert relaunched.base_branch == "origin/dev"
        assert relaunched.base_commit == "abc1234"
        assert relaunched.proof_deep_link == "elixir-card://marketplace/SearchScreen"
        assert relaunched.proof_expected_text == "Search wellness products"
        assert relaunched.pr_url == ""
        launched.append(run_id)

    monkeypatch.setattr(cli, "_run_loop", fake_loop)

    exit_code = run_retry_command(
        state=state,
        run_id=run.id,
        config=MonicaConfig(
            rollout_mode="approved_pr",
            slack=SlackConfig(approver_user_ids=("U_APPROVER",)),
        ),
        readiness_checker=lambda: 0,
    )

    updated = state.get_run(run.id)
    assert updated is not None
    assert exit_code == 0
    assert updated.status == "proof_blocked"
    assert updated.failure_reason == ""
    assert updated.branch_name == "monica/MOB-123-checkout-crash"
    assert updated.base_branch == "origin/dev"
    assert updated.base_commit == "abc1234"
    assert updated.proof_deep_link == "elixir-card://marketplace/SearchScreen"
    assert updated.proof_expected_text == "Search wellness products"
    assert updated.approved_by_user_id == "U_APPROVER"
    assert launched == [run.id]
    assert f"Retried Monica run {run.id}" in capsys.readouterr().out


def test_retry_proof_blocked_run_resumes_from_proof_gate(tmp_path, capsys, monkeypatch):
    state = MonicaState.open(tmp_path / "state.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="T1",
        message_ts="T1",
        user_id="U1",
        request_text="@monica checkout crash",
    )
    state.update_run(
        run.id,
        status="proof_blocked",
        failure_reason="proof_unavailable: simulator not configured",
        branch_name="monica/MOB-123-checkout-crash",
        approved_by_user_id="U_APPROVER",
    )
    launched: list[str] = []

    def fake_loop(run_id: str, *, state: MonicaState) -> None:
        relaunched = state.get_run(run_id)
        assert relaunched is not None
        assert relaunched.status == "proof_blocked"
        assert relaunched.failure_reason == ""
        assert relaunched.branch_name == "monica/MOB-123-checkout-crash"
        assert relaunched.pr_url == ""
        launched.append(run_id)

    monkeypatch.setattr(cli, "_run_loop", fake_loop)

    exit_code = run_retry_command(state=state, run_id=run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert exit_code == 0
    assert updated.status == "proof_blocked"
    assert updated.failure_reason == ""
    assert updated.branch_name == "monica/MOB-123-checkout-crash"
    assert updated.approved_by_user_id == "U_APPROVER"
    assert launched == [run.id]
    assert f"Retried Monica run {run.id}" in capsys.readouterr().out


def test_retry_approved_pr_proof_blocked_run_refuses_when_readiness_check_fails(
    tmp_path,
    capsys,
    monkeypatch,
):
    state = MonicaState.open(tmp_path / "state.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="T1",
        message_ts="T1",
        user_id="U1",
        request_text="@monica marketplace PDP copy bug",
    )
    state.update_run(
        run.id,
        status="proof_blocked",
        failure_reason="proof_unavailable: proof.setup_commands is empty",
        branch_name="monica/MOB-123-marketplace-copy-bug",
        approved_by_user_id="U_APPROVER",
    )
    launched: list[str] = []
    monkeypatch.setattr(cli, "_run_loop", lambda run_id, *, state: launched.append(run_id))

    exit_code = run_retry_command(
        state=state,
        run_id=run.id,
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            slack=SlackConfig(approver_user_ids=("U_APPROVER",)),
        ),
        readiness_checker=lambda: 1,
    )

    updated = state.get_run(run.id)
    out = capsys.readouterr().out
    assert updated is not None
    assert exit_code == 1
    assert updated.status == "proof_blocked"
    assert updated.failure_reason == "proof_unavailable: proof.setup_commands is empty"
    assert updated.branch_name == "monica/MOB-123-marketplace-copy-bug"
    assert launched == []
    assert "Monica readiness check failed" in out


def test_retry_approved_pr_proof_blocked_run_refuses_unconfigured_stored_approver(
    tmp_path,
    capsys,
    monkeypatch,
):
    state = MonicaState.open(tmp_path / "state.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="T1",
        message_ts="T1",
        user_id="U1",
        request_text="@monica marketplace PDP copy bug",
    )
    state.update_run(
        run.id,
        status="proof_blocked",
        failure_reason="proof_unavailable: simulator not configured",
        branch_name="monica/MOB-123-marketplace-copy-bug",
        approved_by_user_id="U_OLD_APPROVER",
    )
    launched: list[str] = []
    monkeypatch.setattr(cli, "_run_loop", lambda run_id, *, state: launched.append(run_id))

    exit_code = run_retry_command(
        state=state,
        run_id=run.id,
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            slack=SlackConfig(approver_user_ids=("U_CURRENT_APPROVER",)),
        ),
        readiness_checker=lambda: 0,
    )

    updated = state.get_run(run.id)
    out = capsys.readouterr().out
    assert updated is not None
    assert exit_code == 1
    assert updated.status == "proof_blocked"
    assert updated.failure_reason == "proof_unavailable: simulator not configured"
    assert updated.branch_name == "monica/MOB-123-marketplace-copy-bug"
    assert updated.approved_by_user_id == "U_OLD_APPROVER"
    assert launched == []
    assert "stored approver U_OLD_APPROVER is not configured" in out


def test_retry_interrupted_proofing_run_resumes_from_proof_gate(tmp_path, capsys, monkeypatch):
    state = MonicaState.open(tmp_path / "state.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="T1",
        message_ts="T1",
        user_id="U1",
        request_text="@monica checkout crash",
    )
    state.update_run(
        run.id,
        status="proofing",
        branch_name="monica/MOB-123-checkout-crash",
        approved_by_user_id="U_APPROVER",
    )
    launched: list[str] = []

    def fake_loop(run_id: str, *, state: MonicaState) -> None:
        relaunched = state.get_run(run_id)
        assert relaunched is not None
        assert relaunched.status == "proof_blocked"
        assert relaunched.failure_reason == ""
        assert relaunched.branch_name == "monica/MOB-123-checkout-crash"
        launched.append(run_id)

    monkeypatch.setattr(cli, "_run_loop", fake_loop)

    exit_code = run_retry_command(state=state, run_id=run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert exit_code == 0
    assert updated.status == "proof_blocked"
    assert updated.branch_name == "monica/MOB-123-checkout-crash"
    assert launched == [run.id]
    assert f"Retried Monica run {run.id}" in capsys.readouterr().out


def test_retry_json_success_outputs_updated_run(tmp_path, capsys, monkeypatch):
    state = MonicaState.open(tmp_path / "state.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="T1",
        message_ts="T1",
        user_id="U1",
        request_text="@monica checkout crash",
    )
    state.update_run(
        run.id,
        status="failed",
        failure_reason="opening_pr_failed: transient gh error",
        approved_by_user_id="U_APPROVER",
    )
    launched: list[str] = []

    def fake_loop(run_id: str, *, state: MonicaState) -> None:
        launched.append(run_id)
        state.update_run(run_id, status="done", pr_url="https://github.com/acme/mobile/pull/123")

    monkeypatch.setattr(cli, "_run_loop", fake_loop)

    exit_code = run_retry_command(state=state, run_id=run.id, json_output=True)

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert launched == [run.id]
    assert payload["ok"] is True
    assert payload["action"] == "retry"
    assert payload["run"]["id"] == run.id
    assert payload["run"]["status"] == "done"
    assert payload["run"]["pr_url"] == "https://github.com/acme/mobile/pull/123"
    assert payload["run"]["approved_by_user_id"] == "U_APPROVER"


def test_retry_json_missing_run_outputs_error(tmp_path, capsys, monkeypatch):
    state = MonicaState.open(tmp_path / "state.sqlite")
    launched: list[str] = []
    monkeypatch.setattr(cli, "_run_loop", lambda run_id, *, state: launched.append(run_id))

    exit_code = run_retry_command(state=state, run_id="missing", json_output=True)

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert launched == []
    assert payload["ok"] is False
    assert payload["action"] == "retry"
    assert payload["error"]["code"] == "not_found"
    assert "missing" in payload["error"]["message"]


def test_retry_approved_run_refuses_unconfigured_stored_approver(tmp_path, capsys, monkeypatch):
    state = MonicaState.open(tmp_path / "state.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="T1",
        message_ts="T1",
        user_id="U1",
        request_text="@monica checkout crash",
    )
    state.update_run(
        run.id,
        status="failed",
        failure_reason="opening_pr_failed: gh pr create failed",
        approved_by_user_id="U_OLD_APPROVER",
    )
    launched: list[str] = []
    monkeypatch.setattr(cli, "_run_loop", lambda run_id, *, state: launched.append(run_id))

    exit_code = run_retry_command(
        state=state,
        run_id=run.id,
        config=MonicaConfig(slack=SlackConfig(approver_user_ids=("U_CURRENT_APPROVER",))),
    )

    updated = state.get_run(run.id)
    assert updated is not None
    assert exit_code == 1
    assert updated.status == "failed"
    assert updated.failure_reason == "opening_pr_failed: gh pr create failed"
    assert updated.approved_by_user_id == "U_OLD_APPROVER"
    assert launched == []
    assert "stored approver U_OLD_APPROVER is not configured" in capsys.readouterr().out


def test_retry_approved_run_refuses_when_readiness_check_fails(tmp_path, capsys, monkeypatch):
    state = MonicaState.open(tmp_path / "state.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="T1",
        message_ts="T1",
        user_id="U1",
        request_text="@monica checkout crash",
    )
    state.update_run(
        run.id,
        status="failed",
        failure_reason="opening_pr_failed: gh pr create failed",
        approved_by_user_id="U_APPROVER",
    )
    launched: list[str] = []
    monkeypatch.setattr(cli, "_run_loop", lambda run_id, *, state: launched.append(run_id))

    exit_code = run_retry_command(
        state=state,
        run_id=run.id,
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            slack=SlackConfig(approver_user_ids=("U_APPROVER",)),
        ),
        readiness_checker=lambda: 1,
    )

    updated = state.get_run(run.id)
    out = capsys.readouterr().out
    assert updated is not None
    assert exit_code == 1
    assert updated.status == "failed"
    assert updated.failure_reason == "opening_pr_failed: gh pr create failed"
    assert updated.approved_by_user_id == "U_APPROVER"
    assert launched == []
    assert "Monica readiness check failed" in out


def test_mobile_command_retry_refuses_when_plugin_is_not_enabled(
    tmp_path, capsys, monkeypatch
):
    runtime = tmp_path / "monica-runtime"
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        runtime=RuntimeConfig(home_subdir=str(runtime)),
        slack=SlackConfig(approver_user_ids=("U_APPROVER",)),
    )
    state = MonicaState.open(runtime / "state.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="T1",
        message_ts="T1",
        user_id="U1",
        request_text="@monica marketplace PDP copy bug",
    )
    state.update_run(
        run.id,
        status="failed",
        failure_reason="opening_pr_failed: gh pr create failed",
        approved_by_user_id="U_APPROVER",
    )
    ready_report = SimpleNamespace(
        ready=True,
        rollout_mode="approved_pr",
        runtime_root_value=str(runtime),
        warnings=(),
        failures=(),
    )
    hermes_config = {
        "plugins": {"enabled": ["other-plugin"], "disabled": []},
        "mobile_bug_agent": {"enabled": True, "rollout_mode": "approved_pr"},
    }
    launched: list[str] = []
    monkeypatch.setattr(cli, "load_monica_config", lambda: config)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: copy.deepcopy(hermes_config))
    monkeypatch.setattr(cli, "check_monica_readiness", lambda **_kwargs: ready_report)
    monkeypatch.setattr(
        cli,
        "_read_gateway_status",
        lambda: {"gateway_state": "running", "pid": 53960, "active_agents": 0},
    )
    monkeypatch.setattr(cli, "_current_hermes_commit", lambda: "abc12345")
    monkeypatch.setattr(cli, "_run_loop", lambda run_id, *, state: launched.append(run_id))

    exit_code = cli.mobile_bug_agent_command(
        SimpleNamespace(mobile_bug_agent_action="retry", run_id=run.id, json=True)
    )

    updated = state.get_run(run.id)
    out = capsys.readouterr().out
    payload = json.loads(out.strip().splitlines()[-1])
    assert updated is not None
    assert exit_code == 1
    assert updated.status == "failed"
    assert updated.failure_reason == "opening_pr_failed: gh pr create failed"
    assert updated.approved_by_user_id == "U_APPROVER"
    assert launched == []
    assert payload["error"]["code"] == "readiness_failed"


def test_retry_refuses_run_that_already_has_pr_url(tmp_path, capsys, monkeypatch):
    state = MonicaState.open(tmp_path / "state.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="T1",
        message_ts="T1",
        user_id="U1",
        request_text="@monica checkout crash",
    )
    state.update_run(
        run.id,
        status="failed",
        failure_reason="opening_pr_failed: transient Slack post failed",
        branch_name="monica/MOB-123-checkout-crash",
        pr_url="https://github.com/acme/mobile/pull/123",
        approved_by_user_id="U_APPROVER",
    )
    launched: list[str] = []
    monkeypatch.setattr(cli, "_run_loop", lambda run_id, *, state: launched.append(run_id))

    exit_code = run_retry_command(state=state, run_id=run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert exit_code == 1
    assert updated.status == "failed"
    assert updated.failure_reason == "opening_pr_failed: transient Slack post failed"
    assert updated.branch_name == "monica/MOB-123-checkout-crash"
    assert updated.pr_url == "https://github.com/acme/mobile/pull/123"
    assert launched == []
    assert "already has a draft PR" in capsys.readouterr().out


def test_retry_clears_stale_branch_metadata_before_relaunch(tmp_path, capsys, monkeypatch):
    state = MonicaState.open(tmp_path / "state.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="T1",
        message_ts="T1",
        user_id="U1",
        request_text="@monica checkout crash",
    )
    state.update_run(
        run.id,
        status="failed",
        failure_reason="opening_pr_failed: stale gh failure",
        branch_name="monica/old-branch",
        base_branch="origin/dev",
        base_commit="abc1234",
        proof_deep_link="elixir-card://marketplace/offer/old-offer",
        proof_expected_text="Old Offer",
    )
    relaunched_state: list[tuple[str, str, str, str, str, str, str, str]] = []

    def fake_run_loop(run_id: str, *, state: MonicaState) -> None:
        current = state.get_run(run_id)
        assert current is not None
        relaunched_state.append(
            (
                current.status,
                current.failure_reason,
                current.branch_name,
                current.base_branch,
                current.base_commit,
                current.proof_deep_link,
                current.proof_expected_text,
                current.pr_url,
            )
        )

    monkeypatch.setattr(cli, "_run_loop", fake_run_loop)

    exit_code = run_retry_command(state=state, run_id=run.id)

    updated = state.get_run(run.id)
    assert updated is not None
    assert exit_code == 0
    assert updated.status == "queued"
    assert updated.failure_reason == ""
    assert updated.branch_name == ""
    assert updated.base_branch == ""
    assert updated.base_commit == ""
    assert updated.proof_deep_link == ""
    assert updated.proof_expected_text == ""
    assert updated.pr_url == ""
    assert relaunched_state == [("queued", "", "", "", "", "", "", "")]
    assert f"Retried Monica run {run.id}" in capsys.readouterr().out


def test_approve_refuses_completed_runs(tmp_path, capsys, monkeypatch):
    state = MonicaState.open(tmp_path / "state.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="T1",
        message_ts="T1",
        user_id="U1",
        request_text="@monica checkout crash",
    )
    state.update_run(
        run.id,
        status="done",
        pr_url="https://github.com/acme/mobile/pull/123",
    )
    launched: list[str] = []
    monkeypatch.setattr(cli, "_run_loop", lambda run_id, *, state: launched.append(run_id))

    exit_code = run_approve_command(state=state, run_id=run.id, user_id="local-operator")

    updated = state.get_run(run.id)
    assert updated is not None
    assert exit_code == 1
    assert updated.status == "done"
    assert updated.approved_by_user_id == ""
    assert launched == []
    assert "not awaiting fix approval" in capsys.readouterr().out


def test_approve_waiting_run_marks_approved_and_invokes_loop(tmp_path, capsys, monkeypatch):
    state = MonicaState.open(tmp_path / "state.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="T1",
        message_ts="T1",
        user_id="U1",
        request_text="@monica checkout crash",
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123")
    launched: list[str] = []
    monkeypatch.setattr(cli, "_run_loop", lambda run_id, *, state: launched.append(run_id))

    exit_code = run_approve_command(state=state, run_id=run.id, user_id="local-operator")

    updated = state.get_run(run.id)
    assert updated is not None
    assert exit_code == 0
    assert updated.status == "approved"
    assert updated.approved_by_user_id == "local-operator"
    assert launched == [run.id]
    assert f"Approved Monica run {run.id}" in capsys.readouterr().out


def test_approve_refuses_during_runtime_sync_lease(tmp_path, capsys, monkeypatch):
    state = MonicaState.open(tmp_path / "state.sqlite")
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
    run = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="T1",
        message_ts="T1",
        user_id="U1",
        request_text="@monica checkout crash",
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123")
    launched: list[str] = []
    monkeypatch.setattr(cli, "_run_loop", lambda run_id, *, state: launched.append(run_id))

    exit_code = run_approve_command(state=state, run_id=run.id, user_id="local-operator")

    updated = state.get_run(run.id)
    assert updated is not None
    assert exit_code == 1
    assert updated.status == "awaiting_fix_approval"
    assert updated.approved_by_user_id == ""
    assert launched == []
    assert "Hermes is updating" in capsys.readouterr().out


def test_approve_json_success_outputs_updated_run(tmp_path, capsys, monkeypatch):
    state = MonicaState.open(tmp_path / "state.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="T1",
        message_ts="T1",
        user_id="U1",
        request_text="@monica checkout crash",
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123")
    launched: list[str] = []

    def fake_loop(run_id: str, *, state: MonicaState) -> None:
        launched.append(run_id)
        state.update_run(run_id, status="fixing")

    monkeypatch.setattr(cli, "_run_loop", fake_loop)

    exit_code = run_approve_command(
        state=state,
        run_id=run.id,
        user_id="local-operator",
        json_output=True,
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert launched == [run.id]
    assert payload["ok"] is True
    assert payload["action"] == "approve"
    assert payload["run"]["id"] == run.id
    assert payload["run"]["status"] == "fixing"
    assert payload["run"]["approved_by_user_id"] == "local-operator"


def test_approve_json_unconfigured_user_outputs_error(tmp_path, capsys, monkeypatch):
    state = MonicaState.open(tmp_path / "state.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="T1",
        message_ts="T1",
        user_id="U1",
        request_text="@monica checkout crash",
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123")
    launched: list[str] = []
    monkeypatch.setattr(cli, "_run_loop", lambda run_id, *, state: launched.append(run_id))

    exit_code = run_approve_command(
        state=state,
        run_id=run.id,
        user_id="local-operator",
        config=MonicaConfig(slack=SlackConfig(approver_user_ids=("U_APPROVER",))),
        json_output=True,
    )

    updated = state.get_run(run.id)
    out = capsys.readouterr().out
    payload = json.loads(out.strip().splitlines()[-1])
    assert updated is not None
    assert exit_code == 1
    assert updated.status == "awaiting_fix_approval"
    assert updated.approved_by_user_id == ""
    assert launched == []
    assert payload["ok"] is False
    assert payload["action"] == "approve"
    assert payload["error"]["code"] == "approver_not_allowed"
    assert payload["run"]["id"] == run.id
    assert payload["run"]["status"] == "awaiting_fix_approval"


def test_approve_refuses_user_outside_configured_approver_allowlist(tmp_path, capsys, monkeypatch):
    state = MonicaState.open(tmp_path / "state.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="T1",
        message_ts="T1",
        user_id="U1",
        request_text="@monica checkout crash",
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123")
    launched: list[str] = []
    monkeypatch.setattr(cli, "_run_loop", lambda run_id, *, state: launched.append(run_id))

    exit_code = run_approve_command(
        state=state,
        run_id=run.id,
        user_id="local-operator",
        config=MonicaConfig(slack=SlackConfig(approver_user_ids=("U_APPROVER",))),
    )

    updated = state.get_run(run.id)
    assert updated is not None
    assert exit_code == 1
    assert updated.status == "awaiting_fix_approval"
    assert updated.approved_by_user_id == ""
    assert launched == []
    assert "not configured as a Monica code approver" in capsys.readouterr().out


def test_approve_refuses_approved_pr_when_readiness_check_fails(tmp_path, capsys, monkeypatch):
    state = MonicaState.open(tmp_path / "state.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="T1",
        message_ts="T1",
        user_id="U1",
        request_text="@monica checkout crash",
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123")
    launched: list[str] = []
    monkeypatch.setattr(cli, "_run_loop", lambda run_id, *, state: launched.append(run_id))

    exit_code = run_approve_command(
        state=state,
        run_id=run.id,
        user_id="U_APPROVER",
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            slack=SlackConfig(approver_user_ids=("U_APPROVER",)),
        ),
        readiness_checker=lambda: 1,
    )

    updated = state.get_run(run.id)
    out = capsys.readouterr().out
    assert updated is not None
    assert exit_code == 1
    assert updated.status == "awaiting_fix_approval"
    assert updated.approved_by_user_id == ""
    assert launched == []
    assert "Monica readiness check failed" in out


def test_mobile_command_approve_refuses_when_plugin_is_not_enabled(
    tmp_path, capsys, monkeypatch
):
    runtime = tmp_path / "monica-runtime"
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        runtime=RuntimeConfig(home_subdir=str(runtime)),
        slack=SlackConfig(approver_user_ids=("U_APPROVER",)),
    )
    state = MonicaState.open(runtime / "state.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="T1",
        message_ts="T1",
        user_id="U1",
        request_text="@monica marketplace PDP copy bug",
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123")
    ready_report = SimpleNamespace(
        ready=True,
        rollout_mode="approved_pr",
        runtime_root_value=str(runtime),
        warnings=(),
        failures=(),
    )
    hermes_config = {
        "plugins": {"enabled": ["other-plugin"], "disabled": []},
        "mobile_bug_agent": {"enabled": True, "rollout_mode": "approved_pr"},
    }
    launched: list[str] = []
    monkeypatch.setattr(cli, "load_monica_config", lambda: config)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: copy.deepcopy(hermes_config))
    monkeypatch.setattr(cli, "check_monica_readiness", lambda **_kwargs: ready_report)
    monkeypatch.setattr(
        cli,
        "_read_gateway_status",
        lambda: {"gateway_state": "running", "pid": 53960, "active_agents": 0},
    )
    monkeypatch.setattr(cli, "_current_hermes_commit", lambda: "abc12345")
    monkeypatch.setattr(cli, "_run_loop", lambda run_id, *, state: launched.append(run_id))

    exit_code = cli.mobile_bug_agent_command(
        SimpleNamespace(
            mobile_bug_agent_action="approve",
            run_id=run.id,
            user_id="U_APPROVER",
            json=True,
        )
    )

    updated = state.get_run(run.id)
    out = capsys.readouterr().out
    payload = json.loads(out.strip().splitlines()[-1])
    assert updated is not None
    assert exit_code == 1
    assert updated.status == "awaiting_fix_approval"
    assert updated.approved_by_user_id == ""
    assert launched == []
    assert payload["error"]["code"] == "readiness_failed"
    assert payload["run"]["id"] == run.id


def test_approve_does_not_launch_when_atomic_approval_already_changed(tmp_path, capsys, monkeypatch):
    state = MonicaState.open(tmp_path / "state.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="T1",
        message_ts="T1",
        user_id="U1",
        request_text="@monica checkout crash",
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123")
    launched: list[str] = []
    monkeypatch.setattr(cli, "_run_loop", lambda run_id, *, state: launched.append(run_id))

    def approve_once(run_id: str, *, approved_by_user_id: str):
        approved = state.update_run(run_id, status="approved", approved_by_user_id="U_OTHER")
        return approved, False

    monkeypatch.setattr(state, "approve_fix_once", approve_once)

    exit_code = run_approve_command(state=state, run_id=run.id, user_id="local-operator")

    updated = state.get_run(run.id)
    assert updated is not None
    assert exit_code == 1
    assert updated.status == "approved"
    assert updated.approved_by_user_id == "U_OTHER"
    assert launched == []
    assert "already approved or no longer awaiting fix approval" in capsys.readouterr().out


def test_sync_approvals_reads_slack_thread_and_invokes_loop(tmp_path, capsys, monkeypatch):
    state = MonicaState.open(tmp_path / "state.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_REPORTER",
        request_text="@monica checkout crash",
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123")
    launched: list[str] = []
    clients: list[Any] = []
    monkeypatch.setattr(cli, "monica_slack_bot_token", lambda: "xoxb-token")

    class FakeClient:
        def __init__(self, *, token, monica_user_ids, download_attachments):
            self.token = token
            self.monica_user_ids = monica_user_ids
            self.download_attachments = download_attachments
            clients.append(self)

        def read_thread(self, *, channel_id, thread_ts, limit):
            assert channel_id == "C123"
            assert thread_ts == "1710000000.000100"
            assert limit >= 2
            return SimpleNamespace(
                messages=[
                    SimpleNamespace(ts="1710000000.000100", user_id="U_REPORTER", text="@monica checkout crash"),
                    SimpleNamespace(ts="4102444800.000100", user_id="U_APPROVER", text="<@BMONICA> approved, fix it"),
                ]
            )

    def fake_loop(run_id: str, *, state: MonicaState) -> None:
        launched.append(run_id)
        state.update_run(run_id, status="done", pr_url="https://github.com/acme/mobile/pull/123")

    monkeypatch.setattr(cli, "_run_loop", fake_loop)

    exit_code = run_sync_approvals_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            slack=SlackConfig(approver_user_ids=("U_APPROVER",), bot_user_ids=("BMONICA",)),
        ),
        state=state,
        run_id=run.id,
        json_output=True,
        client_factory=FakeClient,
        readiness_checker=lambda: 0,
    )

    payload = json.loads(capsys.readouterr().out)
    updated = state.get_run(run.id)
    assert updated is not None
    assert exit_code == 0
    assert launched == [run.id]
    assert clients[0].token == "xoxb-token"
    assert clients[0].monica_user_ids == ("BMONICA",)
    assert clients[0].download_attachments is False
    assert updated.status == "done"
    assert updated.approved_by_user_id == "U_APPROVER"
    assert payload == {
        "ok": True,
                "results": [
                    {
                        "action": "approved",
                        "approval_message_ts": "4102444800.000100",
                        "approved_by_user_id": "U_APPROVER",
                "channel_id": "C123",
                "final_status": "done",
                "pr_url": "https://github.com/acme/mobile/pull/123",
                "run_id": run.id,
                "status": "awaiting_fix_approval",
                "thread_ts": "1710000000.000100",
            }
        ],
    }


def test_sync_approvals_refuses_during_runtime_sync_lease(tmp_path, capsys, monkeypatch):
    state = MonicaState.open(tmp_path / "state.sqlite")
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
    run = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_REPORTER",
        request_text="@monica checkout crash",
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123")
    monkeypatch.setattr(cli, "monica_slack_bot_token", lambda: "xoxb-token")

    exit_code = run_sync_approvals_command(
        config=MonicaConfig(slack=SlackConfig(bot_user_ids=("BMONICA",), approver_user_ids=("U_APPROVER",))),
        state=state,
    )

    updated = state.get_run(run.id)
    assert updated is not None
    assert exit_code == 1
    assert updated.status == "awaiting_fix_approval"
    assert "Hermes is updating" in capsys.readouterr().out


def test_sync_approvals_requires_recovered_approval_to_tag_monica(tmp_path, capsys, monkeypatch):
    state = MonicaState.open(tmp_path / "state.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_REPORTER",
        request_text="@monica checkout crash",
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123")
    launched: list[str] = []
    monkeypatch.setattr(cli, "monica_slack_bot_token", lambda: "xoxb-token")
    monkeypatch.setattr(cli, "_run_loop", lambda run_id, *, state: launched.append(run_id))

    class FakeClient:
        def __init__(self, **_: Any):
            pass

        def read_thread(self, **_: Any):
            return SimpleNamespace(
                messages=[
                    SimpleNamespace(
                        ts="4102444800.000100",
                        user_id="U_APPROVER",
                        text="approved, fix it",
                    ),
                ]
            )

    exit_code = run_sync_approvals_command(
        config=MonicaConfig(
            rollout_mode="approved_pr",
            slack=SlackConfig(
                approver_user_ids=("U_APPROVER",),
                bot_user_ids=("BMONICA",),
            ),
        ),
        state=state,
        run_id=run.id,
        json_output=True,
        client_factory=FakeClient,
        readiness_checker=lambda: 0,
    )

    payload = json.loads(capsys.readouterr().out)
    updated = state.get_run(run.id)
    assert updated is not None
    assert exit_code == 0
    assert launched == []
    assert updated.status == "awaiting_fix_approval"
    assert updated.approved_by_user_id == ""
    assert payload["ok"] is True
    assert payload["results"][0]["action"] == "skipped"
    assert payload["results"][0]["reason"] == "no configured approver approval found in Slack thread"


def test_mobile_command_sync_approvals_refuses_when_plugin_is_not_enabled(
    tmp_path, capsys, monkeypatch
):
    runtime = tmp_path / "monica-runtime"
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        runtime=RuntimeConfig(home_subdir=str(runtime)),
        slack=SlackConfig(approver_user_ids=("U_APPROVER",), bot_user_ids=("BMONICA",)),
    )
    state = MonicaState.open(runtime / "state.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_REPORTER",
        request_text="@monica marketplace PDP copy bug",
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123")
    ready_report = SimpleNamespace(
        ready=True,
        rollout_mode="approved_pr",
        runtime_root_value=str(runtime),
        warnings=(),
        failures=(),
    )
    hermes_config = {
        "plugins": {"enabled": ["other-plugin"], "disabled": []},
        "mobile_bug_agent": {"enabled": True, "rollout_mode": "approved_pr"},
    }
    launched: list[str] = []

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def read_thread(self, *, channel_id, thread_ts, limit):
            return SimpleNamespace(
                messages=[
                    SimpleNamespace(ts="1710000000.000100", user_id="U_REPORTER", text="@monica bug"),
                    SimpleNamespace(ts="4102444800.000100", user_id="U_APPROVER", text="<@BMONICA> approved, fix it"),
                ]
            )

    monkeypatch.setattr(cli, "load_monica_config", lambda: config)
    monkeypatch.setattr(cli, "monica_slack_bot_token", lambda: "xoxb-token")
    monkeypatch.setitem(
        cli.run_sync_approvals_command.__kwdefaults__,
        "client_factory",
        FakeClient,
    )
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: copy.deepcopy(hermes_config))
    monkeypatch.setattr(cli, "check_monica_readiness", lambda **_kwargs: ready_report)
    monkeypatch.setattr(
        cli,
        "_read_gateway_status",
        lambda: {"gateway_state": "running", "pid": 53960, "active_agents": 0},
    )
    monkeypatch.setattr(cli, "_current_hermes_commit", lambda: "abc12345")
    monkeypatch.setattr(cli, "_run_loop", lambda run_id, *, state: launched.append(run_id))

    exit_code = cli.mobile_bug_agent_command(
        SimpleNamespace(
            mobile_bug_agent_action="sync_approvals",
            run_id=run.id,
            limit=20,
            json=True,
        )
    )

    updated = state.get_run(run.id)
    out = capsys.readouterr().out
    payload = json.loads(out.strip().splitlines()[-1])
    assert updated is not None
    assert exit_code == 1
    assert updated.status == "awaiting_fix_approval"
    assert updated.approved_by_user_id == ""
    assert launched == []
    assert payload["ok"] is False
    assert payload["results"][0]["action"] == "error"
    assert payload["results"][0]["error"]["code"] == "readiness_failed"


def test_sync_approvals_ignores_non_approver_and_question_text(tmp_path, capsys, monkeypatch):
    state = MonicaState.open(tmp_path / "state.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_REPORTER",
        request_text="@monica checkout crash",
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123")
    launched: list[str] = []
    monkeypatch.setattr(cli, "monica_slack_bot_token", lambda: "xoxb-token")
    monkeypatch.setattr(cli, "_run_loop", lambda run_id, *, state: launched.append(run_id))

    class FakeClient:
        def __init__(self, **_: Any):
            pass

        def read_thread(self, **_: Any):
            return SimpleNamespace(
                messages=[
                    SimpleNamespace(ts="4102444800.000100", user_id="U_RANDOM", text="approved, fix it"),
                    SimpleNamespace(ts="4102444801.000100", user_id="U_APPROVER", text="should I approve?"),
                    SimpleNamespace(ts="4102444802.000100", user_id="U_APPROVER", text="not approved"),
                ]
            )

    exit_code = run_sync_approvals_command(
        config=MonicaConfig(slack=SlackConfig(approver_user_ids=("U_APPROVER",))),
        state=state,
        run_id=run.id,
        json_output=True,
        client_factory=FakeClient,
    )

    payload = json.loads(capsys.readouterr().out)
    updated = state.get_run(run.id)
    assert updated is not None
    assert exit_code == 0
    assert launched == []
    assert updated.status == "awaiting_fix_approval"
    assert updated.approved_by_user_id == ""
    assert payload["ok"] is True
    assert payload["results"][0]["action"] == "skipped"
    assert payload["results"][0]["reason"] == "no configured approver approval found in Slack thread"


def test_sync_approvals_ignores_approval_that_predates_waiting_state(tmp_path, capsys, monkeypatch):
    state = MonicaState.open(tmp_path / "state.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U_REPORTER",
        request_text="@monica marketplace PDP copy bug",
    )
    state.update_run(run.id, status="awaiting_fix_approval", linear_identifier="MOB-123")
    launched: list[str] = []
    monkeypatch.setattr(cli, "monica_slack_bot_token", lambda: "xoxb-token")
    monkeypatch.setattr(cli, "_run_loop", lambda run_id, *, state: launched.append(run_id))

    class FakeClient:
        def __init__(self, **_: Any):
            pass

        def read_thread(self, **_: Any):
            return SimpleNamespace(
                messages=[
                    SimpleNamespace(
                        ts="1710000001.000100",
                        user_id="U_APPROVER",
                        text="approved, fix it",
                    ),
                ]
            )

    exit_code = run_sync_approvals_command(
        config=MonicaConfig(slack=SlackConfig(approver_user_ids=("U_APPROVER",))),
        state=state,
        run_id=run.id,
        json_output=True,
        client_factory=FakeClient,
    )

    payload = json.loads(capsys.readouterr().out)
    updated = state.get_run(run.id)
    assert updated is not None
    assert exit_code == 0
    assert launched == []
    assert updated.status == "awaiting_fix_approval"
    assert updated.approved_by_user_id == ""
    assert payload["ok"] is True
    assert payload["results"][0]["action"] == "skipped"
    assert payload["results"][0]["reason"] == "no configured approver approval found in Slack thread"


def test_simulate_requires_enabled_config(tmp_path, capsys):
    state = MonicaState.open(tmp_path / "state.sqlite")

    exit_code = run_simulate_command(
        config=MonicaConfig(enabled=False),
        state=state,
        text="checkout crashes on Android",
        channel_id="LOCAL",
        user_id="local-operator",
    )

    assert exit_code == 1
    assert "Monica is disabled" in capsys.readouterr().out
    assert state.list_runs() == []


def test_simulate_json_reports_disabled_config_without_creating_run(tmp_path, capsys):
    state = MonicaState.open(tmp_path / "state.sqlite")

    exit_code = run_simulate_command(
        config=MonicaConfig(enabled=False),
        state=state,
        text="checkout crashes on Android",
        channel_id="LOCAL",
        user_id="local-operator",
        json_output=True,
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert state.list_runs() == []
    assert payload["created"] is False
    assert payload["rollout_mode"] == "dry_run"
    assert payload["error"]["code"] == "config_disabled"
    assert "mobile_bug_agent.enabled" in payload["error"]["message"]


def test_simulate_creates_local_slack_shaped_run_and_invokes_loop(tmp_path, capsys):
    state = MonicaState.open(tmp_path / "state.sqlite")
    launched: list[str] = []

    def fake_loop(run_id: str, *, state: MonicaState) -> None:
        launched.append(run_id)
        state.update_run(run_id, status="done", linear_identifier="DRY-RUN")

    exit_code = run_simulate_command(
        config=MonicaConfig(enabled=True, rollout_mode="dry_run"),
        state=state,
        text="checkout crashes on Android",
        channel_id="LOCAL",
        user_id="local-operator",
        thread_ts="sim-thread",
        loop_runner=fake_loop,
    )

    runs = state.list_runs()
    out = capsys.readouterr().out
    assert exit_code == 0
    assert len(runs) == 1
    assert launched == [runs[0].id]
    assert runs[0].platform == "slack"
    assert runs[0].channel_id == "LOCAL"
    assert runs[0].thread_ts == "sim-thread"
    assert runs[0].raw_event is not None
    assert runs[0].raw_event["monica_simulated"] is True
    assert runs[0].raw_event["type"] == "app_mention"
    assert runs[0].raw_event["permalink"] == "local://monica/simulated/sim-thread"
    assert "Simulated Monica run" in out
    assert "DRY-RUN" in out


def test_simulate_refuses_during_runtime_sync_lease(tmp_path, capsys):
    state = MonicaState.open(tmp_path / "state.sqlite")
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
    launched: list[str] = []

    exit_code = run_simulate_command(
        config=MonicaConfig(enabled=True, rollout_mode="dry_run"),
        state=state,
        text="checkout crashes on Android",
        channel_id="LOCAL",
        user_id="local-operator",
        thread_ts="sim-thread",
        loop_runner=lambda run_id, *, state: launched.append(run_id),
    )

    assert exit_code == 1
    assert state.list_runs() == []
    assert launched == []
    assert "Hermes is updating" in capsys.readouterr().out


def test_simulate_json_outputs_created_run(tmp_path, capsys):
    state = MonicaState.open(tmp_path / "state.sqlite")
    launched: list[str] = []

    def fake_loop(run_id: str, *, state: MonicaState) -> None:
        launched.append(run_id)
        state.update_run(
            run_id,
            status="done",
            linear_identifier="DRY-RUN",
            linear_url="https://linear.app/acme/issue/DRY-RUN",
        )

    exit_code = run_simulate_command(
        config=MonicaConfig(enabled=True, rollout_mode="dry_run"),
        state=state,
        text="checkout crashes on Android",
        channel_id="LOCAL",
        user_id="local-operator",
        thread_ts="sim-thread",
        loop_runner=fake_loop,
        json_output=True,
    )

    payload = json.loads(capsys.readouterr().out)
    runs = state.list_runs()
    assert exit_code == 0
    assert len(runs) == 1
    assert launched == [runs[0].id]
    assert payload["run"]["id"] == runs[0].id
    assert payload["run"]["status"] == "done"
    assert payload["run"]["slack"]["channel_id"] == "LOCAL"
    assert payload["run"]["linear"]["identifier"] == "DRY-RUN"
    assert payload["created"] is True
    assert payload["allow_side_effects"] is False
    assert payload["rollout_mode"] == "dry_run"


def test_simulate_default_thread_ids_do_not_collide_when_clock_repeats(
    tmp_path,
    capsys,
    monkeypatch,
):
    state = MonicaState.open(tmp_path / "state.sqlite")
    launched: list[str] = []
    monkeypatch.setattr(cli.time, "time", lambda: 1710000000.0)

    first = run_simulate_command(
        config=MonicaConfig(enabled=True, rollout_mode="dry_run"),
        state=state,
        text="checkout crashes on Android",
        channel_id="LOCAL",
        user_id="local-operator",
        loop_runner=lambda run_id, *, state: launched.append(run_id),
    )
    second = run_simulate_command(
        config=MonicaConfig(enabled=True, rollout_mode="dry_run"),
        state=state,
        text="checkout still crashes on Android",
        channel_id="LOCAL",
        user_id="local-operator",
        loop_runner=lambda run_id, *, state: launched.append(run_id),
    )

    runs = state.list_runs()
    assert first == 0
    assert second == 0
    assert len(runs) == 2
    assert len({run.thread_ts for run in runs}) == 2
    assert set(launched) == {run.id for run in runs}
    assert all(re.fullmatch(r"\d+\.\d{6}", run.thread_ts) for run in runs)
    assert "already exists" not in capsys.readouterr().out


def test_simulate_refuses_side_effect_rollout_without_explicit_flag(tmp_path, capsys):
    state = MonicaState.open(tmp_path / "state.sqlite")
    launched: list[str] = []

    exit_code = run_simulate_command(
        config=MonicaConfig(enabled=True, rollout_mode="linear_only", dry_run=False),
        state=state,
        text="checkout crashes on Android",
        channel_id="LOCAL",
        user_id="local-operator",
        thread_ts="sim-thread",
        loop_runner=lambda run_id, *, state: launched.append(run_id),
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert state.list_runs() == []
    assert launched == []
    assert "--allow-side-effects" in out


def test_simulate_json_reports_empty_text_without_creating_run(tmp_path, capsys):
    state = MonicaState.open(tmp_path / "state.sqlite")
    launched: list[str] = []

    exit_code = run_simulate_command(
        config=MonicaConfig(enabled=True, rollout_mode="dry_run"),
        state=state,
        text="   ",
        channel_id="LOCAL",
        user_id="local-operator",
        loop_runner=lambda run_id, *, state: launched.append(run_id),
        json_output=True,
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert state.list_runs() == []
    assert launched == []
    assert payload["created"] is False
    assert payload["rollout_mode"] == "dry_run"
    assert payload["allow_side_effects"] is False
    assert payload["error"]["code"] == "empty_text"
    assert "text is required" in payload["error"]["message"]


def test_simulate_json_reports_side_effect_rollout_without_explicit_flag(
    tmp_path,
    capsys,
):
    state = MonicaState.open(tmp_path / "state.sqlite")
    launched: list[str] = []

    exit_code = run_simulate_command(
        config=MonicaConfig(enabled=True, rollout_mode="linear_only", dry_run=False),
        state=state,
        text="checkout crashes on Android",
        channel_id="LOCAL",
        user_id="local-operator",
        thread_ts="sim-thread",
        loop_runner=lambda run_id, *, state: launched.append(run_id),
        json_output=True,
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert state.list_runs() == []
    assert launched == []
    assert payload["created"] is False
    assert payload["rollout_mode"] == "linear_only"
    assert payload["allow_side_effects"] is False
    assert payload["error"]["code"] == "side_effects_not_allowed"
    assert "--allow-side-effects" in payload["error"]["message"]


def test_simulate_allows_side_effect_rollout_with_explicit_flag(tmp_path, capsys):
    state = MonicaState.open(tmp_path / "state.sqlite")
    launched: list[str] = []

    exit_code = run_simulate_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="linear_only",
            dry_run=False,
            slack=SlackConfig(
                allowed_channels=("C_SIM",),
                bot_user_ids=("U_MONICA",),
            ),
        ),
        state=state,
        text="checkout crashes on Android",
        channel_id="C_SIM",
        user_id="local-operator",
        thread_ts="sim-thread",
        allow_side_effects=True,
        loop_runner=lambda run_id, *, state: launched.append(run_id),
        readiness_checker=lambda: 0,
    )

    runs = state.list_runs()
    out = capsys.readouterr().out
    assert exit_code == 0
    assert len(runs) == 1
    assert launched == [runs[0].id]
    assert "Simulated Monica run" in out


def test_simulate_refuses_side_effect_rollout_when_readiness_check_fails(tmp_path, capsys):
    state = MonicaState.open(tmp_path / "state.sqlite")
    launched: list[str] = []

    exit_code = run_simulate_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="linear_only",
            dry_run=False,
            slack=SlackConfig(
                allowed_channels=("C_SIM",),
                bot_user_ids=("U_MONICA",),
            ),
        ),
        state=state,
        text="checkout crashes on Android",
        channel_id="C_SIM",
        user_id="local-operator",
        thread_ts="sim-thread",
        allow_side_effects=True,
        loop_runner=lambda run_id, *, state: launched.append(run_id),
        readiness_checker=lambda: 1,
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert state.list_runs() == []
    assert launched == []
    assert "Monica readiness check failed" in out


def test_mobile_command_simulate_refuses_side_effects_when_plugin_is_not_enabled(
    tmp_path, capsys, monkeypatch
):
    runtime = tmp_path / "monica-runtime"
    config = MonicaConfig(
        enabled=True,
        rollout_mode="approved_pr",
        runtime=RuntimeConfig(home_subdir=str(runtime)),
        slack=SlackConfig(
            allowed_channels=("C123MOBILE",),
            bot_user_ids=("U123MONICA",),
            approver_user_ids=("U_APPROVER",),
        ),
    )
    ready_report = SimpleNamespace(
        ready=True,
        rollout_mode="approved_pr",
        runtime_root_value=str(runtime),
        warnings=(),
        failures=(),
    )
    hermes_config = {
        "plugins": {"enabled": ["other-plugin"], "disabled": []},
        "mobile_bug_agent": {"enabled": True, "rollout_mode": "approved_pr"},
    }
    launched: list[str] = []
    monkeypatch.setattr(cli, "load_monica_config", lambda: config)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: copy.deepcopy(hermes_config))
    monkeypatch.setattr(cli, "check_monica_readiness", lambda **_kwargs: ready_report)
    monkeypatch.setattr(
        cli,
        "_read_gateway_status",
        lambda: {"gateway_state": "running", "pid": 53960, "active_agents": 0},
    )
    monkeypatch.setattr(cli, "_current_hermes_commit", lambda: "abc12345")
    monkeypatch.setattr(cli, "_run_loop", lambda run_id, *, state: launched.append(run_id))

    exit_code = cli.mobile_bug_agent_command(
        SimpleNamespace(
            mobile_bug_agent_action="simulate",
            text=["@monica", "marketplace", "PDP", "copy", "bug"],
            channel_id="C123MOBILE",
            user_id="U_REPORTER",
            thread_ts="",
            allow_side_effects=True,
            json=True,
        )
    )

    state = MonicaState.open(runtime / "state.sqlite")
    out = capsys.readouterr().out
    payload = json.loads(out.strip().splitlines()[-1])
    assert exit_code == 1
    assert state.list_runs() == []
    assert launched == []
    assert payload["error"]["code"] == "readiness_failed"


def test_simulate_json_reports_readiness_failure_without_creating_run(tmp_path, capsys):
    state = MonicaState.open(tmp_path / "state.sqlite")
    launched: list[str] = []

    exit_code = run_simulate_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="linear_only",
            dry_run=False,
            slack=SlackConfig(
                allowed_channels=("C_SIM",),
                bot_user_ids=("U_MONICA",),
            ),
        ),
        state=state,
        text="checkout crashes on Android",
        channel_id="C_SIM",
        user_id="local-operator",
        thread_ts="sim-thread",
        allow_side_effects=True,
        json_output=True,
        loop_runner=lambda run_id, *, state: launched.append(run_id),
        readiness_checker=lambda: 1,
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert state.list_runs() == []
    assert launched == []
    assert payload["created"] is False
    assert payload["rollout_mode"] == "linear_only"
    assert payload["allow_side_effects"] is True
    assert payload["error"]["code"] == "readiness_failed"


def test_simulate_json_reports_readiness_exception_without_creating_run(tmp_path, capsys):
    state = MonicaState.open(tmp_path / "state.sqlite")
    launched: list[str] = []

    def fail_readiness() -> int:
        raise RuntimeError("doctor exploded")

    exit_code = run_simulate_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="linear_only",
            dry_run=False,
            slack=SlackConfig(
                allowed_channels=("C_SIM",),
                bot_user_ids=("U_MONICA",),
            ),
        ),
        state=state,
        text="checkout crashes on Android",
        channel_id="C_SIM",
        user_id="local-operator",
        thread_ts="sim-thread",
        allow_side_effects=True,
        json_output=True,
        loop_runner=lambda run_id, *, state: launched.append(run_id),
        readiness_checker=fail_readiness,
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert state.list_runs() == []
    assert launched == []
    assert payload["created"] is False
    assert payload["rollout_mode"] == "linear_only"
    assert payload["allow_side_effects"] is True
    assert payload["error"]["code"] == "readiness_exception"
    assert "doctor exploded" in payload["error"]["message"]


def test_simulate_refuses_side_effect_rollout_without_bot_user_ids(tmp_path, capsys):
    state = MonicaState.open(tmp_path / "state.sqlite")
    launched: list[str] = []

    exit_code = run_simulate_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="linear_only",
            dry_run=False,
            slack=SlackConfig(allowed_channels=("C_SIM",)),
        ),
        state=state,
        text="checkout crashes on Android",
        channel_id="C_SIM",
        user_id="local-operator",
        thread_ts="sim-thread",
        allow_side_effects=True,
        loop_runner=lambda run_id, *, state: launched.append(run_id),
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert state.list_runs() == []
    assert launched == []
    assert "mobile_bug_agent.slack.bot_user_ids" in out


def test_simulate_json_reports_missing_bot_user_ids_without_creating_run(tmp_path, capsys):
    state = MonicaState.open(tmp_path / "state.sqlite")
    launched: list[str] = []

    exit_code = run_simulate_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="linear_only",
            dry_run=False,
            slack=SlackConfig(allowed_channels=("C_SIM",)),
        ),
        state=state,
        text="checkout crashes on Android",
        channel_id="C_SIM",
        user_id="local-operator",
        thread_ts="sim-thread",
        allow_side_effects=True,
        loop_runner=lambda run_id, *, state: launched.append(run_id),
        json_output=True,
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert state.list_runs() == []
    assert launched == []
    assert payload["created"] is False
    assert payload["rollout_mode"] == "linear_only"
    assert payload["allow_side_effects"] is True
    assert payload["error"]["code"] == "bot_user_ids_empty"
    assert "mobile_bug_agent.slack.bot_user_ids" in payload["error"]["message"]


def test_simulate_refuses_approved_pr_side_effects_without_configured_approvers(
    tmp_path,
    capsys,
):
    state = MonicaState.open(tmp_path / "state.sqlite")
    launched: list[str] = []

    exit_code = run_simulate_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            dry_run=False,
            slack=SlackConfig(allowed_channels=("C_SIM",), bot_user_ids=("U_MONICA",)),
        ),
        state=state,
        text="checkout crashes on Android",
        channel_id="C_SIM",
        user_id="local-operator",
        thread_ts="sim-thread",
        allow_side_effects=True,
        loop_runner=lambda run_id, *, state: launched.append(run_id),
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert state.list_runs() == []
    assert launched == []
    assert "mobile_bug_agent.slack.approver_user_ids" in out


def test_simulate_json_reports_missing_approvers_without_creating_run(tmp_path, capsys):
    state = MonicaState.open(tmp_path / "state.sqlite")
    launched: list[str] = []

    exit_code = run_simulate_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            dry_run=False,
            slack=SlackConfig(allowed_channels=("C_SIM",), bot_user_ids=("U_MONICA",)),
        ),
        state=state,
        text="checkout crashes on Android",
        channel_id="C_SIM",
        user_id="local-operator",
        thread_ts="sim-thread",
        allow_side_effects=True,
        loop_runner=lambda run_id, *, state: launched.append(run_id),
        json_output=True,
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert state.list_runs() == []
    assert launched == []
    assert payload["created"] is False
    assert payload["rollout_mode"] == "approved_pr"
    assert payload["allow_side_effects"] is True
    assert payload["error"]["code"] == "approver_user_ids_empty"
    assert "mobile_bug_agent.slack.approver_user_ids" in payload["error"]["message"]


def test_simulate_refuses_side_effect_rollout_when_allowed_channels_are_empty(
    tmp_path,
    capsys,
):
    state = MonicaState.open(tmp_path / "state.sqlite")
    launched: list[str] = []

    exit_code = run_simulate_command(
        config=MonicaConfig(enabled=True, rollout_mode="linear_only", dry_run=False),
        state=state,
        text="checkout crashes on Android",
        channel_id="C_SIM",
        user_id="local-operator",
        thread_ts="sim-thread",
        allow_side_effects=True,
        loop_runner=lambda run_id, *, state: launched.append(run_id),
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert state.list_runs() == []
    assert launched == []
    assert "mobile_bug_agent.slack.allowed_channels" in out


def test_simulate_json_reports_missing_allowed_channels_without_creating_run(
    tmp_path,
    capsys,
):
    state = MonicaState.open(tmp_path / "state.sqlite")
    launched: list[str] = []

    exit_code = run_simulate_command(
        config=MonicaConfig(enabled=True, rollout_mode="linear_only", dry_run=False),
        state=state,
        text="checkout crashes on Android",
        channel_id="C_SIM",
        user_id="local-operator",
        thread_ts="sim-thread",
        allow_side_effects=True,
        loop_runner=lambda run_id, *, state: launched.append(run_id),
        json_output=True,
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert state.list_runs() == []
    assert launched == []
    assert payload["created"] is False
    assert payload["rollout_mode"] == "linear_only"
    assert payload["allow_side_effects"] is True
    assert payload["error"]["code"] == "allowed_channels_empty"
    assert "mobile_bug_agent.slack.allowed_channels" in payload["error"]["message"]


def test_simulate_refuses_side_effect_rollout_outside_allowed_channel(tmp_path, capsys):
    state = MonicaState.open(tmp_path / "state.sqlite")
    launched: list[str] = []

    exit_code = run_simulate_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="linear_only",
            dry_run=False,
            slack=SlackConfig(allowed_channels=("C_MOBILE",), bot_user_ids=("U_MONICA",)),
        ),
        state=state,
        text="checkout crashes on Android",
        channel_id="C_OTHER",
        user_id="local-operator",
        thread_ts="sim-thread",
        allow_side_effects=True,
        loop_runner=lambda run_id, *, state: launched.append(run_id),
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert state.list_runs() == []
    assert launched == []
    assert "C_OTHER is not in mobile_bug_agent.slack.allowed_channels" in out


def test_simulate_json_reports_disallowed_channel_without_creating_run(tmp_path, capsys):
    state = MonicaState.open(tmp_path / "state.sqlite")
    launched: list[str] = []

    exit_code = run_simulate_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="linear_only",
            dry_run=False,
            slack=SlackConfig(allowed_channels=("C_MOBILE",), bot_user_ids=("U_MONICA",)),
        ),
        state=state,
        text="checkout crashes on Android",
        channel_id="C_OTHER",
        user_id="local-operator",
        thread_ts="sim-thread",
        allow_side_effects=True,
        loop_runner=lambda run_id, *, state: launched.append(run_id),
        json_output=True,
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert state.list_runs() == []
    assert launched == []
    assert payload["created"] is False
    assert payload["rollout_mode"] == "linear_only"
    assert payload["allow_side_effects"] is True
    assert payload["error"]["code"] == "channel_not_allowed"
    assert "C_OTHER" in payload["error"]["message"]


def test_simulate_refuses_duplicate_thread_without_relaunching(tmp_path, capsys):
    state = MonicaState.open(tmp_path / "state.sqlite")
    existing = state.create_run(
        platform="slack",
        channel_id="LOCAL",
        thread_ts="sim-thread",
        message_ts="sim-message",
        user_id="local-operator",
        request_text="old checkout crash simulation",
        raw_event={"monica_simulated": True},
    )
    state.update_run(existing.id, status="done", linear_identifier="DRY-RUN")
    launched: list[str] = []

    exit_code = run_simulate_command(
        config=MonicaConfig(enabled=True, rollout_mode="dry_run"),
        state=state,
        text="new checkout crash simulation",
        channel_id="LOCAL",
        user_id="local-operator",
        thread_ts="sim-thread",
        loop_runner=lambda run_id, *, state: launched.append(run_id),
    )

    updated = state.get_run(existing.id)
    assert updated is not None
    assert exit_code == 1
    assert updated.status == "done"
    assert updated.request_text == "old checkout crash simulation"
    assert launched == []
    assert "already exists" in capsys.readouterr().out


def test_simulate_json_duplicate_thread_returns_existing_run(tmp_path, capsys):
    state = MonicaState.open(tmp_path / "state.sqlite")
    existing = state.create_run(
        platform="slack",
        channel_id="LOCAL",
        thread_ts="sim-thread",
        message_ts="sim-message",
        user_id="local-operator",
        request_text="old checkout crash simulation",
        raw_event={"monica_simulated": True},
    )
    state.update_run(existing.id, status="done", linear_identifier="DRY-RUN")
    launched: list[str] = []

    exit_code = run_simulate_command(
        config=MonicaConfig(enabled=True, rollout_mode="dry_run"),
        state=state,
        text="new checkout crash simulation",
        channel_id="LOCAL",
        user_id="local-operator",
        thread_ts="sim-thread",
        loop_runner=lambda run_id, *, state: launched.append(run_id),
        json_output=True,
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert launched == []
    assert payload["created"] is False
    assert payload["error"]["code"] == "duplicate_thread"
    assert payload["run"]["id"] == existing.id
    assert payload["run"]["status"] == "done"
    assert payload["run"]["linear"]["identifier"] == "DRY-RUN"


def test_open_state_uses_configured_runtime_root(tmp_path):
    config = config_from_mapping({"runtime": {"home_subdir": str(tmp_path / "monica")}})

    state = _open_state(config)

    assert state.path == tmp_path / "monica" / "state.sqlite"


def test_slack_manifest_outputs_required_monica_app_manifest(capsys):
    exit_code = run_slack_manifest_command(
        config=MonicaConfig(),
        app_name="Monica Staging",
        bot_display_name="monica",
    )

    manifest = json.loads(capsys.readouterr().out)
    scopes = manifest["oauth_config"]["scopes"]["bot"]
    assert exit_code == 0
    assert manifest["display_information"]["name"] == "Monica Staging"
    assert manifest["features"]["app_home"] == {
        "home_tab_enabled": False,
        "messages_tab_enabled": True,
        "messages_tab_read_only_enabled": False,
    }
    assert manifest["features"]["bot_user"]["display_name"] == "monica"
    assert manifest["features"]["bot_user"]["always_online"] is True
    assert manifest["settings"]["socket_mode_enabled"] is True
    assert manifest["settings"]["event_subscriptions"]["bot_events"] == [
        "app_mention",
        "message.im",
    ]
    assert scopes == [
        "app_mentions:read",
        "channels:history",
        "groups:history",
        "im:history",
        "im:read",
        "chat:write",
        "files:read",
        "files:write",
    ]


def test_slack_manifest_omits_files_scope_when_attachment_downloads_disabled(capsys):
    exit_code = run_slack_manifest_command(
        config=MonicaConfig(slack=SlackConfig(download_attachments=False)),
        app_name="",
        bot_display_name="",
    )

    manifest = json.loads(capsys.readouterr().out)
    scopes = manifest["oauth_config"]["scopes"]["bot"]
    assert exit_code == 0
    assert manifest["display_information"]["name"] == "Monica"
    assert manifest["features"]["bot_user"]["display_name"] == "monica"
    assert "files:read" not in scopes
    assert "files:write" in scopes


def test_linear_metadata_json_outputs_teams_projects_and_labels(capsys):
    class FakeClient:
        def __init__(self, *, api_key: str) -> None:
            self.api_key = api_key

        def list_workspace_metadata(self):
            return SimpleNamespace(
                teams=(SimpleNamespace(id="team-mobile", key="MOB", name="Mobile"),),
                projects=(SimpleNamespace(id="project-app", name="Mobile App", state="started"),),
                labels=(
                    SimpleNamespace(id="label-bug", name="Bug", color="#e5484d"),
                    SimpleNamespace(id="label-mobile", name="Mobile", color="#3b82f6"),
                ),
            )

    exit_code = run_linear_metadata_command(
        api_key="lin_api_key",
        json_output=True,
        client_factory=FakeClient,
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["teams"][0] == {"id": "team-mobile", "key": "MOB", "name": "Mobile"}
    assert payload["projects"][0] == {
        "id": "project-app",
        "name": "Mobile App",
        "state": "started",
    }
    assert payload["labels"][1] == {
        "id": "label-mobile",
        "name": "Mobile",
        "color": "#3b82f6",
    }


def test_linear_metadata_text_outputs_configure_hint(capsys):
    class FakeClient:
        def __init__(self, *, api_key: str) -> None:
            self.api_key = api_key

        def list_workspace_metadata(self):
            return SimpleNamespace(
                teams=(SimpleNamespace(id="team-mobile", key="MOB", name="Mobile"),),
                projects=(),
                labels=(),
            )

    exit_code = run_linear_metadata_command(
        api_key="lin_api_key",
        json_output=False,
        client_factory=FakeClient,
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Teams:" in out
    assert "team-mobile" in out
    assert "configure-linear-only" in out


def test_linear_metadata_refuses_missing_api_key_without_contacting_linear(capsys):
    contacted = False

    class FakeClient:
        def __init__(self, *, api_key: str) -> None:
            nonlocal contacted
            contacted = True

    exit_code = run_linear_metadata_command(
        api_key="",
        json_output=True,
        client_factory=FakeClient,
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert contacted is False
    assert payload["ok"] is False
    assert payload["error"]["code"] == "linear_api_key_missing"


def test_slack_metadata_json_outputs_bot_user_and_channels(capsys):
    class FakeClient:
        def __init__(self, *, token: str) -> None:
            self.token = token

        def list_workspace_metadata(self):
            return SimpleNamespace(
                auth=SimpleNamespace(
                    bot_user_id="U_MONICA",
                    bot_id="B_MONICA",
                    team_id="T123",
                    team_name="Acme",
                    team_url="https://acme.slack.com/",
                ),
                channels=(
                    SimpleNamespace(
                        id="C_MOBILE",
                        name="mobile-bugs",
                        is_private=False,
                        is_member=True,
                        is_archived=False,
                    ),
                    SimpleNamespace(
                        id="G_TRIAGE",
                        name="app-triage",
                        is_private=True,
                        is_member=False,
                        is_archived=False,
                    ),
                ),
            )

    exit_code = run_slack_metadata_command(
        token="xoxb-token",
        json_output=True,
        client_factory=FakeClient,
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["auth"]["bot_user_id"] == "U_MONICA"
    assert payload["auth"]["bot_id"] == "B_MONICA"
    assert payload["auth"]["team_id"] == "T123"
    assert payload["channels"][0] == {
        "id": "C_MOBILE",
        "name": "mobile-bugs",
        "is_private": False,
        "is_member": True,
        "is_archived": False,
    }


def test_slack_metadata_text_outputs_configure_hint(capsys):
    class FakeClient:
        def __init__(self, *, token: str) -> None:
            self.token = token

        def list_workspace_metadata(self):
            return SimpleNamespace(
                auth=SimpleNamespace(
                    bot_user_id="U_MONICA",
                    bot_id="B_MONICA",
                    team_id="T123",
                    team_name="Acme",
                    team_url="https://acme.slack.com/",
                ),
                channels=(
                    SimpleNamespace(
                        id="C_MOBILE",
                        name="mobile-bugs",
                        is_private=False,
                        is_member=True,
                        is_archived=False,
                    ),
                ),
            )

    exit_code = run_slack_metadata_command(
        token="xoxb-token",
        json_output=False,
        client_factory=FakeClient,
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Bot user ID: U_MONICA" in out
    assert "C_MOBILE" in out
    assert "configure-linear-only" in out


def test_slack_metadata_refuses_missing_token_without_contacting_slack(capsys):
    contacted = False

    class FakeClient:
        def __init__(self, *, token: str) -> None:
            nonlocal contacted
            contacted = True

    exit_code = run_slack_metadata_command(
        token="",
        json_output=True,
        client_factory=FakeClient,
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert contacted is False
    assert payload["ok"] is False
    assert payload["error"]["code"] == "slack_bot_token_missing"


def test_slack_metadata_uses_monica_token_instead_of_shared_slack_token(capsys, monkeypatch):
    seen: list[str] = []

    class FakeClient:
        def __init__(self, *, token: str) -> None:
            seen.append(token)

        def list_workspace_metadata(self):
            return SimpleNamespace(auth=SimpleNamespace(), channels=())

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-chandler")
    monkeypatch.setenv("MONICA_SLACK_BOT_TOKEN", "xoxb-monica")

    exit_code = run_slack_metadata_command(
        json_output=True,
        client_factory=FakeClient,
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["ok"] is True
    assert seen == ["xoxb-monica"]


def test_setup_plan_json_lists_ordered_linear_only_rollout_steps(capsys):
    exit_code = run_setup_plan_command(
        config=MonicaConfig(enabled=False),
        target_rollout_mode="linear_only",
        json_output=True,
        environ={},
        which=lambda command: f"/usr/bin/{command}",
        module_available=lambda name: True,
    )

    payload = json.loads(capsys.readouterr().out)
    step_ids = [step["id"] for step in payload["steps"]]
    assert exit_code == 1
    assert payload["ready"] is False
    assert payload["rollout_mode"] == "linear_only"
    assert payload["failures"][0]["code"] == "config_enabled"
    assert step_ids == [
        "create_slack_app",
        "set_slack_bot_token",
        "discover_slack_ids",
        "set_linear_api_key",
        "discover_linear_ids",
        "configure_linear_only",
        "rerun_doctor",
    ]
    assert payload["steps"][0]["command"] == "hermes mobile-bug-agent slack-manifest"
    assert payload["steps"][2]["command"] == "hermes mobile-bug-agent slack-metadata --json"
    assert payload["steps"][4]["command"] == "hermes mobile-bug-agent linear-metadata --json"
    assert "configure-linear-only" in payload["steps"][5]["command"]


def test_setup_plan_json_lists_ordered_dry_run_bootstrap_steps(capsys):
    exit_code = run_setup_plan_command(
        config=MonicaConfig(enabled=False),
        target_rollout_mode="dry_run",
        json_output=True,
        environ={},
        which=lambda command: f"/usr/bin/{command}",
        module_available=lambda name: True,
    )

    payload = json.loads(capsys.readouterr().out)
    step_ids = [step["id"] for step in payload["steps"]]
    assert exit_code == 1
    assert payload["ready"] is False
    assert payload["rollout_mode"] == "dry_run"
    assert payload["failures"][0]["code"] == "config_enabled"
    assert step_ids == [
        "configure_dry_run",
        "create_slack_app",
        "set_slack_bot_token",
        "rerun_doctor",
    ]
    assert payload["steps"][0]["command"] == "hermes mobile-bug-agent configure-dry-run"


def test_setup_plan_text_lists_next_commands(capsys):
    exit_code = run_setup_plan_command(
        config=MonicaConfig(enabled=False),
        target_rollout_mode="linear_only",
        json_output=False,
        environ={},
        which=lambda command: f"/usr/bin/{command}",
        module_available=lambda name: True,
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "Monica setup plan: linear_only" in out
    assert "hermes mobile-bug-agent slack-manifest" in out
    assert "hermes mobile-bug-agent configure-linear-only" in out


def test_setup_plan_json_has_no_steps_when_rollout_is_ready(capsys):
    exit_code = run_setup_plan_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="linear_only",
            dry_run=False,
            slack=SlackConfig(
                allowed_channels=("C123MOBILE",),
                bot_user_ids=("U123MONICA",),
            ),
            linear=LinearConfig(team_id="team-mobile"),
        ),
        target_rollout_mode="linear_only",
        json_output=True,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "LINEAR_API_KEY": "lin_api_key",
        },
        which=lambda command: f"/usr/bin/{command}",
        module_available=lambda name: True,
        gateway_status={},
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["ready"] is True
    assert payload["steps"] == []


def test_setup_plan_json_lists_gateway_step_when_gateway_is_stopped(capsys):
    exit_code = run_setup_plan_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="linear_only",
            dry_run=False,
            slack=SlackConfig(
                allowed_channels=("C123MOBILE",),
                bot_user_ids=("U123MONICA",),
            ),
            linear=LinearConfig(team_id="team-mobile"),
        ),
        target_rollout_mode="linear_only",
        json_output=True,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin_api_key",
        },
        which=lambda command: f"/usr/bin/{command}",
        module_available=lambda name: True,
        gateway_status={
            "gateway_state": "stopped",
            "pid": 53960,
            "active_agents": 0,
            "updated_at": "2026-06-12T18:12:10Z",
        },
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["ready"] is True
    assert payload["warnings"] == [
        {
            "code": "gateway_not_running",
            "message": (
                "Monica gateway is stopped; live Slack tag/DM intake and approvals "
                "will not be received until the gateway is running."
            ),
        }
    ]
    assert payload["health"]["gateway"]["state"] == "stopped"
    assert payload["steps"] == [
        {
            "id": "start_gateway",
            "title": "Start Monica's Slack gateway",
            "command": "hermes gateway start",
            "why": (
                "The gateway must be running for live Slack tag/DM intake and approvals; "
                "doctor will keep warning until it is running."
            ),
        },
        {
            "id": "rerun_doctor",
            "title": "Re-run the rollout readiness check",
            "command": "hermes mobile-bug-agent doctor --rollout-mode linear_only --json",
            "why": "Only continue to a live Slack test once Monica reports ready.",
        },
    ]


def test_setup_plan_json_lists_supervised_gateway_install_for_approved_pr_when_gateway_is_stopped(
    capsys,
):
    exit_code = run_setup_plan_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            slack=SlackConfig(
                allowed_channels=("C123MOBILE",),
                bot_user_ids=("U123MONICA",),
                approver_user_ids=("U123APPROVER",),
            ),
            linear=LinearConfig(team_id="team-mobile"),
            repo=RepoConfig(url="git@github.com:acme/mobile.git"),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                commands=("npm run monica:proof"),
                setup_commands=("npm run monica:seed-auth",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
                platform_order=("ios", "android"),
            ),
        ),
        target_rollout_mode="approved_pr",
        json_output=True,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin_api_key",
            "GITHUB_TOKEN": "gh_token",
            "MONICA_TEST_LOGIN_TOKEN": "token",
            "SLACK_BOT_SCOPES": (
                "app_mentions:read,chat:write,channels:history,files:read,files:write"
            ),
        },
        which=lambda command: f"/usr/bin/{command}",
        module_available=lambda name: True,
        gateway_status={
            "gateway_state": "stopped",
            "pid": 53960,
            "active_agents": 0,
            "updated_at": "2026-06-12T18:12:10Z",
        },
    )

    payload = json.loads(capsys.readouterr().out)
    step_ids = [step["id"] for step in payload["steps"]]
    assert exit_code == 1
    assert payload["ready"] is False
    assert step_ids == ["install_gateway_service", "rerun_doctor"]
    assert payload["steps"][0] == {
        "id": "install_gateway_service",
        "title": "Install Monica's supervised Slack gateway",
        "command": "hermes gateway install",
        "why": (
            "Approved PR work depends on live Slack tag/DM intake and approvals. "
            "Install the profile-aware gateway service so Monica stays running "
            "under the platform supervisor."
        ),
    }


def test_setup_plan_json_lists_supervised_gateway_install_for_manual_approved_pr_gateway(
    capsys,
    monkeypatch,
):
    monkeypatch.setattr(
        cli,
        "_read_gateway_status",
        lambda: {
            "gateway_state": "running",
            "pid": 53960,
            "start_time": 100.0,
            "active_agents": 0,
            "updated_at": "100.0",
        },
    )
    monkeypatch.setattr(
        cli,
        "_read_gateway_service_status",
        lambda: {
            "available": True,
            "manager": "manual process",
            "service_installed": False,
            "service_running": False,
            "gateway_pids": [53960],
            "service_scope": None,
            "supervised": False,
        },
        raising=False,
    )

    exit_code = run_setup_plan_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            slack=SlackConfig(
                allowed_channels=("C123MOBILE",),
                bot_user_ids=("U123MONICA",),
                approver_user_ids=("U123APPROVER",),
            ),
            linear=LinearConfig(team_id="team-mobile"),
            repo=RepoConfig(url="git@github.com:acme/mobile.git"),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                commands=("npm run monica:proof"),
                setup_commands=("npm run monica:seed-auth",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
                platform_order=("ios", "android"),
            ),
        ),
        target_rollout_mode="approved_pr",
        json_output=True,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin_api_key",
            "GITHUB_TOKEN": "gh_token",
            "MONICA_TEST_LOGIN_TOKEN": "token",
            "SLACK_BOT_SCOPES": (
                "app_mentions:read,chat:write,channels:history,files:read,files:write"
            ),
        },
        which=lambda command: f"/usr/bin/{command}",
        module_available=lambda name: True,
        now=160.0,
    )

    payload = json.loads(capsys.readouterr().out)
    step_ids = [step["id"] for step in payload["steps"]]
    assert exit_code == 1
    assert payload["ready"] is False
    assert step_ids == ["install_gateway_service", "rerun_doctor"]
    assert payload["steps"][0]["command"] == "hermes gateway install"


def test_setup_plan_json_lists_gateway_restart_step_when_gateway_status_is_stale(capsys):
    exit_code = run_setup_plan_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            slack=SlackConfig(
                bot_user_ids=("U123MONICA",),
                allowed_channels=("C123MOBILE",),
                approver_user_ids=("U123APPROVER",),
            ),
            linear=LinearConfig(team_id="team-mobile"),
            repo=RepoConfig(url="git@github.com:acme/mobile.git"),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                commands=("npm run monica:proof",),
                setup_commands=("npm run monica:seed-auth",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
                platform_order=("ios", "android"),
            ),
        ),
        target_rollout_mode="approved_pr",
        json_output=True,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin_api_key",
            "GITHUB_TOKEN": "gh_token",
            "MONICA_TEST_LOGIN_TOKEN": "token",
            "SLACK_BOT_SCOPES": "app_mentions:read,chat:write,channels:history,files:read,files:write",
        },
        which=lambda command: f"/usr/bin/{command}",
        module_available=lambda name: True,
        gateway_status={
            "gateway_state": "running",
            "pid": 53960,
            "active_agents": 0,
            "updated_at": "1970-01-01T00:00:00Z",
        },
        now=2000.0,
    )

    payload = json.loads(capsys.readouterr().out)
    step_ids = [step["id"] for step in payload["steps"]]
    assert exit_code == 1
    assert payload["ready"] is False
    assert {
        "code": "gateway_status_stale",
        "message": (
            "Monica gateway status is stale; live Slack tag/DM intake and approvals "
            "may not be received until the gateway heartbeat is fresh."
        ),
    } in payload["failures"]
    assert payload["health"]["gateway"]["stale"] is True
    assert step_ids == ["restart_gateway", "rerun_doctor"]
    assert payload["steps"][0] == {
        "id": "restart_gateway",
        "title": "Restart Monica's Slack gateway",
        "command": "hermes gateway restart",
        "why": (
            "The gateway heartbeat is stale, so restart it before relying on "
            "live Slack tag/DM intake or approvals."
        ),
    }


def test_setup_plan_json_lists_gateway_restart_step_when_gateway_runtime_metadata_is_missing(capsys):
    exit_code = run_setup_plan_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            slack=SlackConfig(
                bot_user_ids=("U123MONICA",),
                allowed_channels=("C123MOBILE",),
                approver_user_ids=("U123APPROVER",),
            ),
            linear=LinearConfig(team_id="team-mobile"),
            repo=RepoConfig(url="git@github.com:acme/mobile.git"),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                commands=("npm run monica:proof",),
                setup_commands=("npm run monica:seed-auth",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
                platform_order=("ios", "android"),
            ),
        ),
        target_rollout_mode="approved_pr",
        json_output=True,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin_api_key",
            "GITHUB_TOKEN": "gh_token",
            "MONICA_TEST_LOGIN_TOKEN": "token",
            "SLACK_BOT_SCOPES": "app_mentions:read,chat:write,channels:history,files:read,files:write",
        },
        which=lambda command: f"/usr/bin/{command}",
        module_available=lambda name: True,
        gateway_status={
            "gateway_state": "running",
            "pid": 53960,
            "active_agents": 0,
            "updated_at": "100.0",
        },
        now=160.0,
    )

    payload = json.loads(capsys.readouterr().out)
    step_ids = [step["id"] for step in payload["steps"]]
    assert exit_code == 1
    assert payload["ready"] is False
    assert {
        "code": "gateway_runtime_metadata_missing",
        "message": (
            "Monica gateway runtime metadata is incomplete; restart the gateway "
            "so doctor can verify uptime before approved_pr work runs."
        ),
    } in payload["failures"]
    assert step_ids == ["restart_gateway", "rerun_doctor"]
    assert payload["steps"][0] == {
        "id": "restart_gateway",
        "title": "Restart Monica's Slack gateway",
        "command": "hermes gateway restart",
        "why": (
            "The gateway heartbeat or runtime metadata is incomplete, so restart it "
            "before relying on live Slack tag/DM intake or approvals."
        ),
    }


def test_setup_plan_json_lists_plugin_enable_step_when_plugin_is_not_enabled(
    monkeypatch, capsys
):
    ready_report = SimpleNamespace(
        ready=True,
        rollout_mode="approved_pr",
        runtime_root_value="/tmp/monica",
        warnings=(),
        failures=(),
    )
    hermes_config = {
        "plugins": {
            "enabled": ["other-plugin"],
            "disabled": [],
        },
        "mobile_bug_agent": {
            "enabled": True,
            "rollout_mode": "approved_pr",
        },
    }
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: copy.deepcopy(hermes_config))
    monkeypatch.setattr(cli, "check_monica_readiness", lambda **_kwargs: ready_report)
    monkeypatch.setattr(
        cli,
        "_read_gateway_status",
        lambda: {"gateway_state": "running", "pid": 53960, "active_agents": 0},
    )
    monkeypatch.setattr(cli, "_current_hermes_commit", lambda: "abc12345")

    exit_code = cli.mobile_bug_agent_command(
        SimpleNamespace(mobile_bug_agent_action="setup_plan", rollout_mode="approved_pr", json=True)
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["ready"] is False
    assert {
        "code": "plugin_not_enabled",
        "message": (
            "plugins.enabled must include mobile-bug-agent and plugins.disabled must not; "
            "otherwise Monica's Slack gateway hooks will not load."
        ),
    } in payload["failures"]
    assert payload["health"]["plugin"]["enabled"] is False
    assert payload["steps"][0] == {
        "id": "enable_mobile_bug_agent_plugin",
        "title": "Enable the Monica plugin",
        "command": "hermes plugins enable mobile-bug-agent",
        "why": (
            "The gateway only loads Monica's Slack intake, approval, and runtime-sync hooks "
            "when the bundled plugin is enabled."
        ),
    }
    assert payload["steps"][-1]["id"] == "rerun_doctor"


def test_setup_plan_json_lists_slack_manifest_step_when_files_write_scope_is_missing(capsys):
    exit_code = run_setup_plan_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            dry_run=False,
            slack=SlackConfig(
                allowed_channels=("C123MOBILE",),
                bot_user_ids=("U123MONICA",),
                approver_user_ids=("U123APPROVER",),
            ),
            linear=LinearConfig(team_id="team-mobile"),
            repo=RepoConfig(url="git@github.com:acme/mobile.git"),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                setup_commands=("npm run monica:seed-auth",),
                commands=("npm run monica:proof",),
                platform_order=("ios", "android"),
            ),
        ),
        target_rollout_mode="approved_pr",
        json_output=True,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin_api_key",
            "GITHUB_TOKEN": "gh_token",
            "SLACK_BOT_SCOPES": "app_mentions:read,chat:write,channels:history,files:read",
        },
        which=lambda command: f"/usr/bin/{command}",
        module_available=lambda name: True,
        gateway_status={
            "gateway_state": "running",
            "pid": 53960,
            "start_time": 100.0,
            "active_agents": 0,
            "updated_at": "100.0",
        },
        now=160.0,
    )

    payload = json.loads(capsys.readouterr().out)
    step_ids = [step["id"] for step in payload["steps"]]
    assert exit_code == 1
    assert payload["ready"] is False
    assert {
        "code": "slack_scope_files_write",
        "message": "SLACK_BOT_SCOPES is missing required scope: files:write",
    } in payload["failures"]
    assert "create_slack_app" in step_ids
    assert payload["steps"][-1]["id"] == "rerun_doctor"


def test_setup_plan_json_lists_proof_setup_failure_steps_before_rollout_is_ready(capsys):
    exit_code = run_setup_plan_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            dry_run=False,
            slack=SlackConfig(
                allowed_channels=("C123MOBILE",),
                bot_user_ids=("U123MONICA",),
                approver_user_ids=("U123APPROVER",),
            ),
            linear=LinearConfig(team_id="team-mobile"),
            repo=RepoConfig(url="git@github.com:acme/mobile.git"),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                commands=("uv run --project \"$MONICA_HERMES_AGENT_ROOT\" python -m plugins.mobile_bug_agent.simulator_proof",),
                platform_order=("ios", "android"),
                dev_client_scheme="elixir-card",
                ios_bundle_id="com.elixir.card",
            ),
        ),
        target_rollout_mode="approved_pr",
        json_output=True,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin_api_key",
            "GITHUB_TOKEN": "gh_token",
        },
        which=lambda command: None
        if command in {"xcrun", "xcodebuild", "adb", "emulator"}
        else f"/usr/bin/{command}",
        module_available=lambda name: True,
        gateway_status={
            "gateway_state": "running",
            "pid": 53960,
            "start_time": 100.0,
            "active_agents": 0,
            "updated_at": "100.0",
        },
        now=160.0,
    )

    payload = json.loads(capsys.readouterr().out)
    step_ids = [step["id"] for step in payload["steps"]]
    assert exit_code == 1
    assert payload["ready"] is False
    assert step_ids == [
        "configure_simulator_proof",
        "configure_proof_setup_commands",
        "prepare_ios_simulator",
        "prepare_android_emulator",
        "rerun_doctor",
    ]
    setup_step = payload["steps"][1]
    assert "--proof-setup-command '<auth/session seed command>'" in setup_step["command"]
    assert "MONICA_WORKTREE" in setup_step["why"]
    assert "MONICA_PROOF_DIR" in setup_step["why"]
    assert "MONICA_LINEAR_URL" in setup_step["why"]
    assert "MONICA_BRANCH_NAME" in setup_step["why"]
    assert "MONICA_BASE_REF" in setup_step["why"]
    assert "MONICA_BASE_COMMIT" in setup_step["why"]
    assert "MONICA_DEEP_LINK" in setup_step["why"]
    assert "MONICA_PROOF_EXPECTED_TEXT" in setup_step["why"]
    assert "MONICA_PROOF_SCREEN" in setup_step["why"]
    assert "screen.load" in setup_step["why"]
    assert "target route" in setup_step["why"]
    assert "Monica profile .env" in setup_step["why"]
    simulator_step = payload["steps"][0]
    assert "proof.android_package" in simulator_step["command"]
    assert "Install full Xcode" in payload["steps"][2]["why"]
    assert "Install Android SDK" in payload["steps"][3]["why"]


def test_setup_plan_json_gives_configure_command_when_only_proof_setup_is_missing(capsys):
    proof_command = (
        "uv run --project \"$MONICA_HERMES_AGENT_ROOT\" "
        "python -m plugins.mobile_bug_agent.simulator_proof --timeout-seconds 600"
    )

    exit_code = run_setup_plan_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            dry_run=False,
            slack=SlackConfig(
                allowed_channels=("C123MOBILE",),
                bot_user_ids=("U123MONICA",),
                approver_user_ids=("U123APPROVER",),
            ),
            linear=LinearConfig(team_id="team-mobile"),
            repo=RepoConfig(
                url="git@github.com:acme/mobile.git",
                local_name="elixir-card-app",
                default_branch="dev",
                branch_prefix="monica",
            ),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                commands=(proof_command,),
                platform_order=("ios", "android"),
                dev_client_scheme="elixir-card",
                ios_bundle_id="com.elixir.card",
                android_package="com.elixir.card",
            ),
        ),
        target_rollout_mode="approved_pr",
        json_output=True,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin_api_key",
            "GITHUB_TOKEN": "gh_token",
            "SLACK_BOT_SCOPES": (
                "app_mentions:read,chat:write,channels:history,files:read,files:write"
            ),
        },
        which=lambda command: f"/usr/bin/{command}",
        module_available=lambda name: True,
        gateway_status={
            "gateway_state": "running",
            "pid": 53960,
            "start_time": 100.0,
            "active_agents": 0,
            "updated_at": "100.0",
        },
        now=160.0,
    )

    payload = json.loads(capsys.readouterr().out)
    step_ids = [step["id"] for step in payload["steps"]]
    assert exit_code == 1
    assert step_ids == ["configure_proof_setup_commands", "rerun_doctor"]
    setup_command = payload["steps"][0]["command"]
    assert setup_command.startswith("hermes mobile-bug-agent configure-proof-setup ")
    assert "--proof-setup-command '<auth/session seed command>'" in setup_command
    assert "--proof-required-env-key '<test-auth env key>'" in setup_command
    assert "--repo-url" not in setup_command
    assert proof_command not in setup_command


def test_setup_plan_lists_cached_mobile_repo_cleanup_step(tmp_path):
    runtime = tmp_path / "monica-runtime"
    config = MonicaConfig(
        rollout_mode="approved_pr",
        runtime=RuntimeConfig(home_subdir=str(runtime)),
        repo=RepoConfig(local_name="elixir-card-app"),
    )
    report = SimpleNamespace(
        rollout_mode="approved_pr",
        failures=[
            SimpleNamespace(
                code="repo_cached_dirty",
                message=(
                    "cached mobile repo has uncommitted changes: "
                    f"{runtime / 'workspace' / 'repos' / 'elixir-card-app'}"
                ),
            )
        ],
        warnings=[],
    )

    steps = cli._setup_plan_steps(report, config=config)

    assert [step["id"] for step in steps] == ["clean_cached_mobile_repo", "rerun_doctor"]
    cleanup_step = steps[0]
    assert "cached mobile repo" in cleanup_step["title"].lower()
    assert str(runtime / "workspace" / "repos" / "elixir-card-app") in cleanup_step["command"]
    assert "status --short --branch" in cleanup_step["command"]
    assert "archive" in cleanup_step["why"].lower()


def test_setup_plan_json_gives_configure_command_when_proof_setup_is_noop(capsys):
    proof_command = (
        "uv run --project \"$MONICA_HERMES_AGENT_ROOT\" "
        "python -m plugins.mobile_bug_agent.simulator_proof --timeout-seconds 600"
    )

    exit_code = run_setup_plan_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            dry_run=False,
            slack=SlackConfig(
                allowed_channels=("C123MOBILE",),
                bot_user_ids=("U123MONICA",),
                approver_user_ids=("U123APPROVER",),
            ),
            linear=LinearConfig(team_id="team-mobile"),
            repo=RepoConfig(
                url="git@github.com:acme/mobile.git",
                local_name="elixir-card-app",
                default_branch="dev",
                branch_prefix="monica",
            ),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                commands=(proof_command,),
                setup_commands=("true", "exit 0"),
                platform_order=("ios", "android"),
                dev_client_scheme="elixir-card",
                ios_bundle_id="com.elixir.card",
                android_package="com.elixir.card",
            ),
        ),
        target_rollout_mode="approved_pr",
        json_output=True,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin_api_key",
            "GITHUB_TOKEN": "gh_token",
            "SLACK_BOT_SCOPES": (
                "app_mentions:read,chat:write,channels:history,files:read,files:write"
            ),
        },
        which=lambda command: f"/usr/bin/{command}",
        module_available=lambda name: True,
        gateway_status={
            "gateway_state": "running",
            "pid": 53960,
            "start_time": 100.0,
            "active_agents": 0,
            "updated_at": "100.0",
        },
        now=160.0,
    )

    payload = json.loads(capsys.readouterr().out)
    step_ids = [step["id"] for step in payload["steps"]]
    assert exit_code == 1
    assert step_ids == ["configure_proof_setup_commands", "rerun_doctor"]
    setup_command = payload["steps"][0]["command"]
    assert "--proof-setup-command '<auth/session seed command>'" in setup_command


def test_setup_plan_json_gives_configure_command_when_proof_setup_is_invalid(capsys):
    proof_command = (
        "uv run --project \"$MONICA_HERMES_AGENT_ROOT\" "
        "python -m plugins.mobile_bug_agent.simulator_proof --timeout-seconds 600"
    )

    for setup_command in (
        "<auth/session seed command>",
        "MONICA_TEST_LOGIN_TOKEN=secret npm run monica:seed-auth",
        "npm run monica:seed-auth -- --token token",
    ):
        exit_code = run_setup_plan_command(
            config=MonicaConfig(
                enabled=True,
                rollout_mode="approved_pr",
                dry_run=False,
                slack=SlackConfig(
                    allowed_channels=("C123MOBILE",),
                    bot_user_ids=("U123MONICA",),
                    approver_user_ids=("U123APPROVER",),
                ),
                linear=LinearConfig(team_id="team-mobile"),
                repo=RepoConfig(
                    url="git@github.com:acme/mobile.git",
                    local_name="elixir-card-app",
                    default_branch="dev",
                    branch_prefix="monica",
                ),
                verification=VerificationConfig(commands=("npm test",)),
                proof=ProofConfig(
                    enabled=True,
                    required_for_done=True,
                    commands=(proof_command,),
                    setup_commands=(setup_command,),
                    required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
                    platform_order=("ios", "android"),
                    dev_client_scheme="elixir-card",
                    ios_bundle_id="com.elixir.card",
                    android_package="com.elixir.card",
                ),
            ),
            target_rollout_mode="approved_pr",
            json_output=True,
            environ={
                "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
                "MONICA_SLACK_APP_TOKEN": "xapp-token",
                "LINEAR_API_KEY": "lin_api_key",
                "GITHUB_TOKEN": "gh_token",
                "MONICA_TEST_LOGIN_TOKEN": "token",
                "SLACK_BOT_SCOPES": (
                    "app_mentions:read,chat:write,channels:history,files:read,files:write"
                ),
            },
            which=lambda command: f"/usr/bin/{command}",
            module_available=lambda name: True,
            gateway_status={
                "gateway_state": "running",
                "pid": 53960,
                "start_time": 100.0,
                "active_agents": 0,
                "updated_at": "100.0",
            },
            now=160.0,
        )

        payload = json.loads(capsys.readouterr().out)
        step_ids = [step["id"] for step in payload["steps"]]
        assert exit_code == 1
        assert step_ids == ["configure_proof_setup_commands", "rerun_doctor"]
        repair_command = payload["steps"][0]["command"]
        assert repair_command.startswith(
            "hermes mobile-bug-agent configure-proof-setup "
        )
        assert "--proof-setup-command '<auth/session seed command>'" in repair_command


def test_setup_plan_json_reuses_configured_env_keys_when_repairing_proof_setup(capsys):
    proof_command = (
        "uv run --project \"$MONICA_HERMES_AGENT_ROOT\" "
        "python -m plugins.mobile_bug_agent.simulator_proof --timeout-seconds 600"
    )

    exit_code = run_setup_plan_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            dry_run=False,
            slack=SlackConfig(
                allowed_channels=("C123MOBILE",),
                bot_user_ids=("U123MONICA",),
                approver_user_ids=("U123APPROVER",),
            ),
            linear=LinearConfig(team_id="team-mobile"),
            repo=RepoConfig(
                url="git@github.com:acme/mobile.git",
                local_name="elixir-card-app",
                default_branch="dev",
                branch_prefix="monica",
            ),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                commands=(proof_command,),
                setup_commands=("<auth/session seed command>",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
                platform_order=("ios", "android"),
                dev_client_scheme="elixir-card",
                ios_bundle_id="com.elixir.card",
                android_package="com.elixir.card",
            ),
        ),
        target_rollout_mode="approved_pr",
        json_output=True,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin_api_key",
            "GITHUB_TOKEN": "gh_token",
            "MONICA_TEST_LOGIN_TOKEN": "token",
            "SLACK_BOT_SCOPES": (
                "app_mentions:read,chat:write,channels:history,files:read,files:write"
            ),
        },
        which=lambda command: f"/usr/bin/{command}",
        module_available=lambda name: True,
        gateway_status={
            "gateway_state": "running",
            "pid": 53960,
            "start_time": 100.0,
            "active_agents": 0,
            "updated_at": "100.0",
        },
        now=160.0,
    )

    payload = json.loads(capsys.readouterr().out)
    repair_command = payload["steps"][0]["command"]
    assert exit_code == 1
    assert payload["steps"][0]["id"] == "configure_proof_setup_commands"
    assert "--proof-setup-command '<auth/session seed command>'" in repair_command
    assert "--proof-required-env-key '<test-auth env key>'" not in repair_command


def test_setup_plan_json_keeps_env_key_placeholder_when_existing_keys_are_invalid(capsys):
    proof_command = (
        "uv run --project \"$MONICA_HERMES_AGENT_ROOT\" "
        "python -m plugins.mobile_bug_agent.simulator_proof --timeout-seconds 600"
    )

    exit_code = run_setup_plan_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            dry_run=False,
            slack=SlackConfig(
                allowed_channels=("C123MOBILE",),
                bot_user_ids=("U123MONICA",),
                approver_user_ids=("U123APPROVER",),
            ),
            linear=LinearConfig(team_id="team-mobile"),
            repo=RepoConfig(
                url="git@github.com:acme/mobile.git",
                local_name="elixir-card-app",
                default_branch="dev",
                branch_prefix="monica",
            ),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                commands=(proof_command,),
                setup_commands=("<auth/session seed command>",),
                required_env_keys=("MONICA_WORKTREE",),
                platform_order=("ios", "android"),
                dev_client_scheme="elixir-card",
                ios_bundle_id="com.elixir.card",
                android_package="com.elixir.card",
            ),
        ),
        target_rollout_mode="approved_pr",
        json_output=True,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin_api_key",
            "GITHUB_TOKEN": "gh_token",
            "SLACK_BOT_SCOPES": (
                "app_mentions:read,chat:write,channels:history,files:read,files:write"
            ),
        },
        which=lambda command: f"/usr/bin/{command}",
        module_available=lambda name: True,
        gateway_status={
            "gateway_state": "running",
            "pid": 53960,
            "start_time": 100.0,
            "active_agents": 0,
            "updated_at": "100.0",
        },
        now=160.0,
    )

    payload = json.loads(capsys.readouterr().out)
    repair_command = payload["steps"][0]["command"]
    assert exit_code == 1
    assert payload["steps"][0]["id"] == "configure_proof_setup_commands"
    assert "--proof-setup-command '<auth/session seed command>'" in repair_command
    assert "--proof-required-env-key '<test-auth env key>'" in repair_command


def test_setup_plan_json_gives_env_secret_step_when_required_proof_env_key_is_missing(capsys):
    proof_command = (
        "uv run --project \"$MONICA_HERMES_AGENT_ROOT\" "
        "python -m plugins.mobile_bug_agent.simulator_proof --timeout-seconds 600"
    )

    exit_code = run_setup_plan_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            dry_run=False,
            slack=SlackConfig(
                allowed_channels=("C123MOBILE",),
                bot_user_ids=("U123MONICA",),
                approver_user_ids=("U123APPROVER",),
            ),
            linear=LinearConfig(team_id="team-mobile"),
            repo=RepoConfig(
                url="git@github.com:acme/mobile.git",
                local_name="elixir-card-app",
                default_branch="dev",
                branch_prefix="monica",
            ),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                commands=(proof_command,),
                setup_commands=("npm run monica:seed-auth",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
                platform_order=("ios", "android"),
                dev_client_scheme="elixir-card",
                ios_bundle_id="com.elixir.card",
                android_package="com.elixir.card",
            ),
        ),
        target_rollout_mode="approved_pr",
        json_output=True,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin_api_key",
            "GITHUB_TOKEN": "gh_token",
            "SLACK_BOT_SCOPES": (
                "app_mentions:read,chat:write,channels:history,files:read,files:write"
            ),
        },
        which=lambda command: f"/usr/bin/{command}",
        module_available=lambda name: True,
        gateway_status={
            "gateway_state": "running",
            "pid": 53960,
            "start_time": 100.0,
            "active_agents": 0,
            "updated_at": "100.0",
        },
        now=160.0,
    )

    payload = json.loads(capsys.readouterr().out)
    step_ids = [step["id"] for step in payload["steps"]]
    assert exit_code == 1
    assert step_ids == ["configure_proof_required_env_keys", "rerun_doctor"]
    command = payload["steps"][0]["command"]
    assert "MONICA_TEST_LOGIN_TOKEN" in command
    assert "profile .env" in command


def test_setup_plan_json_gives_env_key_config_step_when_required_proof_env_keys_are_empty(capsys):
    proof_command = (
        "uv run --project \"$MONICA_HERMES_AGENT_ROOT\" "
        "python -m plugins.mobile_bug_agent.simulator_proof --timeout-seconds 600"
    )

    exit_code = run_setup_plan_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            dry_run=False,
            slack=SlackConfig(
                allowed_channels=("C123MOBILE",),
                bot_user_ids=("U123MONICA",),
                approver_user_ids=("U123APPROVER",),
            ),
            linear=LinearConfig(team_id="team-mobile"),
            repo=RepoConfig(
                url="git@github.com:acme/mobile.git",
                local_name="elixir-card-app",
                default_branch="dev",
                branch_prefix="monica",
            ),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                commands=(proof_command,),
                setup_commands=("npm run monica:seed-auth",),
                platform_order=("ios", "android"),
                dev_client_scheme="elixir-card",
                ios_bundle_id="com.elixir.card",
                android_package="com.elixir.card",
            ),
        ),
        target_rollout_mode="approved_pr",
        json_output=True,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin_api_key",
            "GITHUB_TOKEN": "gh_token",
            "SLACK_BOT_SCOPES": (
                "app_mentions:read,chat:write,channels:history,files:read,files:write"
            ),
        },
        which=lambda command: f"/usr/bin/{command}",
        module_available=lambda name: True,
        gateway_status={
            "gateway_state": "running",
            "pid": 53960,
            "start_time": 100.0,
            "active_agents": 0,
            "updated_at": "100.0",
        },
        now=160.0,
    )

    payload = json.loads(capsys.readouterr().out)
    step_ids = [step["id"] for step in payload["steps"]]
    assert exit_code == 1
    assert step_ids == ["configure_proof_required_env_keys", "rerun_doctor"]
    command = payload["steps"][0]["command"]
    assert command.startswith("hermes mobile-bug-agent configure-proof-setup ")
    assert "--proof-setup-command 'npm run monica:seed-auth'" in command
    assert "--proof-required-env-key MONICA_TEST_LOGIN_TOKEN" in command


def test_setup_plan_json_preserves_existing_setup_command_when_required_env_keys_are_empty(capsys):
    proof_command = (
        "uv run --project \"$MONICA_HERMES_AGENT_ROOT\" "
        "python -m plugins.mobile_bug_agent.simulator_proof --timeout-seconds 600"
    )

    exit_code = run_setup_plan_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            dry_run=False,
            slack=SlackConfig(
                allowed_channels=("C123MOBILE",),
                bot_user_ids=("U123MONICA",),
                approver_user_ids=("U123APPROVER",),
            ),
            linear=LinearConfig(team_id="team-mobile"),
            repo=RepoConfig(
                url="git@github.com:acme/mobile.git",
                local_name="elixir-card-app",
                default_branch="dev",
                branch_prefix="monica",
            ),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                commands=(proof_command,),
                setup_commands=("npm run monica:seed-auth",),
                platform_order=("ios", "android"),
                dev_client_scheme="elixir-card",
                ios_bundle_id="com.elixir.card",
                android_package="com.elixir.card",
            ),
        ),
        target_rollout_mode="approved_pr",
        json_output=True,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin_api_key",
            "GITHUB_TOKEN": "gh_token",
            "SLACK_BOT_SCOPES": (
                "app_mentions:read,chat:write,channels:history,files:read,files:write"
            ),
        },
        which=lambda command: f"/usr/bin/{command}",
        module_available=lambda name: True,
        gateway_status={
            "gateway_state": "running",
            "pid": 53960,
            "start_time": 100.0,
            "active_agents": 0,
            "updated_at": "100.0",
        },
        now=160.0,
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    command = payload["steps"][0]["command"]
    assert "--proof-setup-command 'npm run monica:seed-auth'" in command
    assert "<auth/session seed command>" not in command
    assert "--proof-required-env-key MONICA_TEST_LOGIN_TOKEN" in command


def test_setup_plan_json_gives_env_key_config_step_when_required_proof_env_key_is_invalid(capsys):
    proof_command = (
        "uv run --project \"$MONICA_HERMES_AGENT_ROOT\" "
        "python -m plugins.mobile_bug_agent.simulator_proof --timeout-seconds 600"
    )

    exit_code = run_setup_plan_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            dry_run=False,
            slack=SlackConfig(
                allowed_channels=("C123MOBILE",),
                bot_user_ids=("U123MONICA",),
                approver_user_ids=("U123APPROVER",),
            ),
            linear=LinearConfig(team_id="team-mobile"),
            repo=RepoConfig(
                url="git@github.com:acme/mobile.git",
                local_name="elixir-card-app",
                default_branch="dev",
                branch_prefix="monica",
            ),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                commands=(proof_command,),
                setup_commands=("npm run monica:seed-auth",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN=secret",),
                platform_order=("ios", "android"),
                dev_client_scheme="elixir-card",
                ios_bundle_id="com.elixir.card",
                android_package="com.elixir.card",
            ),
        ),
        target_rollout_mode="approved_pr",
        json_output=True,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin_api_key",
            "GITHUB_TOKEN": "gh_token",
            "SLACK_BOT_SCOPES": (
                "app_mentions:read,chat:write,channels:history,files:read,files:write"
            ),
        },
        which=lambda command: f"/usr/bin/{command}",
        module_available=lambda name: True,
        gateway_status={
            "gateway_state": "running",
            "pid": 53960,
            "start_time": 100.0,
            "active_agents": 0,
            "updated_at": "100.0",
        },
        now=160.0,
    )

    payload = json.loads(capsys.readouterr().out)
    step_ids = [step["id"] for step in payload["steps"]]
    assert exit_code == 1
    assert step_ids == ["configure_proof_required_env_keys", "rerun_doctor"]
    command = payload["steps"][0]["command"]
    assert "--proof-required-env-key MONICA_TEST_LOGIN_TOKEN" in command
    assert "secret" not in command


def test_setup_plan_json_does_not_suggest_invalid_required_proof_env_key_name(capsys):
    proof_command = (
        "uv run --project \"$MONICA_HERMES_AGENT_ROOT\" "
        "python -m plugins.mobile_bug_agent.simulator_proof --timeout-seconds 600"
    )

    exit_code = run_setup_plan_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            dry_run=False,
            slack=SlackConfig(
                allowed_channels=("C123MOBILE",),
                bot_user_ids=("U123MONICA",),
                approver_user_ids=("U123APPROVER",),
            ),
            linear=LinearConfig(team_id="team-mobile"),
            repo=RepoConfig(
                url="git@github.com:acme/mobile.git",
                local_name="elixir-card-app",
                default_branch="dev",
                branch_prefix="monica",
            ),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                commands=(proof_command,),
                setup_commands=("npm run monica:seed-auth",),
                required_env_keys=("bad key",),
                platform_order=("ios", "android"),
                dev_client_scheme="elixir-card",
                ios_bundle_id="com.elixir.card",
                android_package="com.elixir.card",
            ),
        ),
        target_rollout_mode="approved_pr",
        json_output=True,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin_api_key",
            "GITHUB_TOKEN": "gh_token",
            "SLACK_BOT_SCOPES": (
                "app_mentions:read,chat:write,channels:history,files:read,files:write"
            ),
        },
        which=lambda command: f"/usr/bin/{command}",
        module_available=lambda name: True,
        gateway_status={
            "gateway_state": "running",
            "pid": 53960,
            "start_time": 100.0,
            "active_agents": 0,
            "updated_at": "100.0",
        },
        now=160.0,
    )

    payload = json.loads(capsys.readouterr().out)
    step_ids = [step["id"] for step in payload["steps"]]
    assert exit_code == 1
    assert step_ids == ["configure_proof_required_env_keys", "rerun_doctor"]
    command = payload["steps"][0]["command"]
    assert "--proof-required-env-key MONICA_TEST_LOGIN_TOKEN" in command
    assert "bad key" not in command


def test_setup_plan_json_gives_configure_command_when_proof_command_is_noop(capsys):
    exit_code = run_setup_plan_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            dry_run=False,
            slack=SlackConfig(
                allowed_channels=("C123MOBILE",),
                bot_user_ids=("U123MONICA",),
                approver_user_ids=("U123APPROVER",),
            ),
            linear=LinearConfig(team_id="team-mobile"),
            repo=RepoConfig(
                url="git@github.com:acme/mobile.git",
                local_name="elixir-card-app",
                default_branch="dev",
                branch_prefix="monica",
            ),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                commands=("true", "exit 0"),
                setup_commands=("npm run monica:seed-auth",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
                platform_order=("ios", "android"),
                dev_client_scheme="elixir-card",
                ios_bundle_id="com.elixir.card",
                android_package="com.elixir.card",
            ),
        ),
        target_rollout_mode="approved_pr",
        json_output=True,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin_api_key",
            "GITHUB_TOKEN": "gh_token",
            "MONICA_TEST_LOGIN_TOKEN": "token",
            "SLACK_BOT_SCOPES": (
                "app_mentions:read,chat:write,channels:history,files:read,files:write"
            ),
        },
        which=lambda command: f"/usr/bin/{command}",
        module_available=lambda name: True,
        gateway_status={
            "gateway_state": "running",
            "pid": 53960,
            "start_time": 100.0,
            "active_agents": 0,
            "updated_at": "100.0",
        },
        now=160.0,
    )

    payload = json.loads(capsys.readouterr().out)
    step_ids = [step["id"] for step in payload["steps"]]
    assert exit_code == 1
    assert step_ids == ["configure_simulator_proof", "rerun_doctor"]
    assert "Set mobile_bug_agent.proof.commands" in payload["steps"][0]["command"]


def test_setup_plan_json_gives_configure_command_when_proof_command_is_invalid(capsys):
    for proof_command in (
        "<simulator proof command>",
        "MONICA_TEST_LOGIN_TOKEN=secret npm run monica:proof",
        "npm run monica:proof -- --token=token",
    ):
        exit_code = run_setup_plan_command(
            config=MonicaConfig(
                enabled=True,
                rollout_mode="approved_pr",
                dry_run=False,
                slack=SlackConfig(
                    allowed_channels=("C123MOBILE",),
                    bot_user_ids=("U123MONICA",),
                    approver_user_ids=("U123APPROVER",),
                ),
                linear=LinearConfig(team_id="team-mobile"),
                repo=RepoConfig(
                    url="git@github.com:acme/mobile.git",
                    local_name="elixir-card-app",
                    default_branch="dev",
                    branch_prefix="monica",
                ),
                verification=VerificationConfig(commands=("npm test",)),
                proof=ProofConfig(
                    enabled=True,
                    required_for_done=True,
                    commands=(proof_command,),
                    setup_commands=("npm run monica:seed-auth",),
                    required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
                    platform_order=("ios", "android"),
                    dev_client_scheme="elixir-card",
                    ios_bundle_id="com.elixir.card",
                    android_package="com.elixir.card",
                ),
            ),
            target_rollout_mode="approved_pr",
            json_output=True,
            environ={
                "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
                "MONICA_SLACK_APP_TOKEN": "xapp-token",
                "LINEAR_API_KEY": "lin_api_key",
                "GITHUB_TOKEN": "gh_token",
                "MONICA_TEST_LOGIN_TOKEN": "token",
                "SLACK_BOT_SCOPES": (
                    "app_mentions:read,chat:write,channels:history,files:read,files:write"
                ),
            },
            which=lambda command: f"/usr/bin/{command}",
            module_available=lambda name: True,
            gateway_status={
                "gateway_state": "running",
                "pid": 53960,
                "start_time": 100.0,
                "active_agents": 0,
                "updated_at": "100.0",
            },
            now=160.0,
        )

        payload = json.loads(capsys.readouterr().out)
        step_ids = [step["id"] for step in payload["steps"]]
        assert exit_code == 1
        assert step_ids == ["configure_simulator_proof", "rerun_doctor"]
        assert "Set mobile_bug_agent.proof.commands" in payload["steps"][0]["command"]


def test_setup_plan_json_gives_platform_order_step_when_approved_pr_omits_android(capsys):
    proof_command = (
        "uv run --project \"$MONICA_HERMES_AGENT_ROOT\" "
        "python -m plugins.mobile_bug_agent.simulator_proof --timeout-seconds 600"
    )

    exit_code = run_setup_plan_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            dry_run=False,
            slack=SlackConfig(
                allowed_channels=("C123MOBILE",),
                bot_user_ids=("U123MONICA",),
                approver_user_ids=("U123APPROVER",),
            ),
            linear=LinearConfig(team_id="team-mobile"),
            repo=RepoConfig(
                url="git@github.com:acme/mobile.git",
                local_name="elixir-card-app",
                default_branch="dev",
                branch_prefix="monica",
            ),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                commands=(proof_command,),
                setup_commands=("npm run monica:seed-auth",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
                platform_order=("ios",),
                dev_client_scheme="elixir-card",
                ios_bundle_id="com.elixir.card",
                android_package="com.elixir.card",
            ),
        ),
        target_rollout_mode="approved_pr",
        json_output=True,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin_api_key",
            "GITHUB_TOKEN": "gh_token",
            "MONICA_TEST_LOGIN_TOKEN": "token",
            "SLACK_BOT_SCOPES": (
                "app_mentions:read,chat:write,channels:history,files:read,files:write"
            ),
        },
        which=lambda command: f"/usr/bin/{command}",
        module_available=lambda name: True,
        gateway_status={
            "gateway_state": "running",
            "pid": 53960,
            "start_time": 100.0,
            "active_agents": 0,
            "updated_at": "100.0",
        },
        now=160.0,
    )

    payload = json.loads(capsys.readouterr().out)
    step_ids = [step["id"] for step in payload["steps"]]
    assert exit_code == 1
    assert step_ids == ["configure_proof_platform_order", "rerun_doctor"]
    step = payload["steps"][0]
    assert "proof.platform_order" in step["command"]
    assert "ios" in step["command"]
    assert "android" in step["command"]
    assert "approved_pr cannot open a PR" in step["why"]


def test_setup_plan_commands_preserve_active_named_profile(monkeypatch, capsys):
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "monica")
    proof_command = (
        "uv run --project \"$MONICA_HERMES_AGENT_ROOT\" "
        "python -m plugins.mobile_bug_agent.simulator_proof --timeout-seconds 600"
    )

    exit_code = run_setup_plan_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            dry_run=False,
            slack=SlackConfig(
                allowed_channels=("C123MOBILE",),
                bot_user_ids=("U123MONICA",),
                approver_user_ids=("U123APPROVER",),
            ),
            linear=LinearConfig(team_id="team-mobile"),
            repo=RepoConfig(
                url="git@github.com:acme/mobile.git",
                local_name="elixir-card-app",
                default_branch="dev",
                branch_prefix="monica",
            ),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                commands=(proof_command,),
                platform_order=("ios", "android"),
                dev_client_scheme="elixir-card",
                ios_bundle_id="com.elixir.card",
                android_package="com.elixir.card",
            ),
        ),
        target_rollout_mode="approved_pr",
        json_output=True,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin_api_key",
            "GITHUB_TOKEN": "gh_token",
            "SLACK_BOT_SCOPES": (
                "app_mentions:read,chat:write,channels:history,files:read,files:write"
            ),
        },
        which=lambda command: f"/usr/bin/{command}",
        module_available=lambda name: True,
        gateway_status={
            "gateway_state": "running",
            "pid": 53960,
            "start_time": 100.0,
            "active_agents": 0,
            "updated_at": "100.0",
        },
        now=160.0,
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    setup_command = payload["steps"][0]["command"]
    rerun_command = payload["steps"][1]["command"]
    assert setup_command.startswith("hermes -p monica mobile-bug-agent configure-proof-setup ")
    assert rerun_command == "hermes -p monica mobile-bug-agent doctor --rollout-mode approved_pr --json"


def test_setup_plan_json_recommends_hermes_update_when_runtime_sync_unrecorded_and_idle(
    tmp_path,
    capsys,
):
    state = MonicaState.open(tmp_path / "state.sqlite")

    exit_code = run_setup_plan_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            dry_run=False,
            slack=SlackConfig(
                allowed_channels=("C123MOBILE",),
                bot_user_ids=("U123MONICA",),
                approver_user_ids=("U123APPROVER",),
            ),
            linear=LinearConfig(team_id="team-mobile"),
            repo=RepoConfig(url="git@github.com:acme/mobile.git"),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                commands=("npm run monica:proof",),
                setup_commands=("npm run monica:seed-auth",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
                platform_order=("ios", "android"),
            ),
        ),
        state=state,
        target_rollout_mode="approved_pr",
        json_output=True,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin_api_key",
            "GITHUB_TOKEN": "gh_token",
            "MONICA_TEST_LOGIN_TOKEN": "token",
            "SLACK_BOT_SCOPES": (
                "app_mentions:read,chat:write,channels:history,files:read,files:write"
            ),
        },
        which=lambda command: f"/usr/bin/{command}",
        module_available=lambda name: True,
        gateway_status={
            "gateway_state": "running",
            "pid": 53960,
            "start_time": 100.0,
            "active_agents": 0,
            "updated_at": "100.0",
        },
        current_commit="abc12345",
        now=160.0,
    )

    payload = json.loads(capsys.readouterr().out)
    step_ids = [step["id"] for step in payload["steps"]]
    assert exit_code == 0
    assert step_ids == ["sync_hermes_runtime", "rerun_doctor"]
    assert payload["steps"][0]["command"] == "hermes update"
    assert "Monica is idle" in payload["steps"][0]["why"]


def test_setup_plan_json_does_not_recommend_update_while_runtime_sync_is_active(
    tmp_path,
    capsys,
):
    state = MonicaState.open(tmp_path / "state.sqlite")
    state.record_runtime_sync(commit="dead1234", synced_at="2026-06-13T10:00:00Z")
    active = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U1",
        request_text="@monica marketplace PDP copy bug",
    )
    state.update_run(active.id, status="proofing", linear_identifier="MOB-123")

    exit_code = run_setup_plan_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            dry_run=False,
            slack=SlackConfig(
                allowed_channels=("C123MOBILE",),
                bot_user_ids=("U123MONICA",),
                approver_user_ids=("U123APPROVER",),
            ),
            linear=LinearConfig(team_id="team-mobile"),
            repo=RepoConfig(url="git@github.com:acme/mobile.git"),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                commands=("npm run monica:proof",),
                setup_commands=("npm run monica:seed-auth",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
                platform_order=("ios", "android"),
            ),
        ),
        state=state,
        target_rollout_mode="approved_pr",
        json_output=True,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin_api_key",
            "GITHUB_TOKEN": "gh_token",
            "MONICA_TEST_LOGIN_TOKEN": "token",
            "SLACK_BOT_SCOPES": (
                "app_mentions:read,chat:write,channels:history,files:read,files:write"
            ),
        },
        which=lambda command: f"/usr/bin/{command}",
        module_available=lambda name: True,
        gateway_status={
            "gateway_state": "running",
            "pid": 53960,
            "start_time": 100.0,
            "active_agents": 0,
            "updated_at": "100.0",
        },
        current_commit="fed67890",
        now=160.0,
    )

    payload = json.loads(capsys.readouterr().out)
    step_ids = [step["id"] for step in payload["steps"]]
    assert exit_code == 0
    assert step_ids == ["wait_for_monica_idle_runtime_sync", "rerun_doctor"]
    assert payload["steps"][0]["command"] == "hermes mobile-bug-agent status --json"
    assert "proofing" in payload["steps"][0]["why"]
    assert "hermes update" not in payload["steps"][0]["command"]


def test_setup_plan_json_lists_approved_pr_rollout_steps(capsys):
    exit_code = run_setup_plan_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="linear_only",
            dry_run=False,
            slack=SlackConfig(
                allowed_channels=("C123MOBILE",),
                bot_user_ids=("U123MONICA",),
            ),
            linear=LinearConfig(team_id="team-mobile"),
        ),
        target_rollout_mode="approved_pr",
        json_output=True,
        environ={
            "SLACK_BOT_TOKEN": "xoxb-token",
            "LINEAR_API_KEY": "lin_api_key",
        },
        which=lambda command: None,
        module_available=lambda name: True,
    )

    payload = json.loads(capsys.readouterr().out)
    step_ids = [step["id"] for step in payload["steps"]]
    assert exit_code == 1
    assert payload["ready"] is False
    assert payload["rollout_mode"] == "approved_pr"
    assert "configure_approved_pr" in step_ids
    assert "prepare_pr_tools" in step_ids
    assert payload["steps"][-1]["id"] == "rerun_doctor"
    assert "configure-approved-pr" in next(
        step["command"] for step in payload["steps"] if step["id"] == "configure_approved_pr"
    )
    assert "--proof-setup-command" in next(
        step["command"] for step in payload["steps"] if step["id"] == "configure_approved_pr"
    )
    assert "--proof-required-env-key" in next(
        step["command"] for step in payload["steps"] if step["id"] == "configure_approved_pr"
    )
    assert "--proof-command" in next(
        step["command"] for step in payload["steps"] if step["id"] == "configure_approved_pr"
    )


def test_mobile_bug_agent_command_reports_invalid_runtime_root_for_state_actions(
    tmp_path,
    monkeypatch,
    capsys,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile-home"))
    monkeypatch.setattr(
        cli,
        "load_monica_config",
        lambda: config_from_mapping({"runtime": {"home_subdir": "../outside-runtime"}}),
    )

    exit_code = cli.mobile_bug_agent_command(
        SimpleNamespace(mobile_bug_agent_action="status", limit=5),
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "Monica runtime is not configured correctly" in out
    assert "mobile_bug_agent.runtime.home_subdir must stay inside HERMES_HOME." in out


def test_doctor_parser_accepts_target_rollout_mode():
    parser = ArgumentParser()

    cli.register_cli(parser)
    args = parser.parse_args(["doctor", "--rollout-mode", "linear_only", "--json"])

    assert args.mobile_bug_agent_action == "doctor"
    assert args.rollout_mode == "linear_only"
    assert args.json is True


def test_setup_plan_parser_accepts_target_rollout_mode_and_json():
    parser = ArgumentParser()

    cli.register_cli(parser)
    args = parser.parse_args(["setup-plan", "--rollout-mode", "linear_only", "--json"])

    assert args.mobile_bug_agent_action == "setup_plan"
    assert args.rollout_mode == "linear_only"
    assert args.json is True


def test_slack_manifest_parser_accepts_app_and_bot_names():
    parser = ArgumentParser()

    cli.register_cli(parser)
    args = parser.parse_args(
        ["slack-manifest", "--app-name", "Monica Staging", "--bot-display-name", "monica"]
    )

    assert args.mobile_bug_agent_action == "slack_manifest"
    assert args.app_name == "Monica Staging"
    assert args.bot_display_name == "monica"


def test_linear_metadata_parser_accepts_json_flag():
    parser = ArgumentParser()

    cli.register_cli(parser)
    args = parser.parse_args(["linear-metadata", "--json"])

    assert args.mobile_bug_agent_action == "linear_metadata"
    assert args.json is True


def test_slack_metadata_parser_accepts_json_flag():
    parser = ArgumentParser()

    cli.register_cli(parser)
    args = parser.parse_args(["slack-metadata", "--json"])

    assert args.mobile_bug_agent_action == "slack_metadata"
    assert args.json is True


def test_status_and_show_parsers_accept_json_flags():
    parser = ArgumentParser()

    cli.register_cli(parser)
    status_args = parser.parse_args(["status", "--json", "--limit", "3"])
    show_args = parser.parse_args(["show", "run-id", "--json"])

    assert status_args.mobile_bug_agent_action == "status"
    assert status_args.json is True
    assert status_args.limit == 3
    assert show_args.mobile_bug_agent_action == "show"
    assert show_args.json is True
    assert show_args.run_id == "run-id"


def test_retry_and_approve_parsers_accept_json_flags():
    parser = ArgumentParser()

    cli.register_cli(parser)
    retry_args = parser.parse_args(["retry", "run-id", "--json"])
    approve_args = parser.parse_args(["approve", "run-id", "--user-id", "U_APPROVER", "--json"])

    assert retry_args.mobile_bug_agent_action == "retry"
    assert retry_args.run_id == "run-id"
    assert retry_args.json is True
    assert approve_args.mobile_bug_agent_action == "approve"
    assert approve_args.run_id == "run-id"
    assert approve_args.user_id == "U_APPROVER"
    assert approve_args.json is True


def test_simulate_parser_accepts_json_flag():
    parser = ArgumentParser()

    cli.register_cli(parser)
    args = parser.parse_args(["simulate", "checkout", "crash", "--json"])

    assert args.mobile_bug_agent_action == "simulate"
    assert args.text == ["checkout", "crash"]
    assert args.json is True


def test_configure_linear_only_parser_accepts_non_secret_rollout_values():
    parser = ArgumentParser()

    cli.register_cli(parser)
    args = parser.parse_args(
        [
            "configure-linear-only",
            "--bot-user-id",
            "U123MONICA",
            "--channel-id",
            "C123MOBILE",
            "--linear-team-id",
            "team-id",
            "--linear-project-id",
            "project-id",
            "--linear-label-id",
            "label-one",
            "--linear-label-id",
            "label-two",
        ]
    )

    assert args.mobile_bug_agent_action == "configure_linear_only"
    assert args.bot_user_ids == ["U123MONICA"]
    assert args.channel_ids == ["C123MOBILE"]
    assert args.linear_team_id == "team-id"
    assert args.linear_project_id == "project-id"
    assert args.linear_label_ids == ["label-one", "label-two"]


def test_configure_dry_run_parser_accepts_optional_slack_scope_values():
    parser = ArgumentParser()

    cli.register_cli(parser)
    args = parser.parse_args(
        [
            "configure-dry-run",
            "--bot-user-id",
            "U123MONICA",
            "--channel-id",
            "C123PRIVATE",
            "--channel-id",
            "G456PRIVATE",
        ]
    )

    assert args.mobile_bug_agent_action == "configure_dry_run"
    assert args.bot_user_ids == ["U123MONICA"]
    assert args.channel_ids == ["C123PRIVATE", "G456PRIVATE"]


def test_configure_approved_pr_parser_accepts_non_secret_rollout_values():
    parser = ArgumentParser()

    cli.register_cli(parser)
    args = parser.parse_args(
        [
            "configure-approved-pr",
            "--approver-user-id",
            "U123APPROVER",
            "--repo-url",
            "git@github.com:acme/mobile-app.git",
            "--verification-command",
            "npm test",
            "--verification-command",
            "npm run lint",
            "--proof-setup-command",
            "npm run monica:seed-auth",
            "--proof-command",
            "npm run monica:proof",
            "--proof-required-env-key",
            "MONICA_TEST_LOGIN_TOKEN",
            "--proof-required-env-key",
            "MONICA_TEST_LOGIN_OTP",
            "--repo-local-name",
            "mobile-app",
            "--default-branch",
            "main",
            "--branch-prefix",
            "monica",
            "--proof-dev-client-scheme",
            "elixir-card",
            "--proof-ios-bundle-id",
            "com.elixir.card",
            "--proof-ios-simulator-udid",
            "IOS-UDID-123",
            "--proof-android-serial",
            "emulator-5554",
            "--proof-android-avd",
            "MonicaPixel",
            "--proof-android-package",
            "com.joinelixir.elixirclub",
            "--proof-timeout-minutes",
            "45",
            "--proof-artifact-dir",
            "proof",
        ]
    )

    assert args.mobile_bug_agent_action == "configure_approved_pr"
    assert args.approver_user_ids == ["U123APPROVER"]
    assert args.repo_url == "git@github.com:acme/mobile-app.git"
    assert args.verification_commands == ["npm test", "npm run lint"]
    assert args.proof_setup_commands == ["npm run monica:seed-auth"]
    assert args.proof_commands == ["npm run monica:proof"]
    assert args.proof_required_env_keys == [
        "MONICA_TEST_LOGIN_TOKEN",
        "MONICA_TEST_LOGIN_OTP",
    ]
    assert args.repo_local_name == "mobile-app"
    assert args.default_branch == "main"
    assert args.branch_prefix == "monica"
    assert args.proof_dev_client_scheme == "elixir-card"
    assert args.proof_ios_bundle_id == "com.elixir.card"
    assert args.proof_ios_simulator_udid == "IOS-UDID-123"
    assert args.proof_android_serial == "emulator-5554"
    assert args.proof_android_avd == "MonicaPixel"
    assert args.proof_android_package == "com.joinelixir.elixirclub"
    assert args.proof_timeout_minutes == 45
    assert args.proof_artifact_dir == "proof"


def test_configure_proof_setup_parser_accepts_auth_seed_commands():
    parser = ArgumentParser()

    cli.register_cli(parser)
    args = parser.parse_args(
        [
            "configure-proof-setup",
            "--proof-setup-command",
            "npm run monica:seed-auth",
            "--proof-setup-command",
            "npm run monica:seed-pdp-session",
            "--proof-required-env-key",
            "MONICA_TEST_LOGIN_TOKEN",
        ]
    )

    assert args.mobile_bug_agent_action == "configure_proof_setup"
    assert args.proof_setup_commands == [
        "npm run monica:seed-auth",
        "npm run monica:seed-pdp-session",
    ]
    assert args.proof_required_env_keys == ["MONICA_TEST_LOGIN_TOKEN"]


def test_configure_local_fix_only_parser_accepts_non_secret_rollout_values():
    parser = ArgumentParser()

    cli.register_cli(parser)
    args = parser.parse_args(
        [
            "configure-local-fix-only",
            "--approver-user-id",
            "U123APPROVER",
            "--repo-url",
            "git@github.com:acme/mobile-app.git",
            "--verification-command",
            "npm test",
            "--repo-local-name",
            "mobile-app",
            "--default-branch",
            "main",
            "--branch-prefix",
            "monica",
        ]
    )

    assert args.mobile_bug_agent_action == "configure_local_fix_only"
    assert args.approver_user_ids == ["U123APPROVER"]
    assert args.repo_url == "git@github.com:acme/mobile-app.git"
    assert args.verification_commands == ["npm test"]
    assert args.repo_local_name == "mobile-app"
    assert args.default_branch == "main"
    assert args.branch_prefix == "monica"


def test_configure_dry_run_persists_non_secret_bootstrap_config(capsys):
    existing_config = {
        "plugins": {
            "enabled": ["other-plugin"],
            "disabled": ["mobile-bug-agent", "legacy-plugin"],
        },
        "mobile_bug_agent": {
            "enabled": False,
            "rollout_mode": "linear_only",
            "dry_run": False,
            "slack": {
                "download_attachments": False,
                "allowed_channels": ["C123EXISTING"],
            },
            "runtime": {
                "worker_session_prefix": "not-monica",
                "skip_memory": False,
            },
        },
    }
    saved: list[dict] = []

    exit_code = cli.run_configure_dry_run_command(
        bot_user_ids=("U123MONICA",),
        channel_ids=("C123PRIVATE", "G456PRIVATE"),
        load_config_fn=lambda: copy.deepcopy(existing_config),
        save_config_fn=lambda config: saved.append(config),
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Configured Monica for dry_run rollout" in out
    assert len(saved) == 1
    saved_config = saved[0]
    assert saved_config["plugins"]["enabled"] == ["mobile-bug-agent", "other-plugin"]
    assert saved_config["plugins"]["disabled"] == ["legacy-plugin"]
    monica = saved_config["mobile_bug_agent"]
    assert monica["enabled"] is True
    assert monica["rollout_mode"] == "dry_run"
    assert monica["dry_run"] is True
    assert monica["slack"]["download_attachments"] is False
    assert monica["slack"]["bot_user_ids"] == ["U123MONICA"]
    assert monica["slack"]["allowed_channels"] == ["C123PRIVATE", "G456PRIVATE"]
    assert monica["loop"]["create_linear"] is True
    assert monica["loop"]["require_fix_approval"] is True
    assert monica["runtime"]["home_subdir"] == "agents/monica"
    assert monica["runtime"]["worker_session_prefix"] == "monica"
    assert monica["runtime"]["skip_memory"] is True
    assert "SLACK_BOT_TOKEN" not in str(saved_config)
    assert "LINEAR_API_KEY" not in str(saved_config)


def test_configure_dry_run_keeps_existing_slack_scope_when_not_provided(capsys):
    existing_config = {
        "plugins": {
            "enabled": ["mobile-bug-agent"],
        },
        "mobile_bug_agent": {
            "slack": {
                "bot_user_ids": ["U123EXISTING"],
                "allowed_channels": ["C123EXISTING"],
            },
        },
    }
    saved: list[dict] = []

    exit_code = cli.run_configure_dry_run_command(
        load_config_fn=lambda: copy.deepcopy(existing_config),
        save_config_fn=lambda config: saved.append(config),
    )

    assert exit_code == 0
    monica = saved[0]["mobile_bug_agent"]
    assert monica["slack"]["bot_user_ids"] == ["U123EXISTING"]
    assert monica["slack"]["allowed_channels"] == ["C123EXISTING"]


def test_configure_dry_run_rejects_handles_and_channel_names_without_saving(capsys):
    saved: list[dict] = []

    exit_code = cli.run_configure_dry_run_command(
        bot_user_ids=("@monica",),
        channel_ids=("#mobile-bugs",),
        load_config_fn=lambda: {},
        save_config_fn=lambda config: saved.append(config),
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert saved == []
    assert "Slack mention user IDs like U012ABCDEF, not handles like @monica" in out
    assert "Slack channel IDs like C123 or G123, not names like #mobile-bugs" in out
    assert "Monica dry_run configuration was not saved." in out


def test_configure_linear_only_persists_non_secret_rollout_config(capsys):
    existing_config = {
        "plugins": {
            "enabled": ["other-plugin"],
            "disabled": ["mobile-bug-agent", "legacy-plugin"],
        },
        "mobile_bug_agent": {
            "slack": {
                "download_attachments": False,
            },
            "loop": {
                "max_thread_messages": 12,
            },
        },
    }
    saved: list[dict] = []

    exit_code = run_configure_linear_only_command(
        bot_user_ids=("U123MONICA",),
        channel_ids=("C123MOBILE", "G456PRIVATE"),
        linear_team_id="team-id",
        linear_project_id="project-id",
        linear_label_ids=("label-one", "label-two"),
        load_config_fn=lambda: copy.deepcopy(existing_config),
        save_config_fn=lambda config: saved.append(config),
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Configured Monica for linear_only rollout" in out
    assert len(saved) == 1
    saved_config = saved[0]
    assert saved_config["plugins"]["enabled"] == ["mobile-bug-agent", "other-plugin"]
    assert saved_config["plugins"]["disabled"] == ["legacy-plugin"]
    monica = saved_config["mobile_bug_agent"]
    assert monica["enabled"] is True
    assert monica["rollout_mode"] == "linear_only"
    assert monica["dry_run"] is False
    assert monica["slack"]["bot_user_ids"] == ["U123MONICA"]
    assert monica["slack"]["allowed_channels"] == ["C123MOBILE", "G456PRIVATE"]
    assert monica["slack"]["download_attachments"] is False
    assert monica["loop"]["create_linear"] is True
    assert monica["loop"]["max_thread_messages"] == 12
    assert monica["linear"]["team_id"] == "team-id"
    assert monica["linear"]["project_id"] == "project-id"
    assert monica["linear"]["label_ids"] == ["label-one", "label-two"]
    assert "SLACK_BOT_TOKEN" not in str(saved_config)
    assert "LINEAR_API_KEY" not in str(saved_config)


def test_configure_approved_pr_persists_non_secret_full_loop_config(capsys):
    existing_config = {
        "plugins": {
            "enabled": ["mobile-bug-agent"],
        },
        "mobile_bug_agent": {
            "enabled": True,
            "rollout_mode": "linear_only",
            "slack": {
                "bot_user_ids": ["U123MONICA"],
                "allowed_channels": ["C123MOBILE"],
            },
            "linear": {
                "team_id": "team-id",
            },
            "runtime": {
                "home_subdir": "agents/monica",
                "worker_session_prefix": "not-monica",
                "skip_memory": False,
            },
            "worker": {
                "backend": "codex_cli",
                "codex_command": "codex",
                "codex_model": "gpt-5-codex",
                "codex_profile": "monica",
                "codex_sandbox": "danger-full-access",
                "codex_approval_policy": "on-request",
                "timeout_minutes": 99,
            },
        },
    }
    saved: list[dict] = []

    exit_code = run_configure_approved_pr_command(
        approver_user_ids=("U123APPROVER",),
        repo_url="git@github.com:acme/mobile-app.git",
        verification_commands=("npm test", "npm run lint"),
        proof_setup_commands=("npm run monica:seed-auth",),
        proof_commands=("npm run monica:proof",),
        proof_required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
        repo_local_name="mobile-app",
        default_branch="main",
        branch_prefix="monica",
        load_config_fn=lambda: copy.deepcopy(existing_config),
        save_config_fn=lambda config: saved.append(config),
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Configured Monica for approved_pr rollout" in out
    assert len(saved) == 1
    monica = saved[0]["mobile_bug_agent"]
    assert monica["enabled"] is True
    assert monica["rollout_mode"] == "approved_pr"
    assert monica["dry_run"] is False
    assert monica["loop"]["create_linear"] is True
    assert monica["loop"]["require_fix_approval"] is True
    assert monica["slack"]["approver_user_ids"] == ["U123APPROVER"]
    assert monica["slack"]["bot_user_ids"] == ["U123MONICA"]
    assert monica["linear"]["team_id"] == "team-id"
    assert monica["repo"]["url"] == "git@github.com:acme/mobile-app.git"
    assert monica["repo"]["local_name"] == "mobile-app"
    assert monica["repo"]["default_branch"] == "main"
    assert monica["repo"]["branch_prefix"] == "monica"
    assert monica["verification"]["commands"] == ["npm test", "npm run lint"]
    assert monica["proof"]["setup_commands"] == ["npm run monica:seed-auth"]
    assert monica["proof"]["commands"] == ["npm run monica:proof"]
    assert monica["proof"]["required_env_keys"] == ["MONICA_TEST_LOGIN_TOKEN"]
    assert monica["runtime"]["worker_session_prefix"] == "monica"
    assert monica["runtime"]["skip_memory"] is True
    assert monica["worker"]["backend"] == "codex_cli"
    assert monica["worker"]["codex_command"] == "codex"
    assert monica["worker"]["codex_model"] == "gpt-5-codex"
    assert monica["worker"]["codex_profile"] == "monica"
    assert monica["worker"]["codex_sandbox"] == "workspace-write"
    assert monica["worker"]["codex_approval_policy"] == "never"
    assert monica["worker"]["timeout_minutes"] == 99
    assert saved[0]["plugins"]["enabled"] == ["mobile-bug-agent"]
    assert "GITHUB_TOKEN" not in str(saved[0])
    assert "SLACK_BOT_TOKEN" not in str(saved[0])


def test_configure_proof_setup_persists_setup_commands_without_overwriting_rollout(capsys):
    existing_config = {
        "plugins": {"enabled": ["mobile-bug-agent"], "disabled": []},
        "mobile_bug_agent": {
            "enabled": True,
            "rollout_mode": "approved_pr",
            "repo": {
                "url": "git@github.com:acme/mobile.git",
                "local_name": "elixir-card-app",
                "default_branch": "dev",
            },
            "proof": {
                "enabled": True,
                "required_for_done": True,
                "platform_order": ["ios", "android"],
                "commands": ["npm run monica:proof"],
                "dev_client_scheme": "elixir-card",
            },
        },
    }
    saved: list[dict] = []

    exit_code = cli.run_configure_proof_setup_command(
        proof_setup_commands=("npm run monica:seed-auth",),
        proof_required_env_keys=(
            " MONICA_TEST_LOGIN_TOKEN ",
            "MONICA_TEST_LOGIN_TOKEN",
            " ",
        ),
        load_config_fn=lambda: copy.deepcopy(existing_config),
        save_config_fn=lambda config: saved.append(config),
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Configured Monica proof setup commands" in out
    assert len(saved) == 1
    monica = saved[0]["mobile_bug_agent"]
    assert monica["rollout_mode"] == "approved_pr"
    assert monica["repo"] == existing_config["mobile_bug_agent"]["repo"]
    assert monica["proof"]["setup_commands"] == ["npm run monica:seed-auth"]
    assert monica["proof"]["required_env_keys"] == ["MONICA_TEST_LOGIN_TOKEN"]
    assert monica["proof"]["commands"] == ["npm run monica:proof"]
    assert monica["proof"]["dev_client_scheme"] == "elixir-card"
    assert saved[0]["plugins"]["enabled"] == ["mobile-bug-agent"]


def test_configure_proof_setup_preserves_existing_required_env_keys_when_not_repassed(capsys):
    existing_config = {
        "plugins": {"enabled": ["mobile-bug-agent"], "disabled": []},
        "mobile_bug_agent": {
            "enabled": True,
            "rollout_mode": "approved_pr",
            "proof": {
                "enabled": True,
                "required_for_done": True,
                "platform_order": ["ios", "android"],
                "setup_commands": ["npm run monica:old-seed-auth"],
                "commands": ["npm run monica:proof"],
                "required_env_keys": ["MONICA_TEST_LOGIN_TOKEN"],
            },
        },
    }
    saved: list[dict] = []

    exit_code = cli.run_configure_proof_setup_command(
        proof_setup_commands=("npm run monica:new-seed-auth",),
        load_config_fn=lambda: copy.deepcopy(existing_config),
        save_config_fn=lambda config: saved.append(config),
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Configured Monica proof setup commands" in out
    assert len(saved) == 1
    proof = saved[0]["mobile_bug_agent"]["proof"]
    assert proof["setup_commands"] == ["npm run monica:new-seed-auth"]
    assert proof["required_env_keys"] == ["MONICA_TEST_LOGIN_TOKEN"]
    assert proof["commands"] == ["npm run monica:proof"]


def test_configure_proof_setup_rejects_placeholder_without_saving(capsys):
    saved: list[dict] = []

    exit_code = cli.run_configure_proof_setup_command(
        proof_setup_commands=("<auth/session seed command>",),
        load_config_fn=lambda: {},
        save_config_fn=lambda config: saved.append(config),
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert saved == []
    assert "--proof-setup-command must be the real auth/session seed command" in out


def test_configure_proof_setup_rejects_inline_secret_env_assignment(capsys):
    saved: list[dict] = []

    exit_code = cli.run_configure_proof_setup_command(
        proof_setup_commands=("MONICA_TEST_LOGIN_TOKEN=secret npm run monica:seed-auth",),
        load_config_fn=lambda: {},
        save_config_fn=lambda config: saved.append(config),
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert saved == []
    assert "--proof-setup-command must not inline secret env assignment" in out
    assert "MONICA_TEST_LOGIN_TOKEN" in out


def test_configure_proof_setup_rejects_inline_session_secret_assignment(capsys):
    saved: list[dict] = []

    exit_code = cli.run_configure_proof_setup_command(
        proof_setup_commands=("MONICA_TEST_SESSION=session-secret npm run monica:seed-auth",),
        proof_required_env_keys=("MONICA_TEST_SESSION",),
        load_config_fn=lambda: {},
        save_config_fn=lambda config: saved.append(config),
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert saved == []
    assert "--proof-setup-command must not inline secret env assignment" in out
    assert "MONICA_TEST_SESSION" in out
    assert "session-secret" not in out


def test_configure_proof_setup_rejects_required_env_key_assignment(capsys):
    saved: list[dict] = []

    exit_code = cli.run_configure_proof_setup_command(
        proof_setup_commands=("npm run monica:seed-auth",),
        proof_required_env_keys=("MONICA_TEST_LOGIN_TOKEN=secret",),
        load_config_fn=lambda: {},
        save_config_fn=lambda config: saved.append(config),
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert saved == []
    assert "--proof-required-env-key must name an environment key" in out
    assert "MONICA_TEST_LOGIN_TOKEN" in out
    assert "secret" not in out


def test_configure_proof_setup_requires_required_env_key(capsys):
    saved: list[dict] = []

    exit_code = cli.run_configure_proof_setup_command(
        proof_setup_commands=("npm run monica:seed-auth",),
        load_config_fn=lambda: {},
        save_config_fn=lambda config: saved.append(config),
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert saved == []
    assert "at least one --proof-required-env-key is required" in out
    assert "Monica proof setup configuration was not saved." in out


def test_configure_approved_pr_persists_non_secret_proof_commands(capsys):
    existing_config = {
        "plugins": {
            "enabled": ["mobile-bug-agent"],
        },
        "mobile_bug_agent": {
            "enabled": True,
            "rollout_mode": "linear_only",
            "slack": {
                "bot_user_ids": ["U123MONICA"],
                "allowed_channels": ["C123MOBILE"],
            },
            "linear": {
                "team_id": "team-id",
            },
        },
    }
    saved: list[dict] = []

    exit_code = run_configure_approved_pr_command(
        approver_user_ids=("U123APPROVER",),
        repo_url="git@github.com:acme/mobile-app.git",
        verification_commands=("npm test",),
        proof_setup_commands=("npm run monica:seed-auth",),
        proof_commands=("npm run monica:proof",),
        proof_required_env_keys=(
            " MONICA_TEST_LOGIN_TOKEN ",
            "MONICA_TEST_LOGIN_OTP",
            "MONICA_TEST_LOGIN_TOKEN",
            " ",
        ),
        load_config_fn=lambda: copy.deepcopy(existing_config),
        save_config_fn=lambda config: saved.append(config),
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Configured Monica for approved_pr rollout" in out
    proof = saved[0]["mobile_bug_agent"]["proof"]
    assert proof["enabled"] is True
    assert proof["required_for_done"] is True
    assert proof["platform_order"] == ["ios", "android"]
    assert proof["setup_commands"] == ["npm run monica:seed-auth"]
    assert proof["commands"] == ["npm run monica:proof"]
    assert proof["required_env_keys"] == [
        "MONICA_TEST_LOGIN_TOKEN",
        "MONICA_TEST_LOGIN_OTP",
    ]
    assert "profile-secret" not in str(saved[0])
    assert "GITHUB_TOKEN" not in str(saved[0])


def test_configure_approved_pr_persists_non_secret_proof_environment(capsys):
    saved: list[dict] = []

    exit_code = run_configure_approved_pr_command(
        approver_user_ids=("U123APPROVER",),
        repo_url="git@github.com:acme/mobile-app.git",
        verification_commands=("npm test",),
        proof_setup_commands=("npm run monica:seed-auth",),
        proof_commands=("npm run monica:proof",),
        proof_required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
        proof_dev_client_scheme="elixir-card",
        proof_ios_bundle_id="com.elixir.card",
        proof_ios_simulator_udid="IOS-UDID-123",
        proof_android_serial="emulator-5554",
        proof_android_avd="MonicaPixel",
        proof_android_package="com.joinelixir.elixirclub",
        proof_timeout_minutes=45,
        proof_artifact_dir="proof",
        load_config_fn=lambda: {"mobile_bug_agent": {"proof": {"timeout_minutes": 10}}},
        save_config_fn=lambda config: saved.append(config),
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Configured Monica for approved_pr rollout" in out
    proof = saved[0]["mobile_bug_agent"]["proof"]
    assert proof["dev_client_scheme"] == "elixir-card"
    assert proof["ios_bundle_id"] == "com.elixir.card"
    assert proof["ios_simulator_udid"] == "IOS-UDID-123"
    assert proof["android_serial"] == "emulator-5554"
    assert proof["android_avd"] == "MonicaPixel"
    assert proof["android_package"] == "com.joinelixir.elixirclub"
    assert proof["timeout_minutes"] == 45
    assert proof["artifact_dir"] == "proof"
    assert proof["required_env_keys"] == ["MONICA_TEST_LOGIN_TOKEN"]
    assert "profile-secret" not in str(saved[0])
    assert "SUPABASE_ANON_KEY" not in str(saved[0])


def test_configure_approved_pr_requires_proof_setup_command(capsys):
    saved: list[dict] = []

    exit_code = run_configure_approved_pr_command(
        approver_user_ids=("U123APPROVER",),
        repo_url="git@github.com:acme/mobile-app.git",
        verification_commands=("npm test",),
        proof_commands=("npm run monica:proof",),
        load_config_fn=lambda: {},
        save_config_fn=lambda config: saved.append(config),
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert saved == []
    assert "at least one --proof-setup-command is required" in out
    assert "Monica approved_pr configuration was not saved." in out


def test_configure_approved_pr_rejects_placeholder_proof_setup_command(capsys):
    saved: list[dict] = []

    exit_code = run_configure_approved_pr_command(
        approver_user_ids=("U123APPROVER",),
        repo_url="git@github.com:acme/mobile-app.git",
        verification_commands=("npm test",),
        proof_setup_commands=("<auth/session seed command>",),
        proof_commands=("npm run monica:proof",),
        load_config_fn=lambda: {},
        save_config_fn=lambda config: saved.append(config),
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert saved == []
    assert "--proof-setup-command must be the real auth/session seed command" in out
    assert "Monica approved_pr configuration was not saved." in out


def test_configure_approved_pr_rejects_embedded_placeholder_proof_commands(capsys):
    saved: list[dict] = []

    exit_code = run_configure_approved_pr_command(
        approver_user_ids=("U123APPROVER",),
        repo_url="git@github.com:acme/mobile-app.git",
        verification_commands=("npm test",),
        proof_setup_commands=("npm run monica:seed -- '<auth/session seed command>'",),
        proof_commands=("sh -c '<simulator proof command>'",),
        proof_required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
        load_config_fn=lambda: {},
        save_config_fn=lambda config: saved.append(config),
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert saved == []
    assert "--proof-setup-command must be the real auth/session seed command" in out
    assert "--proof-command must be the real simulator proof command" in out
    assert "Monica approved_pr configuration was not saved." in out


def test_configure_approved_pr_rejects_noop_proof_setup_command(capsys):
    saved: list[dict] = []

    exit_code = run_configure_approved_pr_command(
        approver_user_ids=("U123APPROVER",),
        repo_url="git@github.com:acme/mobile-app.git",
        verification_commands=("npm test",),
        proof_setup_commands=("true", "exit 0"),
        proof_commands=("npm run monica:proof",),
        load_config_fn=lambda: {},
        save_config_fn=lambda config: saved.append(config),
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert saved == []
    assert "--proof-setup-command must seed test auth/session state" in out
    assert "Monica approved_pr configuration was not saved." in out


def test_configure_approved_pr_rejects_inline_secret_proof_setup_command(capsys):
    saved: list[dict] = []

    exit_code = run_configure_approved_pr_command(
        approver_user_ids=("U123APPROVER",),
        repo_url="git@github.com:acme/mobile-app.git",
        verification_commands=("npm test",),
        proof_setup_commands=("MONICA_TEST_LOGIN_OTP=123456 npm run monica:seed-auth",),
        proof_commands=("npm run monica:proof",),
        load_config_fn=lambda: {},
        save_config_fn=lambda config: saved.append(config),
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert saved == []
    assert "--proof-setup-command must not inline secret env assignment" in out
    assert "MONICA_TEST_LOGIN_OTP" in out
    assert "Monica approved_pr configuration was not saved." in out


def test_configure_approved_pr_requires_required_proof_env_key(capsys):
    saved: list[dict] = []

    exit_code = run_configure_approved_pr_command(
        approver_user_ids=("U123APPROVER",),
        repo_url="git@github.com:acme/mobile-app.git",
        verification_commands=("npm test",),
        proof_setup_commands=("npm run monica:seed-auth",),
        proof_commands=("npm run monica:proof",),
        load_config_fn=lambda: {},
        save_config_fn=lambda config: saved.append(config),
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert saved == []
    assert "at least one --proof-required-env-key is required" in out
    assert "Monica approved_pr configuration was not saved." in out


def test_configure_approved_pr_requires_proof_command(capsys):
    saved: list[dict] = []

    exit_code = run_configure_approved_pr_command(
        approver_user_ids=("U123APPROVER",),
        repo_url="git@github.com:acme/mobile-app.git",
        verification_commands=("npm test",),
        proof_setup_commands=("npm run monica:seed-auth",),
        load_config_fn=lambda: {},
        save_config_fn=lambda config: saved.append(config),
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert saved == []
    assert "at least one --proof-command is required" in out
    assert "Monica approved_pr configuration was not saved." in out


def test_configure_approved_pr_rejects_placeholder_proof_command(capsys):
    saved: list[dict] = []

    exit_code = run_configure_approved_pr_command(
        approver_user_ids=("U123APPROVER",),
        repo_url="git@github.com:acme/mobile-app.git",
        verification_commands=("npm test",),
        proof_setup_commands=("npm run monica:seed-auth",),
        proof_commands=("<simulator proof command>",),
        load_config_fn=lambda: {},
        save_config_fn=lambda config: saved.append(config),
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert saved == []
    assert "--proof-command must be the real simulator proof command" in out
    assert "Monica approved_pr configuration was not saved." in out


def test_configure_approved_pr_rejects_noop_proof_command(capsys):
    saved: list[dict] = []

    exit_code = run_configure_approved_pr_command(
        approver_user_ids=("U123APPROVER",),
        repo_url="git@github.com:acme/mobile-app.git",
        verification_commands=("npm test",),
        proof_setup_commands=("npm run monica:seed-auth",),
        proof_commands=("true", "exit 0"),
        load_config_fn=lambda: {},
        save_config_fn=lambda config: saved.append(config),
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert saved == []
    assert "--proof-command must capture simulator proof artifacts" in out
    assert "Monica approved_pr configuration was not saved." in out


def test_configure_approved_pr_requires_builtin_simulator_proof_app_settings(capsys):
    saved: list[dict] = []

    exit_code = run_configure_approved_pr_command(
        approver_user_ids=("U123APPROVER",),
        repo_url="git@github.com:acme/mobile-app.git",
        verification_commands=("npm test",),
        proof_setup_commands=("npm run monica:seed-auth",),
        proof_commands=(
            'uv run --project "$MONICA_HERMES_AGENT_ROOT" '
            "python -m plugins.mobile_bug_agent.simulator_proof --timeout-seconds 600",
        ),
        load_config_fn=lambda: {},
        save_config_fn=lambda config: saved.append(config),
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert saved == []
    assert "proof.dev_client_scheme is required for built-in simulator proof" in out
    assert "proof.ios_bundle_id is required for built-in simulator proof" in out
    assert "proof.android_package is required for built-in simulator proof" in out
    assert "Monica approved_pr configuration was not saved." in out


def test_configure_local_fix_only_persists_non_secret_local_code_config(capsys):
    existing_config = {
        "plugins": {
            "enabled": ["mobile-bug-agent"],
        },
        "mobile_bug_agent": {
            "enabled": True,
            "rollout_mode": "linear_only",
            "slack": {
                "bot_user_ids": ["U123MONICA"],
                "allowed_channels": ["D123MONICA"],
            },
            "linear": {
                "team_id": "team-id",
                "label_ids": ["bug-label"],
            },
            "worker": {
                "backend": "codex_cli",
                "codex_command": "codex",
                "codex_sandbox": "danger-full-access",
                "codex_approval_policy": "on-request",
            },
        },
    }
    saved: list[dict] = []

    exit_code = run_configure_local_fix_only_command(
        approver_user_ids=("U123APPROVER",),
        repo_url="git@github.com:acme/mobile-app.git",
        verification_commands=("npm test", "npm run lint"),
        repo_local_name="mobile-app",
        default_branch="main",
        branch_prefix="monica",
        load_config_fn=lambda: copy.deepcopy(existing_config),
        save_config_fn=lambda config: saved.append(config),
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Configured Monica for local_fix_only rollout" in out
    assert len(saved) == 1
    monica = saved[0]["mobile_bug_agent"]
    assert monica["enabled"] is True
    assert monica["rollout_mode"] == "local_fix_only"
    assert monica["dry_run"] is False
    assert monica["loop"]["create_linear"] is True
    assert monica["loop"]["require_fix_approval"] is True
    assert monica["slack"]["approver_user_ids"] == ["U123APPROVER"]
    assert monica["slack"]["bot_user_ids"] == ["U123MONICA"]
    assert monica["slack"]["allowed_channels"] == ["D123MONICA"]
    assert monica["linear"]["team_id"] == "team-id"
    assert monica["linear"]["label_ids"] == ["bug-label"]
    assert monica["repo"]["url"] == "git@github.com:acme/mobile-app.git"
    assert monica["repo"]["local_name"] == "mobile-app"
    assert monica["repo"]["default_branch"] == "main"
    assert monica["repo"]["branch_prefix"] == "monica"
    assert monica["verification"]["commands"] == ["npm test", "npm run lint"]
    assert monica["worker"]["codex_sandbox"] == "workspace-write"
    assert monica["worker"]["codex_approval_policy"] == "never"
    assert "GITHUB_TOKEN" not in str(saved[0])


def test_configure_linear_only_rejects_handles_and_channel_names_without_saving(capsys):
    saved: list[dict] = []

    exit_code = run_configure_linear_only_command(
        bot_user_ids=("@monica",),
        channel_ids=("#mobile-bugs",),
        linear_team_id="team-id",
        load_config_fn=lambda: {},
        save_config_fn=lambda config: saved.append(config),
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert saved == []
    assert "Slack mention user IDs like U012ABCDEF, not handles like @monica" in out
    assert "Slack channel IDs like C123 or G123, not names like #mobile-bugs" in out


def test_configure_approved_pr_rejects_unsafe_values_without_saving(capsys):
    saved: list[dict] = []

    exit_code = run_configure_approved_pr_command(
        approver_user_ids=("@ritik",),
        repo_url="",
        verification_commands=(),
        repo_local_name="../mobile-app",
        default_branch="main branch",
        branch_prefix="chandler",
        load_config_fn=lambda: {},
        save_config_fn=lambda config: saved.append(config),
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert saved == []
    assert "Slack user IDs like U123, not handles like @ritik" in out
    assert "--repo-url is required" in out
    assert "at least one --verification-command is required" in out
    assert "repo.local_name must be a simple directory name" in out
    assert "repo.branch_prefix must not point at Chandler" in out
    assert "repo.default_branch must be a safe git branch name" in out


def test_doctor_allows_dry_run_with_slack_token(capsys):
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="dry_run",
            slack=SlackConfig(bot_user_ids=("UMONICA",), allowed_channels=("C123",)),
        ),
        environ={"MONICA_SLACK_BOT_TOKEN": "xoxb-token"},
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Monica doctor: ready" in out
    assert "Monica runtime root:" in out


def test_doctor_requires_monica_token_even_when_chandler_slack_token_exists(capsys):
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="dry_run",
            slack=SlackConfig(bot_user_ids=("UMONICA",), allowed_channels=("C123",)),
        ),
        environ={"SLACK_BOT_TOKEN": "xoxb-chandler"},
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "FAIL: MONICA_SLACK_BOT_TOKEN is missing" in out
    assert "xoxb-chandler" not in out
    assert "Monica doctor: not ready" in out


def test_doctor_preflights_target_rollout_without_mutating_config(capsys):
    config = MonicaConfig(
        enabled=True,
        rollout_mode="dry_run",
        dry_run=True,
        slack=SlackConfig(
            bot_user_ids=("U123MONICA",),
            allowed_channels=("C123MOBILE",),
        ),
    )

    exit_code = run_doctor_command(
        config=config,
        target_rollout_mode="linear_only",
        environ={
            "SLACK_BOT_TOKEN": "xoxb-token",
            "SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin_api_token",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda _name: True,
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert config.rollout_mode == "dry_run"
    assert config.dry_run is True
    assert "Monica rollout mode: linear_only" in out
    assert "FAIL: linear.team_id is missing" in out
    assert "Monica doctor: not ready" in out


def test_doctor_json_outputs_machine_readable_readiness(capsys):
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="dry_run",
            slack=SlackConfig(bot_user_ids=("U123MONICA",), allowed_channels=("C123MOBILE",)),
        ),
        json_output=True,
        environ={"MONICA_SLACK_BOT_TOKEN": "xoxb-token"},
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: name != "slack_sdk",
        gateway_status={},
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["ready"] is False
    assert payload["rollout_mode"] == "dry_run"
    assert payload["runtime_root"]
    assert payload["warnings"] == [
        {
            "code": "slack_app_token",
            "message": "MONICA_SLACK_APP_TOKEN is missing; Monica Slack Socket Mode may not start",
        }
    ]
    assert payload["failures"] == [
        {
            "code": "slack_sdk",
            "message": "slack-sdk Python package is not installed; install `hermes-agent[slack]` or `hermes-agent[messaging]`",
        }
    ]
    assert "xoxb-token" not in json.dumps(payload)


def test_doctor_json_warns_when_gateway_is_stopped(capsys):
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="dry_run",
            slack=SlackConfig(bot_user_ids=("U123MONICA",), allowed_channels=("C123MOBILE",)),
        ),
        json_output=True,
        environ={"MONICA_SLACK_BOT_TOKEN": "xoxb-token"},
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        gateway_status={
            "gateway_state": "stopped",
            "pid": 53960,
            "active_agents": 0,
            "updated_at": "2026-06-12T18:12:10Z",
        },
        current_commit="abc12345",
        now=160.0,
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["warnings"] == [
        {
            "code": "slack_app_token",
            "message": "MONICA_SLACK_APP_TOKEN is missing; Monica Slack Socket Mode may not start",
        },
        {
            "code": "gateway_not_running",
            "message": (
                "Monica gateway is stopped; live Slack tag/DM intake and approvals "
                "will not be received until the gateway is running."
            ),
        },
    ]
    assert payload["health"]["gateway"]["state"] == "stopped"


def test_doctor_json_blocks_approved_pr_when_gateway_is_stopped(capsys):
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            slack=SlackConfig(
                bot_user_ids=("U123MONICA",),
                allowed_channels=("C123MOBILE",),
                approver_user_ids=("U123APPROVER",),
            ),
            linear=LinearConfig(team_id="team-mobile"),
            repo=RepoConfig(url="git@github.com:acme/mobile.git"),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                commands=("npm run monica:proof",),
                setup_commands=("npm run monica:seed-auth",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
                platform_order=("ios", "android"),
            ),
        ),
        json_output=True,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "GITHUB_TOKEN": "gh-token",
            "MONICA_TEST_LOGIN_TOKEN": "token",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        gateway_status={
            "gateway_state": "stopped",
            "pid": 53960,
            "active_agents": 0,
            "updated_at": "2026-06-12T18:12:10Z",
        },
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["ready"] is False
    assert {
        "code": "gateway_not_running",
        "message": (
            "Monica gateway is stopped; live Slack tag/DM intake and approvals "
            "will not be received until the gateway is running."
        ),
    } in payload["failures"]
    assert payload["health"]["gateway"]["state"] == "stopped"


def test_doctor_json_blocks_approved_pr_when_gateway_is_manual_process(
    capsys,
    monkeypatch,
):
    monkeypatch.setattr(
        cli,
        "_read_gateway_status",
        lambda: {
            "gateway_state": "running",
            "pid": 53960,
            "start_time": 100.0,
            "active_agents": 0,
            "updated_at": "100.0",
        },
    )
    monkeypatch.setattr(
        cli,
        "_read_gateway_service_status",
        lambda: {
            "available": True,
            "manager": "manual process",
            "service_installed": False,
            "service_running": False,
            "gateway_pids": [53960],
            "service_scope": None,
            "supervised": False,
        },
        raising=False,
    )

    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            slack=SlackConfig(
                bot_user_ids=("U123MONICA",),
                allowed_channels=("C123MOBILE",),
                approver_user_ids=("U123APPROVER",),
            ),
            linear=LinearConfig(team_id="team-mobile"),
            repo=RepoConfig(url="git@github.com:acme/mobile.git"),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                commands=("npm run monica:proof",),
                setup_commands=("npm run monica:seed-auth",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
                platform_order=("ios", "android"),
            ),
        ),
        target_rollout_mode="approved_pr",
        json_output=True,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin_api_key",
            "GITHUB_TOKEN": "gh_token",
            "MONICA_TEST_LOGIN_TOKEN": "token",
            "SLACK_BOT_SCOPES": (
                "app_mentions:read,chat:write,channels:history,files:read,files:write"
            ),
        },
        which=lambda command: f"/usr/bin/{command}",
        module_available=lambda name: True,
        now=160.0,
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["ready"] is False
    assert payload["health"]["gateway_service"]["manager"] == "manual process"
    assert {
        "code": "gateway_not_supervised",
        "message": (
            "Monica gateway is running manually; approved_pr work needs the "
            "profile-aware gateway service so Slack approvals survive restarts "
            "and terminal exits."
        ),
    } in payload["failures"]


def test_doctor_json_blocks_approved_pr_when_gateway_status_is_stale(capsys):
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            slack=SlackConfig(
                bot_user_ids=("U123MONICA",),
                allowed_channels=("C123MOBILE",),
                approver_user_ids=("U123APPROVER",),
            ),
            linear=LinearConfig(team_id="team-mobile"),
            repo=RepoConfig(url="git@github.com:acme/mobile.git"),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                commands=("npm run monica:proof",),
                setup_commands=("npm run monica:seed-auth",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
                platform_order=("ios", "android"),
            ),
        ),
        json_output=True,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "GITHUB_TOKEN": "gh-token",
            "MONICA_TEST_LOGIN_TOKEN": "token",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        gateway_status={
            "gateway_state": "running",
            "pid": 53960,
            "active_agents": 0,
            "updated_at": "1970-01-01T00:00:00Z",
        },
        now=2000.0,
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["ready"] is False
    assert {
        "code": "gateway_status_stale",
        "message": (
            "Monica gateway status is stale; live Slack tag/DM intake and approvals "
            "may not be received until the gateway heartbeat is fresh."
        ),
    } in payload["failures"]
    assert payload["health"]["gateway"]["state"] == "running"
    assert payload["health"]["gateway"]["stale"] is True
    assert payload["health"]["gateway"]["updated_age_seconds"] == 2000


def test_doctor_json_blocks_approved_pr_when_gateway_heartbeat_is_missing(capsys):
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            slack=SlackConfig(
                bot_user_ids=("U123MONICA",),
                allowed_channels=("C123MOBILE",),
                approver_user_ids=("U123APPROVER",),
            ),
            linear=LinearConfig(team_id="team-mobile"),
            repo=RepoConfig(url="git@github.com:acme/mobile.git"),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                commands=("npm run monica:proof",),
                setup_commands=("npm run monica:seed-auth",),
                platform_order=("ios", "android"),
            ),
        ),
        json_output=True,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "GITHUB_TOKEN": "gh-token",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        gateway_status={
            "gateway_state": "running",
            "pid": 53960,
            "active_agents": 0,
        },
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["ready"] is False
    assert {
        "code": "gateway_heartbeat_missing",
        "message": (
            "Monica gateway heartbeat is missing; restart the gateway so live Slack "
            "tag/DM intake and approvals run on the current Monica runtime."
        ),
    } in payload["failures"]
    assert payload["health"]["gateway"]["state"] == "running"
    assert payload["health"]["gateway"]["updated_at"] == ""
    assert payload["health"]["gateway"]["updated_age_seconds"] is None


def test_doctor_json_blocks_approved_pr_when_gateway_start_time_is_missing(capsys):
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            slack=SlackConfig(
                bot_user_ids=("U123MONICA",),
                allowed_channels=("C123MOBILE",),
                approver_user_ids=("U123APPROVER",),
            ),
            linear=LinearConfig(team_id="team-mobile"),
            repo=RepoConfig(url="git@github.com:acme/mobile.git"),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                commands=("npm run monica:proof",),
                setup_commands=("npm run monica:seed-auth",),
                platform_order=("ios", "android"),
            ),
        ),
        json_output=True,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "GITHUB_TOKEN": "gh-token",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        gateway_status={
            "gateway_state": "running",
            "pid": 53960,
            "active_agents": 0,
            "updated_at": "100.0",
        },
        now=160.0,
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["ready"] is False
    assert {
        "code": "gateway_runtime_metadata_missing",
        "message": (
            "Monica gateway runtime metadata is incomplete; restart the gateway "
            "so doctor can verify uptime before approved_pr work runs."
        ),
    } in payload["failures"]
    assert payload["health"]["gateway"]["state"] == "running"
    assert payload["health"]["gateway"]["start_time"] is None
    assert payload["health"]["gateway"]["uptime_seconds"] is None
    assert payload["health"]["gateway"]["updated_age_seconds"] == 60


def test_doctor_json_blocks_approved_pr_when_gateway_status_is_unavailable(capsys):
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            slack=SlackConfig(
                bot_user_ids=("U123MONICA",),
                allowed_channels=("C123MOBILE",),
                approver_user_ids=("U123APPROVER",),
            ),
            linear=LinearConfig(team_id="team-mobile"),
            repo=RepoConfig(url="git@github.com:acme/mobile.git"),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                commands=("npm run monica:proof",),
                setup_commands=("npm run monica:seed-auth",),
                platform_order=("ios", "android"),
            ),
        ),
        json_output=True,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "GITHUB_TOKEN": "gh-token",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        gateway_status={},
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["ready"] is False
    assert {
        "code": "gateway_not_running",
        "message": (
            "Monica gateway status is unavailable; live Slack tag/DM intake "
            "and approvals will not be received until the gateway is running."
        ),
    } in payload["failures"]
    assert payload["health"]["gateway"]["state"] == ""


def test_doctor_json_blocks_approved_pr_when_plugin_is_not_enabled(monkeypatch, capsys):
    ready_report = SimpleNamespace(
        ready=True,
        rollout_mode="approved_pr",
        runtime_root_value="/tmp/monica",
        warnings=(),
        failures=(),
    )
    hermes_config = {
        "plugins": {
            "enabled": ["other-plugin"],
            "disabled": [],
        },
        "mobile_bug_agent": {
            "enabled": True,
            "rollout_mode": "approved_pr",
        },
    }
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: copy.deepcopy(hermes_config))
    monkeypatch.setattr(cli, "check_monica_readiness", lambda **_kwargs: ready_report)
    monkeypatch.setattr(
        cli,
        "_read_gateway_status",
        lambda: {"gateway_state": "running", "pid": 53960, "active_agents": 0},
    )
    monkeypatch.setattr(cli, "_current_hermes_commit", lambda: "abc12345")

    exit_code = cli.mobile_bug_agent_command(
        SimpleNamespace(mobile_bug_agent_action="doctor", rollout_mode="approved_pr", json=True)
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["ready"] is False
    assert {
        "code": "plugin_not_enabled",
        "message": (
            "plugins.enabled must include mobile-bug-agent and plugins.disabled must not; "
            "otherwise Monica's Slack gateway hooks will not load."
        ),
    } in payload["failures"]
    assert payload["health"]["plugin"] == {
        "configured": True,
        "enabled": False,
        "disabled": False,
        "name": "mobile-bug-agent",
    }


def test_doctor_json_reports_gateway_health_and_commit(tmp_path, capsys):
    state = MonicaState.open(tmp_path / "state.sqlite")
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="dry_run",
            slack=SlackConfig(bot_user_ids=("U123MONICA",), allowed_channels=("C123MOBILE",)),
        ),
        state=state,
        json_output=True,
        environ={"MONICA_SLACK_BOT_TOKEN": "xoxb-token"},
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        gateway_status={
            "gateway_state": "running",
            "pid": 1234,
            "start_time": 100.0,
            "active_agents": 2,
            "updated_at": "2026-06-13T00:00:00Z",
        },
        current_commit="abc12345",
        now=160.0,
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["health"] == {
        "hermes_commit": "abc12345",
        "gateway": {
            "state": "running",
            "pid": 1234,
            "active_agents": 2,
            "start_time": 100.0,
            "uptime_seconds": 60,
            "updated_at": "2026-06-13T00:00:00Z",
            "updated_age_seconds": 0,
            "stale": False,
        },
        "gateway_service": {"available": False},
        "runtime_sync": {
            "available": True,
            "idle": True,
            "ready_for_monica_work": True,
            "current_commit": "abc12345",
            "stale": False,
            "last_synced_commit": "",
            "last_synced_at": "",
            "last_sync_status": "",
            "last_sync_failure_reason": "",
            "lease": {
                "active": False,
                "id": "",
                "started_at": "",
                "expires_at": "",
                "pre_update_commit": "",
                "project_root": "",
                "owner_id": "",
                "owner_pid": 0,
                "owner_host": "",
            },
            "blocking_statuses": list(RUNTIME_SYNC_BLOCKING_STATUSES),
            "active_run_count": 0,
            "active_runs": [],
        },
    }


def test_doctor_json_warns_when_runtime_sync_has_never_been_recorded(tmp_path, capsys):
    state = MonicaState.open(tmp_path / "state.sqlite")

    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            slack=SlackConfig(
                bot_user_ids=("U123MONICA",),
                allowed_channels=("C123MOBILE",),
                approver_user_ids=("U123APPROVER",),
            ),
            linear=LinearConfig(team_id="team-mobile"),
            repo=RepoConfig(url="git@github.com:acme/mobile.git"),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                commands=("npm run monica:proof",),
                setup_commands=("npm run monica:seed-auth",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
                platform_order=("ios", "android"),
            ),
        ),
        state=state,
        json_output=True,
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "GITHUB_TOKEN": "gh-token",
            "MONICA_TEST_LOGIN_TOKEN": "token",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        gateway_status={
            "gateway_state": "running",
            "pid": 1234,
            "start_time": 100.0,
            "active_agents": 0,
            "updated_at": "2026-06-13T00:00:00Z",
        },
        current_commit="abc12345",
        now=160.0,
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["health"]["runtime_sync"]["idle"] is True
    assert payload["health"]["runtime_sync"]["current_commit"] == "abc12345"
    assert payload["health"]["runtime_sync"]["last_synced_commit"] == ""
    assert any(warning["code"] == "runtime_sync_unrecorded" for warning in payload["warnings"])


def test_doctor_json_reports_runtime_sync_health(tmp_path, capsys):
    state = MonicaState.open(tmp_path / "state.sqlite")
    state.record_runtime_sync(commit="abc12345", synced_at="2026-06-13T10:00:00Z")
    active = state.create_run(
        platform="slack",
        channel_id="C123",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000100",
        user_id="U1",
        request_text="@monica marketplace PDP copy bug",
    )
    state.update_run(active.id, status="opening_pr", linear_identifier="MOB-123")

    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="dry_run",
            slack=SlackConfig(bot_user_ids=("U123MONICA",), allowed_channels=("C123MOBILE",)),
        ),
        state=state,
        json_output=True,
        environ={"MONICA_SLACK_BOT_TOKEN": "xoxb-token"},
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        gateway_status={"gateway_state": "running", "pid": 1234, "active_agents": 0},
        current_commit="def67890",
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["health"]["runtime_sync"]["idle"] is False
    assert payload["health"]["runtime_sync"]["active_run_count"] == 1
    assert payload["health"]["runtime_sync"]["stale"] is True
    assert payload["health"]["runtime_sync"]["last_synced_commit"] == "abc12345"
    assert payload["health"]["runtime_sync"]["last_synced_at"] == "2026-06-13T10:00:00Z"
    assert payload["health"]["runtime_sync"]["active_runs"][0]["id"] == active.id
    assert payload["health"]["runtime_sync"]["active_runs"][0]["status"] == "opening_pr"
    assert any(warning["code"] == "runtime_sync_stale" for warning in payload["warnings"])


def test_doctor_json_fails_during_runtime_sync_lease(tmp_path, capsys):
    state = MonicaState.open(tmp_path / "state.sqlite")
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

    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="dry_run",
            slack=SlackConfig(bot_user_ids=("U123MONICA",), allowed_channels=("C123MOBILE",)),
        ),
        state=state,
        json_output=True,
        environ={"MONICA_SLACK_BOT_TOKEN": "xoxb-token"},
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        current_commit="dead1234",
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["ready"] is False
    assert payload["health"]["runtime_sync"]["lease"]["active"] is True
    assert any(failure["code"] == "runtime_sync_in_progress" for failure in payload["failures"])


def test_doctor_text_warns_when_gateway_is_stopped(capsys):
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="dry_run",
            slack=SlackConfig(bot_user_ids=("U123MONICA",), allowed_channels=("C123MOBILE",)),
        ),
        environ={"MONICA_SLACK_BOT_TOKEN": "xoxb-token"},
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        gateway_status={
            "gateway_state": "stopped",
            "pid": 53960,
            "active_agents": 0,
            "updated_at": "2026-06-12T18:12:10Z",
        },
        current_commit="abc12345",
        now=160.0,
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Gateway: stopped pid:53960 uptime:- active_agents:0" in out
    assert (
        "WARN: Monica gateway is stopped; live Slack tag/DM intake and approvals "
        "will not be received until the gateway is running."
    ) in out
    assert "Monica doctor: ready" in out


def test_doctor_text_reports_gateway_health_and_commit(capsys):
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="dry_run",
            slack=SlackConfig(bot_user_ids=("U123MONICA",), allowed_channels=("C123MOBILE",)),
        ),
        environ={"MONICA_SLACK_BOT_TOKEN": "xoxb-token"},
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        gateway_status={
            "gateway_state": "running",
            "pid": 1234,
            "start_time": 100.0,
            "active_agents": 2,
            "updated_at": "2026-06-13T00:00:00Z",
        },
        current_commit="abc12345",
        now=160.0,
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Hermes commit: abc12345" in out
    assert "Gateway: running pid:1234 uptime:60s active_agents:2" in out
    assert "Monica doctor: ready" in out


def test_doctor_fails_when_slack_sdk_is_missing(capsys):
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="dry_run",
            slack=SlackConfig(bot_user_ids=("U_MONICA",), allowed_channels=("C123",)),
        ),
        environ={"SLACK_BOT_TOKEN": "xoxb-token"},
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: name != "slack_sdk",
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert (
        "FAIL: slack-sdk Python package is not installed; install "
        "`hermes-agent[slack]` or `hermes-agent[messaging]`"
    ) in out
    assert "Monica doctor: not ready" in out


def test_doctor_reports_invalid_runtime_root_without_traceback(capsys):
    exit_code = run_doctor_command(
        config=config_from_mapping(
            {
                "enabled": True,
                "rollout_mode": "dry_run",
                "runtime": {"home_subdir": "../outside-runtime"},
                "slack": {"bot_user_ids": ["U_MONICA"], "allowed_channels": ["C123"]},
            }
        ),
        environ={"SLACK_BOT_TOKEN": "xoxb-token"},
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "FAIL: mobile_bug_agent.runtime.home_subdir must stay inside HERMES_HOME." in out
    assert "Monica doctor: not ready" in out


def test_doctor_blocks_dry_run_with_bot_id_mentions(capsys):
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="dry_run",
            slack=SlackConfig(bot_user_ids=("B012ABCDEF",), allowed_channels=("C123",)),
        ),
        environ={"SLACK_BOT_TOKEN": "xoxb-token"},
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert (
        "FAIL: slack.bot_user_ids must contain Slack mention user IDs like U012ABCDEF, "
        "not bot_id values like B012ABCDEF"
    ) in out
    assert "Monica doctor: not ready" in out


def test_doctor_blocks_dry_run_with_handle_style_bot_user_id(capsys):
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="dry_run",
            slack=SlackConfig(bot_user_ids=("@monica",), allowed_channels=("C123",)),
        ),
        environ={"SLACK_BOT_TOKEN": "xoxb-token"},
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert (
        "FAIL: slack.bot_user_ids must contain Slack mention user IDs like U012ABCDEF, "
        "not handles like @monica"
    ) in out
    assert "Monica doctor: not ready" in out


def test_doctor_blocks_dry_run_with_malformed_bot_user_id(capsys):
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="dry_run",
            slack=SlackConfig(bot_user_ids=("monica",), allowed_channels=("C123",)),
        ),
        environ={"SLACK_BOT_TOKEN": "xoxb-token"},
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert (
        "FAIL: slack.bot_user_ids must contain Slack mention user IDs like U012ABCDEF, "
        "not invalid values like monica"
    ) in out
    assert "Monica doctor: not ready" in out


def test_doctor_blocks_side_effect_rollouts_with_bot_id_mentions(capsys):
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="linear_only",
            slack=SlackConfig(bot_user_ids=("B012ABCDEF",), allowed_channels=("C123",)),
            linear=LinearConfig(team_id="team-id"),
        ),
        environ={
            "SLACK_BOT_TOKEN": "xoxb-token",
            "SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        gateway_status={
            "gateway_state": "running",
            "pid": 53960,
            "active_agents": 0,
            "updated_at": "100.0",
        },
        now=160.0,
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert (
        "FAIL: slack.bot_user_ids must contain Slack mention user IDs like U012ABCDEF, "
        "not bot_id values like B012ABCDEF"
    ) in out
    assert "Monica doctor: not ready" in out


def test_doctor_blocks_side_effect_rollouts_with_handle_style_bot_user_id(capsys):
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="linear_only",
            slack=SlackConfig(bot_user_ids=("@monica",), allowed_channels=("C123",)),
            linear=LinearConfig(team_id="team-id"),
        ),
        environ={
            "SLACK_BOT_TOKEN": "xoxb-token",
            "SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        gateway_status={
            "gateway_state": "running",
            "pid": 53960,
            "start_time": 100.0,
            "active_agents": 0,
            "updated_at": "100.0",
        },
        now=160.0,
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert (
        "FAIL: slack.bot_user_ids must contain Slack mention user IDs like U012ABCDEF, "
        "not handles like @monica"
    ) in out
    assert "Monica doctor: not ready" in out


def test_doctor_blocks_side_effect_rollouts_with_malformed_bot_user_id(capsys):
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="linear_only",
            slack=SlackConfig(bot_user_ids=("monica",), allowed_channels=("C123",)),
            linear=LinearConfig(team_id="team-id"),
        ),
        environ={
            "SLACK_BOT_TOKEN": "xoxb-token",
            "SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert (
        "FAIL: slack.bot_user_ids must contain Slack mention user IDs like U012ABCDEF, "
        "not invalid values like monica"
    ) in out
    assert "Monica doctor: not ready" in out


def test_doctor_blocks_side_effect_rollouts_without_bot_user_ids(capsys):
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="linear_only",
            slack=SlackConfig(allowed_channels=("C123",)),
            linear=LinearConfig(team_id="team-id"),
        ),
        environ={
            "SLACK_BOT_TOKEN": "xoxb-token",
            "SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "FAIL: slack.bot_user_ids is empty; configure Monica's Slack mention user ID" in out
    assert "Monica doctor: not ready" in out


def test_doctor_fails_when_provided_slack_scopes_are_missing_required_post_scope(capsys):
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="dry_run",
            slack=SlackConfig(bot_user_ids=("U_MONICA",), allowed_channels=("C123",)),
        ),
        environ={
            "SLACK_BOT_TOKEN": "xoxb-token",
            "SLACK_BOT_SCOPES": "app_mentions:read,channels:history",
        },
        which=lambda name: f"/usr/bin/{name}",
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "FAIL: SLACK_BOT_SCOPES is missing required scope: chat:write" in out
    assert "Monica doctor: not ready" in out


def test_doctor_blocks_approved_pr_without_repo_verification_and_tools(capsys):
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            slack=SlackConfig(bot_user_ids=("BMONICA",), allowed_channels=("C123",)),
            linear=LinearConfig(team_id="team-id"),
        ),
        environ={"SLACK_BOT_TOKEN": "xoxb-token", "LINEAR_API_KEY": "lin-key"},
        which=lambda name: None,
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "FAIL: repo.url is missing" in out
    assert "FAIL: verification.commands is empty" in out
    assert "FAIL: git executable was not found" in out
    assert "FAIL: gh executable was not found" in out
    assert "FAIL: codex executable was not found" in out


def test_doctor_blocks_approved_pr_without_configured_approvers(capsys):
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            slack=SlackConfig(bot_user_ids=("U_MONICA",), allowed_channels=("C123",)),
            linear=LinearConfig(team_id="team-id"),
            repo=RepoConfig(url="git@github.com:acme/mobile.git"),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                commands=("npm run monica:proof",),
                setup_commands=("npm run monica:seed-auth",),
                platform_order=("ios", "android"),
            ),
        ),
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "GITHUB_TOKEN": "gh-token",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        gateway_status={
            "gateway_state": "running",
            "pid": 53960,
            "start_time": 100.0,
            "active_agents": 0,
            "updated_at": "100.0",
        },
        now=160.0,
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "FAIL: slack.approver_user_ids is empty; configure at least one Monica code approver" in out
    assert "Monica doctor: not ready" in out


def test_doctor_blocks_approved_pr_with_unsafe_repo_local_name(capsys):
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            slack=SlackConfig(
                bot_user_ids=("U_MONICA",),
                allowed_channels=("C123",),
                approver_user_ids=("U_APPROVER",),
            ),
            linear=LinearConfig(team_id="team-id"),
            repo=RepoConfig(url="git@github.com:acme/mobile.git", local_name="../outside-runtime"),
            verification=VerificationConfig(commands=("npm test",)),
        ),
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "GITHUB_TOKEN": "gh-token",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "FAIL: repo.local_name must be a simple directory name." in out
    assert "Monica doctor: not ready" in out


def test_doctor_blocks_approved_pr_with_chandler_repo_local_name(capsys):
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            slack=SlackConfig(
                bot_user_ids=("U_MONICA",),
                allowed_channels=("C123",),
                approver_user_ids=("U_APPROVER",),
            ),
            linear=LinearConfig(team_id="team-id"),
            repo=RepoConfig(url="git@github.com:acme/mobile.git", local_name="chandler"),
            verification=VerificationConfig(commands=("npm test",)),
        ),
        environ={
            "SLACK_BOT_TOKEN": "xoxb-token",
            "SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "GITHUB_TOKEN": "gh-token",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "FAIL: repo.local_name must not point at a Chandler directory." in out
    assert "Monica doctor: not ready" in out


def test_doctor_blocks_approved_pr_with_unsafe_repo_branch_prefix(capsys):
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            slack=SlackConfig(
                bot_user_ids=("U_MONICA",),
                allowed_channels=("C123",),
                approver_user_ids=("U_APPROVER",),
            ),
            linear=LinearConfig(team_id="team-id"),
            repo=RepoConfig(
                url="git@github.com:acme/mobile.git",
                branch_prefix="../chandler",
            ),
            verification=VerificationConfig(commands=("npm test",)),
        ),
        environ={
            "SLACK_BOT_TOKEN": "xoxb-token",
            "SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "GITHUB_TOKEN": "gh-token",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "FAIL: repo.branch_prefix must be a safe git branch prefix." in out
    assert "Monica doctor: not ready" in out


def test_doctor_blocks_approved_pr_with_chandler_repo_branch_prefix(capsys):
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            slack=SlackConfig(
                bot_user_ids=("U_MONICA",),
                allowed_channels=("C123",),
                approver_user_ids=("U_APPROVER",),
            ),
            linear=LinearConfig(team_id="team-id"),
            repo=RepoConfig(
                url="git@github.com:acme/mobile.git",
                branch_prefix="chandler",
            ),
            verification=VerificationConfig(commands=("npm test",)),
        ),
        environ={
            "SLACK_BOT_TOKEN": "xoxb-token",
            "SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "GITHUB_TOKEN": "gh-token",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "FAIL: repo.branch_prefix must not point at Chandler." in out
    assert "Monica doctor: not ready" in out


def test_doctor_blocks_approved_pr_with_unsafe_repo_default_branch(capsys):
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            slack=SlackConfig(
                bot_user_ids=("U_MONICA",),
                allowed_channels=("C123",),
                approver_user_ids=("U_APPROVER",),
            ),
            linear=LinearConfig(team_id="team-id"),
            repo=RepoConfig(
                url="git@github.com:acme/mobile.git",
                default_branch="../main",
            ),
            verification=VerificationConfig(commands=("npm test",)),
        ),
        environ={
            "SLACK_BOT_TOKEN": "xoxb-token",
            "SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "GITHUB_TOKEN": "gh-token",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "FAIL: repo.default_branch must be a safe git branch name." in out
    assert "Monica doctor: not ready" in out


def test_doctor_blocks_approved_pr_with_handle_style_approver(capsys):
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            slack=SlackConfig(
                bot_user_ids=("U_MONICA",),
                allowed_channels=("C123",),
                approver_user_ids=("@ritik",),
            ),
            linear=LinearConfig(team_id="team-id"),
            repo=RepoConfig(url="git@github.com:acme/mobile.git"),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                commands=("npm run monica:proof",),
                setup_commands=("npm run monica:seed-auth",),
                platform_order=("ios", "android"),
            ),
        ),
        environ={
            "SLACK_BOT_TOKEN": "xoxb-token",
            "SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "GITHUB_TOKEN": "gh-token",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "FAIL: slack.approver_user_ids must contain Slack user IDs like U123, not handles like @ritik" in out
    assert "Monica doctor: not ready" in out


def test_doctor_blocks_approved_pr_with_malformed_approver_user_id(capsys):
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            slack=SlackConfig(
                bot_user_ids=("U_MONICA",),
                allowed_channels=("C123",),
                approver_user_ids=("ritik",),
            ),
            linear=LinearConfig(team_id="team-id"),
            repo=RepoConfig(url="git@github.com:acme/mobile.git"),
            verification=VerificationConfig(commands=("npm test",)),
        ),
        environ={
            "SLACK_BOT_TOKEN": "xoxb-token",
            "SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "GITHUB_TOKEN": "gh-token",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert (
        "FAIL: slack.approver_user_ids must contain Slack user IDs like U123, "
        "not invalid values like ritik"
    ) in out
    assert "Monica doctor: not ready" in out


def test_doctor_blocks_approved_pr_with_interactive_codex_approval_policy(capsys):
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            slack=SlackConfig(
                bot_user_ids=("U_MONICA",),
                allowed_channels=("C123",),
                approver_user_ids=("U_APPROVER",),
            ),
            linear=LinearConfig(team_id="team-id"),
            repo=RepoConfig(url="git@github.com:acme/mobile.git"),
            verification=VerificationConfig(commands=("npm test",)),
            worker=WorkerConfig(codex_approval_policy="on-request"),
        ),
        environ={
            "SLACK_BOT_TOKEN": "xoxb-token",
            "SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "GITHUB_TOKEN": "gh-token",
        },
        which=lambda name: f"/usr/bin/{name}",
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "FAIL: worker.codex_approval_policy must be `never` for approved_pr codex_cli runs" in out
    assert "Monica doctor: not ready" in out


def test_doctor_blocks_approved_pr_with_non_workspace_codex_sandbox(capsys):
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            slack=SlackConfig(
                bot_user_ids=("U_MONICA",),
                allowed_channels=("C123",),
                approver_user_ids=("U_APPROVER",),
            ),
            linear=LinearConfig(team_id="team-id"),
            repo=RepoConfig(url="git@github.com:acme/mobile.git"),
            verification=VerificationConfig(commands=("npm test",)),
            worker=WorkerConfig(codex_sandbox="danger-full-access"),
        ),
        environ={
            "SLACK_BOT_TOKEN": "xoxb-token",
            "SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "GITHUB_TOKEN": "gh-token",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "FAIL: worker.codex_sandbox must be `workspace-write` for approved_pr codex_cli runs" in out
    assert "Monica doctor: not ready" in out


def test_doctor_blocks_internal_worker_with_chandler_session_prefix(capsys):
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            slack=SlackConfig(
                bot_user_ids=("U_MONICA",),
                allowed_channels=("C123",),
                approver_user_ids=("U_APPROVER",),
            ),
            linear=LinearConfig(team_id="team-id"),
            repo=RepoConfig(url="git@github.com:acme/mobile.git"),
            runtime=RuntimeConfig(worker_session_prefix="chandler"),
            verification=VerificationConfig(commands=("npm test",)),
            worker=WorkerConfig(backend="internal_agent"),
        ),
        environ={
            "SLACK_BOT_TOKEN": "xoxb-token",
            "SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "GITHUB_TOKEN": "gh-token",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "FAIL: runtime.worker_session_prefix must include `monica` to keep worker sessions segregated" in out
    assert "Monica doctor: not ready" in out


def test_doctor_blocks_chandler_runtime_home_subdir(capsys):
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            slack=SlackConfig(
                bot_user_ids=("U_MONICA",),
                allowed_channels=("C123",),
                approver_user_ids=("U_APPROVER",),
            ),
            linear=LinearConfig(team_id="team-id"),
            repo=RepoConfig(url="git@github.com:acme/mobile.git"),
            runtime=RuntimeConfig(home_subdir="agents/chandler"),
            verification=VerificationConfig(commands=("npm test",)),
        ),
        environ={
            "SLACK_BOT_TOKEN": "xoxb-token",
            "SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "GITHUB_TOKEN": "gh-token",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "FAIL: mobile_bug_agent.runtime.home_subdir must not point at a Chandler runtime path" in out
    assert "Monica doctor: not ready" in out


def test_doctor_blocks_approved_pr_when_linear_creation_is_disabled(capsys):
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            slack=SlackConfig(
                bot_user_ids=("U_MONICA",),
                allowed_channels=("C123",),
                approver_user_ids=("U_APPROVER",),
            ),
            loop=LoopConfig(create_linear=False),
            linear=LinearConfig(team_id="team-id"),
            repo=RepoConfig(url="git@github.com:acme/mobile.git"),
            verification=VerificationConfig(commands=("npm test",)),
        ),
        environ={
            "SLACK_BOT_TOKEN": "xoxb-token",
            "SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "GITHUB_TOKEN": "gh-token",
        },
        which=lambda name: f"/usr/bin/{name}",
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "FAIL: loop.create_linear must be true in approved_pr mode" in out
    assert "Monica doctor: not ready" in out


def test_doctor_blocks_approved_pr_when_fix_approval_gate_is_disabled(capsys):
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            slack=SlackConfig(
                bot_user_ids=("U_MONICA",),
                allowed_channels=("C123",),
                approver_user_ids=("U_APPROVER",),
            ),
            loop=LoopConfig(require_fix_approval=False),
            linear=LinearConfig(team_id="team-id"),
            repo=RepoConfig(url="git@github.com:acme/mobile.git"),
            verification=VerificationConfig(commands=("npm test",)),
        ),
        environ={
            "SLACK_BOT_TOKEN": "xoxb-token",
            "SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "GITHUB_TOKEN": "gh-token",
        },
        which=lambda name: f"/usr/bin/{name}",
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "FAIL: loop.require_fix_approval must be true in approved_pr mode" in out
    assert "Monica doctor: not ready" in out


def test_doctor_blocks_side_effect_rollouts_without_allowed_channels(capsys):
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="linear_only",
            slack=SlackConfig(bot_user_ids=("U_MONICA",)),
            linear=LinearConfig(team_id="team-id"),
        ),
        environ={
            "SLACK_BOT_TOKEN": "xoxb-token",
            "SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
        },
        which=lambda name: f"/usr/bin/{name}",
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "FAIL: slack.allowed_channels is empty; configure the Slack channels Monica may act in" in out
    assert "Monica doctor: not ready" in out


def test_doctor_blocks_dry_run_with_slack_channel_names(capsys):
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="dry_run",
            slack=SlackConfig(bot_user_ids=("U_MONICA",), allowed_channels=("#mobile-bugs",)),
        ),
        environ={"SLACK_BOT_TOKEN": "xoxb-token"},
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "FAIL: slack.allowed_channels must contain Slack channel IDs like C123 or G123, not names like #mobile-bugs" in out
    assert "Monica doctor: not ready" in out


def test_doctor_blocks_side_effect_rollouts_with_slack_channel_names(capsys):
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="linear_only",
            slack=SlackConfig(bot_user_ids=("U_MONICA",), allowed_channels=("#mobile-bugs",)),
            linear=LinearConfig(team_id="team-id"),
        ),
        environ={
            "SLACK_BOT_TOKEN": "xoxb-token",
            "SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
        },
        which=lambda name: f"/usr/bin/{name}",
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "FAIL: slack.allowed_channels must contain Slack channel IDs like C123 or G123, not names like #mobile-bugs" in out
    assert "Monica doctor: not ready" in out


def test_doctor_blocks_linear_only_when_linear_creation_is_disabled(capsys):
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="linear_only",
            slack=SlackConfig(bot_user_ids=("U_MONICA",), allowed_channels=("C123",)),
            loop=LoopConfig(create_linear=False),
            linear=LinearConfig(team_id="team-id"),
        ),
        environ={
            "SLACK_BOT_TOKEN": "xoxb-token",
            "SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
        },
        which=lambda name: f"/usr/bin/{name}",
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "FAIL: loop.create_linear must be true in linear_only mode" in out
    assert "Monica doctor: not ready" in out


def test_doctor_accepts_approved_pr_when_required_setup_exists(capsys):
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="approved_pr",
            slack=SlackConfig(
                bot_user_ids=("U_MONICA",),
                allowed_channels=("C123",),
                approver_user_ids=("U_APPROVER",),
            ),
            linear=LinearConfig(team_id="team-id"),
            repo=RepoConfig(url="git@github.com:acme/mobile.git"),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                commands=("npm run monica:proof",),
                setup_commands=("npm run monica:seed-auth",),
                required_env_keys=("MONICA_TEST_LOGIN_TOKEN",),
                platform_order=("ios", "android"),
            ),
        ),
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
            "GITHUB_TOKEN": "gh-token",
            "MONICA_TEST_LOGIN_TOKEN": "token",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
        gateway_status={
            "gateway_state": "running",
            "pid": 53960,
            "start_time": 100.0,
            "active_agents": 0,
            "updated_at": "100.0",
        },
        now=160.0,
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Monica doctor: ready" in out


def test_doctor_accepts_local_fix_only_without_github_push_or_pr_tools(capsys):
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="local_fix_only",
            slack=SlackConfig(
                bot_user_ids=("U_MONICA",),
                allowed_channels=("D123MONICA",),
                approver_user_ids=("U_APPROVER",),
            ),
            linear=LinearConfig(team_id="team-id"),
            repo=RepoConfig(url="git@github.com:acme/mobile.git"),
            verification=VerificationConfig(commands=("npm test",)),
        ),
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
        },
        which=lambda name: None if name == "gh" else f"/usr/bin/{name}",
        module_available=lambda name: True,
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "gh executable was not found" not in out
    assert "GITHUB_TOKEN is missing" not in out
    assert "Monica doctor: ready" in out


def test_doctor_warns_when_required_proof_has_no_commands(capsys):
    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="local_fix_only",
            slack=SlackConfig(
                bot_user_ids=("U_MONICA",),
                allowed_channels=("D123MONICA",),
                approver_user_ids=("U_APPROVER",),
            ),
            linear=LinearConfig(team_id="team-id"),
            repo=RepoConfig(url="git@github.com:acme/mobile.git"),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(enabled=True, required_for_done=True),
        ),
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
        },
        which=lambda name: f"/usr/bin/{name}",
        module_available=lambda name: True,
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "WARN: proof.commands is empty; Monica will block at proof after verification" in out
    assert "Monica doctor: ready" in out


def test_doctor_warns_when_required_proof_tooling_is_missing(capsys):
    missing = {"xcrun", "xcodebuild", "adb", "emulator"}

    exit_code = run_doctor_command(
        config=MonicaConfig(
            enabled=True,
            rollout_mode="local_fix_only",
            slack=SlackConfig(
                bot_user_ids=("U_MONICA",),
                allowed_channels=("D123MONICA",),
                approver_user_ids=("U_APPROVER",),
            ),
            linear=LinearConfig(team_id="team-id"),
            repo=RepoConfig(url="git@github.com:acme/mobile.git"),
            verification=VerificationConfig(commands=("npm test",)),
            proof=ProofConfig(
                enabled=True,
                required_for_done=True,
                commands=("npm run monica:proof",),
                platform_order=("ios", "android"),
            ),
        ),
        environ={
            "MONICA_SLACK_BOT_TOKEN": "xoxb-token",
            "MONICA_SLACK_APP_TOKEN": "xapp-token",
            "LINEAR_API_KEY": "lin-key",
        },
        which=lambda name: None if name in missing else f"/usr/bin/{name}",
        module_available=lambda name: True,
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert (
        "WARN: iOS proof is configured but xcrun/simctl or xcodebuild was not found; "
        "Monica will block at proof until a simulator is available"
    ) in out
    assert (
        "WARN: Android proof is configured but adb or emulator was not found; "
        "Monica will block at proof until an emulator is available"
    ) in out
    assert "Monica doctor: ready" in out
