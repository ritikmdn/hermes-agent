from __future__ import annotations

from pathlib import Path

import pytest

from plugins.mobile_bug_agent.proof_auth_setup import (
    MONICA_AUTH_BOOTSTRAP_NAME,
    MONICA_AUTH_LOG_MARKER,
    ProofAuthBootstrapPatch,
    _normalize_test_phone,
    _render_auth_bootstrap,
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
