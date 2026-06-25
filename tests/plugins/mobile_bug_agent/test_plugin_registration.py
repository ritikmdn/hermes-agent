from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

import yaml

import plugins.mobile_bug_agent as monica_plugin
from gateway.config import Platform
from plugins.mobile_bug_agent import register
from plugins.mobile_bug_agent.config import MonicaConfig, RuntimeConfig, SlackConfig
from plugins.mobile_bug_agent.state import MonicaState

SAFE_RUNTIME_UNAVAILABLE_TEXT = (
    "Monica could not start because her runtime is not configured correctly. "
    "Run `hermes mobile-bug-agent doctor` on the host for details."
)


class FakeContext:
    def __init__(self) -> None:
        self.cli_commands: list[dict] = []
        self.hooks: list[str] = []

    def register_cli_command(self, **kwargs):
        self.cli_commands.append(kwargs)

    def register_hook(self, name, fn):
        self.hooks.append(name)


def test_plugin_registers_cli_and_gateway_hook():
    ctx = FakeContext()

    register(ctx)

    assert [cmd["name"] for cmd in ctx.cli_commands] == ["mobile-bug-agent"]
    assert ctx.cli_commands[0]["help"] == "Inspect and operate Monica mobile bug loops"
    assert ctx.hooks == ["pre_gateway_dispatch", "gateway_startup", "pre_update", "post_update"]


def test_plugin_manifest_points_to_operator_docs():
    manifest_path = Path(__file__).resolve().parents[3] / "plugins" / "mobile_bug_agent" / "plugin.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))

    assert "docs/monica-agent.md" in manifest["description"]


def test_bundled_plugin_loads_when_enabled(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["mobile-bug-agent"]}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    from hermes_cli.plugins import PluginManager

    manager = PluginManager()
    manager.discover_and_load()

    loaded = manager._plugins["mobile-bug-agent"]
    assert loaded.enabled
    assert loaded.manifest.source == "bundled"
    assert "pre_gateway_dispatch" in loaded.hooks_registered
    assert "gateway_startup" in loaded.hooks_registered
    assert "pre_update" in loaded.hooks_registered
    assert "mobile-bug-agent" in manager._cli_commands


def test_pre_update_blocks_when_monica_has_active_runtime_sync_run(tmp_path, monkeypatch):
    runtime = tmp_path / "monica-runtime"
    state = MonicaState.open(runtime / "state.sqlite")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong",
    )
    state.update_run(run.id, status="proofing", linear_identifier="MOB-123")
    monkeypatch.setattr(
        monica_plugin,
        "load_monica_config",
        lambda: MonicaConfig(enabled=True, runtime=RuntimeConfig(home_subdir=str(runtime))),
    )

    result = monica_plugin._on_pre_update()

    assert result == {
        "action": "block",
        "message": (
            "Monica is active (1 run: MOB-123/proofing). "
            "Wait until Monica is idle before running `hermes update`."
        ),
    }


def test_pre_update_allows_hermes_update_when_monica_is_idle(tmp_path, monkeypatch):
    runtime = tmp_path / "monica-runtime"
    state = MonicaState.open(runtime / "state.sqlite")
    done = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong",
    )
    state.update_run(done.id, status="done", linear_identifier="MOB-123")
    blocked = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000001.000100",
        message_ts="1710000001.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP layout is off",
    )
    state.update_run(blocked.id, status="blocked", linear_identifier="MOB-456")
    monkeypatch.setattr(
        monica_plugin,
        "load_monica_config",
        lambda: MonicaConfig(enabled=True, runtime=RuntimeConfig(home_subdir=str(runtime))),
    )
    monkeypatch.setattr(monica_plugin, "_current_hermes_commit", lambda _root="": "dead1234")
    monkeypatch.setattr(monica_plugin, "_utc_now_iso", lambda: "2026-06-15T10:00:00Z")

    result = monica_plugin._on_pre_update()

    assert result is None
    lease = state.current_runtime_sync_lease()
    assert lease is not None
    assert lease.pre_update_commit == "dead1234"
    assert lease.project_root


