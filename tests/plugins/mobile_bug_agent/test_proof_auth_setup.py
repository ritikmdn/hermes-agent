from __future__ import annotations

from pathlib import Path

import pytest

from plugins.mobile_bug_agent.proof_auth_setup import (
    MONICA_AUTH_BOOTSTRAP_NAME,
    MONICA_AUTH_LOG_MARKER,
    ProofAuthBootstrapPatch,
    ProofAuthSetupHarness,
    _normalize_test_phone,
    _render_auth_bootstrap,
    _wait_for_auth_setup_marker,
)


def _mobile_worktree(tmp_path: Path) -> Path:
    worktree = tmp_path / "mobile"
    app_root = worktree / "apps" / "elixir-card" / "app"
    app_root.mkdir(parents=True)
    (worktree / ".git").mkdir()
    (worktree / "package.json").write_text("{}", encoding="utf-8")
    (app_root / "_layout.tsx").write_text(
        "\n".join(
            [
                'import "@elixir/ui-kit/boot";',
                'import React from "react";',
                "",
                "export default function RootLayout() {",
                "  return null;",
                "}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return worktree


def test_normalize_test_phone_matches_mobile_login_prefixing() -> None:
    assert _normalize_test_phone("9876543210") == "+919876543210"
    assert _normalize_test_phone("+15551234567") == "+15551234567"
    assert _normalize_test_phone("  987 654 3210  ") == "+919876543210"


def test_auth_route_embeds_otp_without_logging_secret_values() -> None:
    rendered = _render_auth_bootstrap(phone="+919876543210", otp="654321")

    assert "supabase.auth.verifyOtp" in rendered
    assert "MONICA_AUTH_SETUP_COMPLETE" in rendered
    assert "+919876543210" in rendered
    assert "654321" in rendered
    assert f'console.error("[MonicaProofAuth] {MONICA_AUTH_LOG_MARKER}")' in rendered
    assert "token" not in rendered.split("MONICA_AUTH_SETUP_COMPLETE", 1)[1]


def test_bootstrap_patch_creates_temporary_import_and_restores_layout(tmp_path: Path) -> None:
    worktree = _mobile_worktree(tmp_path)
    app_root = worktree / "apps" / "elixir-card" / "app"
    layout = app_root / "_layout.tsx"
    original_layout = layout.read_text(encoding="utf-8")
    bootstrap_path = app_root / f"{MONICA_AUTH_BOOTSTRAP_NAME}.ts"

    with ProofAuthBootstrapPatch(worktree, phone="+919876543210", otp="654321") as patch:
        assert patch.bootstrap_path == bootstrap_path
        assert bootstrap_path.is_file()
        assert "MONICA_AUTH_SETUP_COMPLETE" in bootstrap_path.read_text(encoding="utf-8")
        patched_layout = layout.read_text(encoding="utf-8")
        assert f'import "./{MONICA_AUTH_BOOTSTRAP_NAME}";' in patched_layout
        assert "MONICA_TEST_OTP" not in patched_layout

    assert not bootstrap_path.exists()
    assert layout.read_text(encoding="utf-8") == original_layout


def test_bootstrap_patch_refuses_existing_auth_bootstrap(tmp_path: Path) -> None:
    worktree = _mobile_worktree(tmp_path)
    bootstrap_path = (
        worktree / "apps" / "elixir-card" / "app" / f"{MONICA_AUTH_BOOTSTRAP_NAME}.ts"
    )
    bootstrap_path.write_text("export default null;\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="temporary Monica auth bootstrap already exists"):
        with ProofAuthBootstrapPatch(worktree, phone="+919876543210", otp="654321"):
            pass


def test_bootstrap_patch_cleans_stale_generated_bootstrap_before_reapplying(
    tmp_path: Path,
) -> None:
    worktree = _mobile_worktree(tmp_path)
    bootstrap_path = (
        worktree / "apps" / "elixir-card" / "app" / f"{MONICA_AUTH_BOOTSTRAP_NAME}.ts"
    )
    bootstrap_path.write_text(
        "[MonicaProofAuth] MONICA_AUTH_SETUP_COMPLETE\n",
        encoding="utf-8",
    )

    with ProofAuthBootstrapPatch(worktree, phone="+919876543210", otp="654321"):
        assert "supabase.auth.verifyOtp" in bootstrap_path.read_text(encoding="utf-8")

    assert not bootstrap_path.exists()


@pytest.mark.parametrize("bootstrap_exists", (False, True))
def test_bootstrap_patch_drops_stale_layout_import_before_restore(
    tmp_path: Path,
    bootstrap_exists: bool,
) -> None:
    worktree = _mobile_worktree(tmp_path)
    app_root = worktree / "apps" / "elixir-card" / "app"
    layout = app_root / "_layout.tsx"
    original_layout = layout.read_text(encoding="utf-8")
    bootstrap_path = app_root / f"{MONICA_AUTH_BOOTSTRAP_NAME}.ts"
    layout.write_text(
        original_layout.replace(
            'import "@elixir/ui-kit/boot";',
            (
                'import "@elixir/ui-kit/boot";\n'
                f'import "./{MONICA_AUTH_BOOTSTRAP_NAME}";'
            ),
            1,
        ),
        encoding="utf-8",
    )
    if bootstrap_exists:
        bootstrap_path.write_text(
            "[MonicaProofAuth] MONICA_AUTH_SETUP_COMPLETE\n",
            encoding="utf-8",
        )

    with ProofAuthBootstrapPatch(worktree, phone="+919876543210", otp="654321"):
        assert bootstrap_path.is_file()
        assert f'import "./{MONICA_AUTH_BOOTSTRAP_NAME}";' in layout.read_text(
            encoding="utf-8"
        )

    assert not bootstrap_path.exists()
    assert layout.read_text(encoding="utf-8") == original_layout


def test_wait_for_auth_setup_accepts_restored_valid_session(tmp_path: Path) -> None:
    stdout = tmp_path / "metro.stdout.log"
    stderr = tmp_path / "metro.stderr.log"
    stdout.write_text(
        "\n".join(
            [
                "INFO  [Auth] Session restored: bcd04c8c-45bf-4e35-9199-2b62056ac277",
                "INFO  [Auth] Token valid, initialization complete",
            ]
        ),
        encoding="utf-8",
    )
    stderr.write_text("", encoding="utf-8")

    _wait_for_auth_setup_marker(
        stdout,
        stderr,
        timeout_seconds=1,
        platform="Android",
        sleep=lambda _seconds: None,
    )


def test_wait_for_auth_setup_failure_marker_wins_over_restored_session(tmp_path: Path) -> None:
    stdout = tmp_path / "metro.stdout.log"
    stderr = tmp_path / "metro.stderr.log"
    stdout.write_text(
        "\n".join(
            [
                "INFO  [Auth] Session restored: bcd04c8c-45bf-4e35-9199-2b62056ac277",
                "INFO  [Auth] Token valid, initialization complete",
                "ERROR  [MonicaProofAuth] MONICA_AUTH_SETUP_FAILED invalid OTP",
            ]
        ),
        encoding="utf-8",
    )
    stderr.write_text("", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Android proof auth setup failed"):
        _wait_for_auth_setup_marker(
            stdout,
            stderr,
            timeout_seconds=1,
            platform="Android",
            sleep=lambda _seconds: None,
        )


def test_ios_auth_setup_skips_install_when_app_is_already_installed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = _mobile_worktree(tmp_path)
    app_dir = worktree / "apps" / "elixir-card"
    app_dir.mkdir(exist_ok=True)
    (app_dir / "package.json").write_text("{}", encoding="utf-8")
    proof_dir = tmp_path / "proof"
    commands: list[tuple[str, ...]] = []

    def run_text(args: tuple[str, ...], _cwd: Path, _timeout: int) -> str:
        commands.append(args)
        return ""

    def run_ios_until_ready(*_args: object, **_kwargs: object) -> None:
        return None

    harness = ProofAuthSetupHarness(
        run_text=run_text,
        run_ios_until_ready=run_ios_until_ready,
    )
    monkeypatch.setattr(
        harness._proof_harness,
        "_prepare_ios_native_project",
        lambda _app_dir, _timeout: None,
    )
    monkeypatch.setattr(
        "plugins.mobile_bug_agent.proof_auth_setup._ios_app_is_installed",
        lambda *_args: True,
    )
    monkeypatch.setattr(
        "plugins.mobile_bug_agent.proof_auth_setup._install_temporary_ios_fast_fingerprint_config",
        lambda _app_dir: (lambda: None),
    )
    monkeypatch.setattr(
        "plugins.mobile_bug_agent.proof_auth_setup._grant_ios_notification_permission",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "plugins.mobile_bug_agent.proof_auth_setup._worktree_has_native_sensitive_changes",
        lambda _worktree: False,
    )

    harness._setup_ios_auth(
        worktree=worktree,
        proof_dir=proof_dir,
        simulator_udid="SIMULATOR",
        bundle_id="com.elixir.card",
        dev_client_scheme="elixir-card",
        timeout_seconds=10,
    )

    assert ("npx", "expo", "run:ios", "--no-install", "--no-bundler") not in commands


def test_ios_auth_setup_reinstalls_when_native_sensitive_files_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = _mobile_worktree(tmp_path)
    app_dir = worktree / "apps" / "elixir-card"
    app_dir.mkdir(exist_ok=True)
    (app_dir / "package.json").write_text("{}", encoding="utf-8")
    proof_dir = tmp_path / "proof"
    commands: list[tuple[str, ...]] = []

    def run_text(args: tuple[str, ...], _cwd: Path, _timeout: int) -> str:
        commands.append(args)
        return ""

    harness = ProofAuthSetupHarness(
        run_text=run_text,
        run_ios_until_ready=lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        harness._proof_harness,
        "_prepare_ios_native_project",
        lambda _app_dir, _timeout: None,
    )
    monkeypatch.setattr(
        "plugins.mobile_bug_agent.proof_auth_setup._ios_app_is_installed",
        lambda *_args: True,
    )
    monkeypatch.setattr(
        "plugins.mobile_bug_agent.proof_auth_setup._install_temporary_ios_fast_fingerprint_config",
        lambda _app_dir: (lambda: None),
    )
    monkeypatch.setattr(
        "plugins.mobile_bug_agent.proof_auth_setup._grant_ios_notification_permission",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "plugins.mobile_bug_agent.proof_auth_setup._worktree_has_native_sensitive_changes",
        lambda _worktree: True,
    )

    harness._setup_ios_auth(
        worktree=worktree,
        proof_dir=proof_dir,
        simulator_udid="SIMULATOR",
        bundle_id="com.elixir.card",
        dev_client_scheme="elixir-card",
        timeout_seconds=10,
    )

    assert ("npx", "expo", "run:ios", "--no-install", "--no-bundler") in commands


def test_android_auth_setup_uses_metro_start_when_app_is_already_installed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = _mobile_worktree(tmp_path)
    app_dir = worktree / "apps" / "elixir-card"
    app_dir.mkdir(exist_ok=True)
    (app_dir / "package.json").write_text("{}", encoding="utf-8")
    proof_dir = tmp_path / "proof"
    proof_dir.mkdir()
    foreground_calls: list[tuple[str, ...]] = []

    def run_text(args: tuple[str, ...], _cwd: Path, _timeout: int) -> str:
        if args == ("emulator", "-list-avds"):
            return "MonicaPixel\n"
        return "ok"

    def run_android_until_foreground(
        args,
        _cwd,
        _timeout,
        _adb,
        _package,
        log_dir,
        open_dev_client,
        capture_foreground,
        open_dev_client_before_foreground=False,
    ):
        foreground_calls.append(args)
        assert open_dev_client_before_foreground is True
        assert log_dir is not None
        (log_dir / "android-metro.stdout.log").write_text(
            "\n".join(
                [
                    "INFO  [Auth] Session restored: bcd04c8c-45bf-4e35-9199-2b62056ac277",
                    "INFO  [Auth] Token valid, initialization complete",
                ]
            ),
            encoding="utf-8",
        )
        open_dev_client()
        capture_foreground()

    harness = ProofAuthSetupHarness(
        run_text=run_text,
        run_android_until_foreground=run_android_until_foreground,
        sleep=lambda _seconds: None,
    )
    monkeypatch.setattr(
        "plugins.mobile_bug_agent.proof_auth_setup._ensure_android_emulator",
        lambda *_args: (lambda: None),
    )
    monkeypatch.setattr(
        "plugins.mobile_bug_agent.proof_auth_setup._android_package_is_installed",
        lambda *_args: True,
    )
    monkeypatch.setattr(
        "plugins.mobile_bug_agent.proof_auth_setup._worktree_has_native_sensitive_changes",
        lambda _worktree: False,
    )

    harness._setup_android_auth(
        worktree=worktree,
        proof_dir=proof_dir,
        android_serial="emulator-5554",
        android_avd="MonicaPixel",
        android_package="com.elixir.card",
        dev_client_scheme="elixir-card",
        timeout_seconds=10,
    )

    assert foreground_calls == [
        (
            "npx",
            "expo",
            "start",
            "--dev-client",
            "--clear",
            "--host",
            "localhost",
            "--port",
            "8081",
        )
    ]


def test_android_auth_setup_reinstalls_when_native_sensitive_files_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = _mobile_worktree(tmp_path)
    app_dir = worktree / "apps" / "elixir-card"
    app_dir.mkdir(exist_ok=True)
    (app_dir / "package.json").write_text("{}", encoding="utf-8")
    proof_dir = tmp_path / "proof"
    proof_dir.mkdir()
    foreground_calls: list[tuple[str, ...]] = []

    def run_text(args: tuple[str, ...], _cwd: Path, _timeout: int) -> str:
        if args == ("emulator", "-list-avds"):
            return "MonicaPixel\n"
        return "ok"

    def run_android_until_foreground(
        args,
        _cwd,
        _timeout,
        _adb,
        _package,
        log_dir,
        open_dev_client,
        capture_foreground,
        open_dev_client_before_foreground=False,
    ):
        foreground_calls.append(args)
        assert log_dir is not None
        (log_dir / "android-metro.stdout.log").write_text(
            "INFO  [Auth] Session restored: bcd04c8c-45bf-4e35-9199-2b62056ac277\n"
            "INFO  [Auth] Token valid, initialization complete\n",
            encoding="utf-8",
        )
        open_dev_client()
        capture_foreground()

    harness = ProofAuthSetupHarness(
        run_text=run_text,
        run_android_until_foreground=run_android_until_foreground,
        sleep=lambda _seconds: None,
    )
    monkeypatch.setattr(
        "plugins.mobile_bug_agent.proof_auth_setup._ensure_android_emulator",
        lambda *_args: (lambda: None),
    )
    monkeypatch.setattr(
        "plugins.mobile_bug_agent.proof_auth_setup._android_package_is_installed",
        lambda *_args: True,
    )
    monkeypatch.setattr(
        "plugins.mobile_bug_agent.proof_auth_setup._worktree_has_native_sensitive_changes",
        lambda _worktree: True,
    )

    harness._setup_android_auth(
        worktree=worktree,
        proof_dir=proof_dir,
        android_serial="emulator-5554",
        android_avd="MonicaPixel",
        android_package="com.elixir.card",
        dev_client_scheme="elixir-card",
        timeout_seconds=10,
    )

    assert foreground_calls == [
        ("npx", "expo", "run:android", "--app-id", "com.elixir.card")
    ]