def test_pre_update_blocks_when_runtime_sync_lease_is_active(tmp_path, monkeypatch):
    runtime = tmp_path / "monica-runtime"
    state = MonicaState.open(runtime / "state.sqlite")
    state.try_acquire_runtime_sync_lease(
        owner_id="other-update",
        owner_pid=111,
        owner_host="host",
        project_root=str(tmp_path),
        pre_update_commit="dead1234",
        started_at="2026-06-15T10:00:00Z",
        expires_at="2026-06-15T10:15:00Z",
    )
    monkeypatch.setattr(
        monica_plugin,
        "load_monica_config",
        lambda: MonicaConfig(enabled=True, runtime=RuntimeConfig(home_subdir=str(runtime))),
    )
    monkeypatch.setattr(monica_plugin, "_current_hermes_commit", lambda _root="": "abc12345")
    monkeypatch.setattr(monica_plugin, "_utc_now_iso", lambda: "2026-06-15T10:05:00Z")

    result = monica_plugin._on_pre_update(project_root=str(tmp_path))

    assert result == {
        "action": "block",
        "message": (
            "Monica runtime sync is already in progress. "
            "Wait for the current Hermes update to finish before starting another update."
        ),
    }


def test_pre_update_replaces_expired_runtime_sync_lease(tmp_path, monkeypatch):
    runtime = tmp_path / "monica-runtime"
    state = MonicaState.open(runtime / "state.sqlite")
    old_lease, reason = state.try_acquire_runtime_sync_lease(
        owner_id="interrupted-update",
        owner_pid=111,
        owner_host="host",
        project_root=str(tmp_path),
        pre_update_commit="dead1234",
        started_at="2026-06-15T10:00:00Z",
        expires_at="2026-06-15T10:15:00Z",
    )
    assert old_lease is not None
    assert reason == ""
    monkeypatch.setattr(
        monica_plugin,
        "load_monica_config",
        lambda: MonicaConfig(enabled=True, runtime=RuntimeConfig(home_subdir=str(runtime))),
    )
    monkeypatch.setattr(monica_plugin, "_current_hermes_commit", lambda _root="": "abc12345")
    monkeypatch.setattr(monica_plugin, "_utc_now_iso", lambda: "2026-06-15T10:20:00Z")

    result = monica_plugin._on_pre_update(project_root=str(tmp_path))

    assert result is None
    lease = state.current_runtime_sync_lease()
    assert lease is not None
    assert lease.lease_id != old_lease.lease_id
    assert lease.pre_update_commit == "abc12345"
    assert lease.started_at == "2026-06-15T10:20:00Z"
    assert lease.expires_at == "2026-06-15T10:50:00Z"
    health = state.runtime_sync_health()
    assert health["last_sync_status"] == "failed"
    assert health["last_sync_failure_reason"] == "lease_expired"
    assert health["last_sync_lease_id"] == old_lease.lease_id


def test_pre_update_blocks_when_monica_idle_check_is_unavailable(monkeypatch):
    def fail_config():
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(monica_plugin, "load_monica_config", fail_config)

    result = monica_plugin._on_pre_update()

    assert result == {
        "action": "block",
        "message": (
            "Monica idle state is unavailable, so `hermes update` was not started. "
            "Run `hermes mobile-bug-agent doctor` on the host before updating Hermes."
        ),
    }


def test_post_update_records_last_runtime_sync_when_monica_is_idle(tmp_path, monkeypatch):
    runtime = tmp_path / "monica-runtime"
    monkeypatch.setattr(
        monica_plugin,
        "load_monica_config",
        lambda: MonicaConfig(enabled=True, runtime=RuntimeConfig(home_subdir=str(runtime))),
    )
    monkeypatch.setattr(monica_plugin, "_current_hermes_commit", lambda _root: "abc12345")
    monkeypatch.setattr(monica_plugin, "_utc_now_iso", lambda: "2026-06-13T10:00:00Z")

    pre = monica_plugin._on_pre_update(project_root=str(tmp_path))
    assert pre is None
    result = monica_plugin._on_post_update(project_root=str(tmp_path))

    state = MonicaState.open(runtime / "state.sqlite")
    assert result is not None
    assert result["action"] == "recorded"
    assert result["last_synced_commit"] == "abc12345"
    assert result["last_synced_at"] == "2026-06-13T10:00:00Z"
    assert result["last_sync_status"] == "recorded"
    assert state.runtime_sync_metadata() == {
        "last_synced_commit": "abc12345",
        "last_synced_at": "2026-06-13T10:00:00Z",
    }
    assert state.runtime_sync_health()["last_sync_status"] == "recorded"
    assert state.current_runtime_sync_lease() is None


def test_post_update_skips_runtime_sync_when_monica_becomes_active(tmp_path, monkeypatch):
    runtime = tmp_path / "monica-runtime"
    state = MonicaState.open(runtime / "state.sqlite")
    state.record_runtime_sync(commit="dead1234", synced_at="2026-06-12T10:00:00Z")
    run = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong",
    )
    state.update_run(run.id, status="opening_pr", linear_identifier="MOB-123")
    monkeypatch.setattr(
        monica_plugin,
        "load_monica_config",
        lambda: MonicaConfig(enabled=True, runtime=RuntimeConfig(home_subdir=str(runtime))),
    )
    monkeypatch.setattr(monica_plugin, "_current_hermes_commit", lambda _root: "abc12345")
    monkeypatch.setattr(monica_plugin, "_utc_now_iso", lambda: "2026-06-13T10:00:00Z")

    result = monica_plugin._on_post_update(project_root=str(tmp_path))

    assert result == {
        "action": "skipped",
        "reason": "monica_active",
        "active_run_count": "1",
        "active_runs": "MOB-123/opening_pr",
    }
    assert state.runtime_sync_metadata() == {
        "last_synced_commit": "dead1234",
        "last_synced_at": "2026-06-12T10:00:00Z",
    }


def test_post_update_does_not_record_runtime_sync_without_commit(tmp_path, monkeypatch):
    runtime = tmp_path / "monica-runtime"
    monkeypatch.setattr(
        monica_plugin,
        "load_monica_config",
        lambda: MonicaConfig(enabled=True, runtime=RuntimeConfig(home_subdir=str(runtime))),
    )
    commits = iter(["dead1234", ""])
    monkeypatch.setattr(monica_plugin, "_current_hermes_commit", lambda _root: next(commits))
    monkeypatch.setattr(monica_plugin, "_utc_now_iso", lambda: "2026-06-13T10:00:00Z")

    pre = monica_plugin._on_pre_update(project_root=str(tmp_path))
    assert pre is None
    result = monica_plugin._on_post_update(project_root=str(tmp_path))

    state = MonicaState.open(runtime / "state.sqlite")
    assert result == {"action": "skipped", "reason": "commit_unavailable"}
    assert state.runtime_sync_metadata() == {
        "last_synced_commit": "",
        "last_synced_at": "",
    }
    assert state.runtime_sync_health()["last_sync_status"] == "failed"
    assert state.runtime_sync_health()["last_sync_failure_reason"] == "commit_unavailable"
    assert state.current_runtime_sync_lease() is None


def test_post_update_does_not_record_runtime_sync_with_invalid_commit(tmp_path, monkeypatch):
    runtime = tmp_path / "monica-runtime"
    monkeypatch.setattr(
        monica_plugin,
        "load_monica_config",
        lambda: MonicaConfig(enabled=True, runtime=RuntimeConfig(home_subdir=str(runtime))),
    )
    monkeypatch.setattr(monica_plugin, "_current_hermes_commit", lambda _root: "not-a-sha")
    monkeypatch.setattr(monica_plugin, "_utc_now_iso", lambda: "2026-06-13T10:00:00Z")

    result = monica_plugin._on_post_update(project_root=str(tmp_path))

    state = MonicaState.open(runtime / "state.sqlite")
    assert result == {"action": "skipped", "reason": "commit_unavailable"}
    assert state.runtime_sync_metadata() == {
        "last_synced_commit": "",
        "last_synced_at": "",
    }


def test_gateway_startup_recovery_relaunches_safe_pending_runs(tmp_path):
    state = MonicaState.open(tmp_path / "state.sqlite")
    queued = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong",
    )
    approved = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000001.000100",
        message_ts="1710000001.000200",
        user_id="U_TAGGER",
        request_text="marketplace Search copy is wrong",
    )
    state.update_run(approved.id, status="approved", approved_by_user_id="U_APPROVER")
    waiting = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000002.000100",
        message_ts="1710000002.000200",
        user_id="U_TAGGER",
        request_text="marketplace offer card copy is wrong",
    )
    state.update_run(waiting.id, status="awaiting_fix_approval")
    launched: list[str] = []

    result = monica_plugin._recover_pending_runs_on_gateway_startup(
        state=state,
        loop_launcher=launched.append,
    )

    assert result == {"launched": 2}
    assert launched == [approved.id, queued.id]


def test_gateway_startup_recovery_reaps_stale_loop_lease_before_launch(tmp_path):
    state = MonicaState.open(tmp_path / "state.sqlite")
    queued = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong",
    )
    lease, reason = state.acquire_loop_lease(
        queued.id,
        owner_id="old-gateway",
        acquired_at=1781520000.0,
        ttl_seconds=1,
    )
    assert lease is not None
    assert reason == ""
    launched: list[str] = []

    result = monica_plugin._recover_pending_runs_on_gateway_startup(
        state=state,
        loop_launcher=launched.append,
        now=1781520300.0,
    )

    assert result == {"launched": 1}
    assert launched == [queued.id]
    assert state.current_loop_lease(queued.id) is None


def test_gateway_startup_recovery_syncs_pending_approvals(tmp_path):
    state = MonicaState.open(tmp_path / "state.sqlite")
    waiting = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong",
    )
    state.update_run(waiting.id, status="awaiting_fix_approval")
    calls: list[dict] = []

    result = monica_plugin._recover_on_gateway_startup(
        config=MonicaConfig(enabled=True, slack=SlackConfig(bot_user_ids=("U_MONICA",), approver_user_ids=("U_APPROVER",))),
        state=state,
        loop_launcher=lambda _run_id: None,
        sync_approvals=lambda **kwargs: calls.append(kwargs) or 0,
    )

    assert result["approval_sync_exit_code"] == 0
    assert calls
    assert calls[0]["state"] is state
    assert calls[0]["limit"] >= 20


def test_gateway_startup_recovery_reaps_expired_runtime_sync_lease(tmp_path):
    state = MonicaState.open(tmp_path / "state.sqlite")
    old_lease, reason = state.try_acquire_runtime_sync_lease(
        owner_id="interrupted-update",
        owner_pid=111,
        owner_host="host",
        project_root=str(tmp_path),
        pre_update_commit="dead1234",
        started_at="2026-06-15T10:00:00Z",
        expires_at="2026-06-15T10:15:00Z",
    )
    assert old_lease is not None
    assert reason == ""
    queued = state.create_run(
        platform="slack",
        channel_id="C_MOBILE",
        thread_ts="1710000000.000100",
        message_ts="1710000000.000200",
        user_id="U_TAGGER",
        request_text="marketplace PDP copy is wrong",
    )
    launched: list[str] = []

    result = monica_plugin._recover_on_gateway_startup(
        config=MonicaConfig(enabled=True),
        state=state,
        loop_launcher=launched.append,
        now="2026-06-15T10:16:00Z",
    )

    assert result == {"launched": 1}
    assert launched == [queued.id]
    assert state.current_runtime_sync_lease() is None
    health = state.runtime_sync_health()
    assert health["last_sync_status"] == "failed"
    assert health["last_sync_failure_reason"] == "lease_expired"
    assert health["last_sync_lease_id"] == old_lease.lease_id


def test_bundled_plugin_is_opt_in(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": []}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    from hermes_cli.plugins import PluginManager

    manager = PluginManager()
    manager.discover_and_load()

    loaded = manager._plugins["mobile-bug-agent"]
    assert not loaded.enabled
    assert "not enabled" in str(loaded.error)


def test_pre_gateway_dispatch_reports_runtime_bootstrap_failure(monkeypatch):
    monica_plugin._runtime.cache_clear()
    monkeypatch.setattr(monica_plugin, "load_monica_config", lambda: MonicaConfig(enabled=True))
    monkeypatch.setattr(
        monica_plugin,
        "runtime_root",
        lambda config: (_ for _ in ()).throw(ValueError("bad Monica runtime root")),
    )
    event = SimpleNamespace(
        source=SimpleNamespace(platform=Platform.SLACK),
        raw_message={"type": "app_mention", "text": "<@U_MONICA> checkout crash"},
    )

    try:
        result = monica_plugin._on_pre_gateway_dispatch(event)
    finally:
        monica_plugin._runtime.cache_clear()

    assert result == {
        "action": "skip_reply",
        "reason": "monica_runtime_unavailable",
        "text": SAFE_RUNTIME_UNAVAILABLE_TEXT,
    }


def test_pre_gateway_dispatch_runtime_failure_does_not_leak_raw_exception_details(monkeypatch):
    monica_plugin._runtime.cache_clear()
    monkeypatch.setattr(monica_plugin, "load_monica_config", lambda: MonicaConfig(enabled=True))
    monkeypatch.setattr(
        monica_plugin,
        "runtime_root",
        lambda config: (_ for _ in ()).throw(ValueError("/Users/ritik/.hermes/secrets")),
    )
    event = SimpleNamespace(
        source=SimpleNamespace(platform=Platform.SLACK),
        raw_message={"type": "app_mention", "text": "<@U_MONICA> checkout crash"},
    )

    try:
        result = monica_plugin._on_pre_gateway_dispatch(event)
    finally:
        monica_plugin._runtime.cache_clear()

    assert result is not None
    assert result["action"] == "skip_reply"
    assert result["reason"] == "monica_runtime_unavailable"
    assert result["text"] == SAFE_RUNTIME_UNAVAILABLE_TEXT
    assert "/Users/ritik/.hermes/secrets" not in result["text"]


def test_pre_gateway_dispatch_runtime_failure_catches_configured_message_mention(monkeypatch):
    monica_plugin._runtime.cache_clear()
    monkeypatch.setattr(
        monica_plugin,
        "load_monica_config",
        lambda: MonicaConfig(enabled=True, slack=SlackConfig(bot_user_ids=("U_MONICA",))),
    )
    monkeypatch.setattr(
        monica_plugin,
        "runtime_root",
        lambda config: (_ for _ in ()).throw(ValueError("bad Monica runtime root")),
    )
    event = SimpleNamespace(
        source=SimpleNamespace(platform=Platform.SLACK),
        raw_message={"type": "message", "text": "<@U_MONICA> checkout crash"},
    )

    try:
        result = monica_plugin._on_pre_gateway_dispatch(event)
    finally:
        monica_plugin._runtime.cache_clear()

    assert result == {
        "action": "skip_reply",
        "reason": "monica_runtime_unavailable",
        "text": SAFE_RUNTIME_UNAVAILABLE_TEXT,
    }


def test_pre_gateway_dispatch_runtime_failure_ignores_other_app_mentions_when_configured(
    monkeypatch,
):
    monica_plugin._runtime.cache_clear()
    monkeypatch.setattr(
        monica_plugin,
        "load_monica_config",
        lambda: MonicaConfig(enabled=True, slack=SlackConfig(bot_user_ids=("U_MONICA",))),
    )
    monkeypatch.setattr(
        monica_plugin,
        "runtime_root",
        lambda config: (_ for _ in ()).throw(ValueError("bad Monica runtime root")),
    )
    event = SimpleNamespace(
        source=SimpleNamespace(platform=Platform.SLACK),
        raw_message={"type": "app_mention", "text": "<@U_CHANDLER> checkout crash"},
    )

    try:
        result = monica_plugin._on_pre_gateway_dispatch(event)
    finally:
        monica_plugin._runtime.cache_clear()

    assert result is None


def test_pre_gateway_dispatch_runtime_failure_catches_punctuated_message_mention(monkeypatch):
    monica_plugin._runtime.cache_clear()
    monkeypatch.setattr(
        monica_plugin,
        "load_monica_config",
        lambda: MonicaConfig(enabled=True, slack=SlackConfig(bot_user_ids=("U_MONICA",))),
    )
    monkeypatch.setattr(
        monica_plugin,
        "runtime_root",
        lambda config: (_ for _ in ()).throw(ValueError("bad Monica runtime root")),
    )
    event = SimpleNamespace(
        source=SimpleNamespace(platform=Platform.SLACK),
        raw_message={"type": "message", "text": "<@U_MONICA>, checkout crash"},
    )

    try:
        result = monica_plugin._on_pre_gateway_dispatch(event)
    finally:
        monica_plugin._runtime.cache_clear()

    assert result == {
        "action": "skip_reply",
        "reason": "monica_runtime_unavailable",
        "text": SAFE_RUNTIME_UNAVAILABLE_TEXT,
    }


def test_pre_gateway_dispatch_runtime_failure_catches_direct_message(monkeypatch):
    monica_plugin._runtime.cache_clear()
    monkeypatch.setattr(monica_plugin, "load_monica_config", lambda: MonicaConfig(enabled=True))
    monkeypatch.setattr(
        monica_plugin,
        "runtime_root",
        lambda config: (_ for _ in ()).throw(ValueError("bad Monica runtime root")),
    )
    event = SimpleNamespace(
        source=SimpleNamespace(platform=Platform.SLACK, chat_id="D_MONICA", chat_type="dm"),
        raw_message={
            "type": "message",
            "channel_type": "im",
            "text": "checkout crash",
        },
    )

    try:
        result = monica_plugin._on_pre_gateway_dispatch(event)
    finally:
        monica_plugin._runtime.cache_clear()

    assert result == {
        "action": "skip_reply",
        "reason": "monica_runtime_unavailable",
        "text": SAFE_RUNTIME_UNAVAILABLE_TEXT,
    }


def test_pre_gateway_dispatch_runtime_failure_ignores_group_dm_without_mention(monkeypatch):
    monica_plugin._runtime.cache_clear()
    monkeypatch.setattr(monica_plugin, "load_monica_config", lambda: MonicaConfig(enabled=True))
    monkeypatch.setattr(
        monica_plugin,
        "runtime_root",
        lambda config: (_ for _ in ()).throw(ValueError("bad Monica runtime root")),
    )
    event = SimpleNamespace(
        source=SimpleNamespace(platform=Platform.SLACK, chat_id="G_MONICA", chat_type="dm"),
        raw_message={
            "type": "message",
            "channel_type": "mpim",
            "text": "checkout crash",
        },
    )

    try:
        result = monica_plugin._on_pre_gateway_dispatch(event)
    finally:
        monica_plugin._runtime.cache_clear()

    assert result is None
