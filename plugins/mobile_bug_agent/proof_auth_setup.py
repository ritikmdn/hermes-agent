from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Callable, Sequence

from .simulator_proof import (
    RunAndroidUntilForeground,
    RunBytes,
    RunIosUntilReady,
    RunText,
    SimulatorProofHarness,
    _android_packager_hostname,
    _android_metro_command,
    _android_package_is_installed,
    _android_run_command,
    _android_settle_seconds,
    _clean_platforms,
    _ensure_android_emulator,
    _expo_dev_client_url,
    _expo_project_dir,
    _grant_ios_notification_permission,
    _install_temporary_ios_fast_fingerprint_config,
    _ios_app_is_installed,
    _ios_expo_host,
    _ios_packager_hostname,
    _open_android_url,
    _packager_url,
    _prepare_react_native_worktree,
    _run_android_until_foreground,
    _run_bytes_command,
    _run_ios_until_ready,
    _run_text_command,
    _tail_text,
    _worktree_has_native_sensitive_changes,
)


MONICA_AUTH_BOOTSTRAP_NAME = "monica-auth-bootstrap"
MONICA_AUTH_LOG_MARKER = "MONICA_AUTH_SETUP_COMPLETE"
_MONICA_AUTH_FAILED_MARKER = "MONICA_AUTH_SETUP_FAILED"
_AUTH_RESTORED_MARKER = "Session restored:"
_AUTH_TOKEN_VALID_MARKER = "[Auth] Token valid, initialization complete"
_BOOTSTRAP_IMPORT = f'import "./{MONICA_AUTH_BOOTSTRAP_NAME}";'
_BOOTSTRAP_ANCHOR = 'import "@elixir/ui-kit/boot";'


def _normalize_test_phone(value: str) -> str:
    clean = str(value or "").strip()
    if clean.startswith("+"):
        return "+" + re.sub(r"\D", "", clean[1:])
    digits = re.sub(r"\D", "", clean)
    if not digits:
        return ""
    return f"+91{digits}"


def _render_auth_bootstrap(*, phone: str, otp: str) -> str:
    phone_json = json.dumps(phone)
    otp_json = json.dumps(otp)
    return f"""import {{ supabase }} from "@elixir/core/lib/supabase";

const PHONE = {phone_json};
const OTP = {otp_json};

declare global {{
  // eslint-disable-next-line no-var
  var __MONICA_AUTH_SETUP_PROMISE__: Promise<void> | undefined;
}}

async function runMonicaAuthSetup() {{
  try {{
    console.error("[MonicaProofAuth] starting");
    const request = await supabase.auth.signInWithOtp({{ phone: PHONE }});
    if (request.error) {{
      console.warn("[MonicaProofAuth] otp request warning", request.error.message);
    }}

    const verified = await supabase.auth.verifyOtp({{
      phone: PHONE,
      token: OTP,
      type: "sms",
    }});
    if (verified.error) {{
      throw verified.error;
    }}

    const session = verified.data.session ?? (await supabase.auth.getSession()).data.session;
    if (!session?.access_token) {{
      throw new Error("Supabase session was not persisted after OTP verification");
    }}

    console.error("[MonicaProofAuth] {MONICA_AUTH_LOG_MARKER}");
  }} catch (error) {{
    const message = error instanceof Error ? error.message : String(error);
    console.error("[MonicaProofAuth] {_MONICA_AUTH_FAILED_MARKER}", message);
  }}
}}

if (!globalThis.__MONICA_AUTH_SETUP_PROMISE__) {{
  globalThis.__MONICA_AUTH_SETUP_PROMISE__ = runMonicaAuthSetup();
}}

export {{}};
"""


class ProofAuthBootstrapPatch:
    def __init__(self, worktree: str | Path, *, phone: str, otp: str) -> None:
        self.worktree = Path(worktree)
        self.phone = phone
        self.otp = otp
        self.app_root = self.worktree / "apps" / "elixir-card" / "app"
        self.bootstrap_path = self.app_root / f"{MONICA_AUTH_BOOTSTRAP_NAME}.ts"
        self.layout_path = self.app_root / "_layout.tsx"
        self._original_layout = ""

    def __enter__(self) -> "ProofAuthBootstrapPatch":
        self._cleanup_stale_generated_bootstrap()
        if self.bootstrap_path.exists():
            raise RuntimeError(
                f"temporary Monica auth bootstrap already exists: {self.bootstrap_path}"
            )
        if not self.layout_path.is_file():
            raise RuntimeError(f"app/_layout.tsx was not found: {self.layout_path}")

        self._original_layout = self.layout_path.read_text(encoding="utf-8")
        self.bootstrap_path.write_text(
            _render_auth_bootstrap(phone=self.phone, otp=self.otp),
            encoding="utf-8",
        )
        try:
            self.layout_path.write_text(
                _patch_root_layout(self._original_layout),
                encoding="utf-8",
            )
        except Exception:
            self.bootstrap_path.unlink(missing_ok=True)
            raise
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.bootstrap_path.unlink(missing_ok=True)
        if self._original_layout:
            self.layout_path.write_text(
                self._original_layout,
                encoding="utf-8",
            )

    def _cleanup_stale_generated_bootstrap(self) -> None:
        if not self.bootstrap_path.exists():
            self._cleanup_stale_layout_import()
            return
        try:
            text = self.bootstrap_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        if MONICA_AUTH_LOG_MARKER in text and "[MonicaProofAuth]" in text:
            self.bootstrap_path.unlink(missing_ok=True)
            self._cleanup_stale_layout_import()

    def _cleanup_stale_layout_import(self) -> None:
        if not self.layout_path.is_file():
            return
        try:
            text = self.layout_path.read_text(encoding="utf-8")
        except OSError:
            return
        cleaned = _remove_bootstrap_import(text)
        if cleaned != text:
            self.layout_path.write_text(cleaned, encoding="utf-8")


class ProofAuthSetupHarness:
    def __init__(
        self,
        *,
        run_text: RunText | None = None,
        run_bytes: RunBytes | None = None,
        run_android_until_foreground: RunAndroidUntilForeground | None = None,
        run_ios_until_ready: RunIosUntilReady | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._run_text = run_text or _run_text_command
        self._run_bytes = run_bytes or _run_bytes_command
        self._run_android_until_foreground = (
            run_android_until_foreground or _run_android_until_foreground
        )
        self._run_ios_until_ready = run_ios_until_ready or _run_ios_until_ready
        self._sleep = sleep or time.sleep
        self._proof_harness = SimulatorProofHarness(
            run_text=self._run_text,
            run_bytes=self._run_bytes,
            run_android_until_foreground=self._run_android_until_foreground,
            run_ios_until_ready=self._run_ios_until_ready,
        )

    def run(
        self,
        *,
        worktree: str | Path,
        proof_dir: str | Path,
        platforms: Sequence[str],
        phone: str,
        otp: str,
        dev_client_scheme: str = "",
        ios_simulator_udid: str = "",
        ios_bundle_id: str = "",
        android_serial: str = "",
        android_avd: str = "",
        android_package: str = "",
        timeout_seconds: int = 600,
    ) -> list[str]:
        worktree_path = Path(worktree)
        proof_path = Path(proof_dir)
        self._proof_harness._validate_worktree(worktree_path)
        _prepare_react_native_worktree(worktree_path)
        proof_path.mkdir(parents=True, exist_ok=True)

        clean_phone = _normalize_test_phone(phone)
        clean_otp = str(otp or "").strip()
        if not clean_phone:
            raise RuntimeError("MONICA_TEST_PHONE is required for proof auth setup.")
        if not clean_otp:
            raise RuntimeError("MONICA_TEST_OTP is required for proof auth setup.")
        scheme = dev_client_scheme.strip()
        if not scheme:
            raise RuntimeError("MONICA_DEV_CLIENT_SCHEME is required for proof auth setup.")

        completed: list[str] = []
        with ProofAuthBootstrapPatch(worktree_path, phone=clean_phone, otp=clean_otp):
            for platform in _clean_platforms(platforms):
                if platform == "ios":
                    self._setup_ios_auth(
                        worktree=worktree_path,
                        proof_dir=proof_path,
                        simulator_udid=ios_simulator_udid,
                        bundle_id=ios_bundle_id,
                        dev_client_scheme=scheme,
                        timeout_seconds=timeout_seconds,
                    )
                elif platform == "android":
                    self._setup_android_auth(
                        worktree=worktree_path,
                        proof_dir=proof_path,
                        android_serial=android_serial,
                        android_avd=android_avd,
                        android_package=android_package,
                        dev_client_scheme=scheme,
                        timeout_seconds=timeout_seconds,
                    )
                else:
                    raise RuntimeError(f"unsupported proof auth setup platform: {platform}")
                completed.append(platform)
        return completed

    def _setup_ios_auth(
        self,
        *,
        worktree: Path,
        proof_dir: Path,
        simulator_udid: str,
        bundle_id: str,
        dev_client_scheme: str,
        timeout_seconds: int,
    ) -> None:
        target = simulator_udid.strip() or "booted"
        clean_bundle_id = bundle_id.strip()
        if not clean_bundle_id:
            raise RuntimeError("MONICA_IOS_BUNDLE_ID is required for proof auth setup.")

        app_dir = _expo_project_dir(worktree)
        packager_url = _packager_url(_ios_packager_hostname(), 8081)
        restore_fingerprint_config = _install_temporary_ios_fast_fingerprint_config(app_dir)
        try:
            self._proof_harness._prepare_ios_native_project(app_dir, timeout_seconds)
            if not _ios_app_is_installed(
                target,
                app_dir,
                clean_bundle_id,
            ) or _worktree_has_native_sensitive_changes(worktree):
                try:
                    self._run_text(
                        ("npx", "expo", "run:ios", "--no-install", "--no-bundler"),
                        app_dir,
                        timeout_seconds,
                    )
                except RuntimeError:
                    if not _ios_app_is_installed(target, app_dir, clean_bundle_id):
                        raise
            _grant_ios_notification_permission(
                self._run_text,
                target,
                app_dir,
                clean_bundle_id,
                timeout_seconds,
            )

            def setup_ready_app() -> None:
                _wait_for_auth_setup_marker(
                    proof_dir / "ios-metro.stdout.log",
                    proof_dir / "ios-metro.stderr.log",
                    timeout_seconds=timeout_seconds,
                    platform="iOS",
                    sleep=self._sleep,
                )

            self._run_ios_until_ready(
                (
                    "npx",
                    "expo",
                    "start",
                    "--dev-client",
                    "--clear",
                    "--host",
                    _ios_expo_host(),
                    "--port",
                    "8081",
                ),
                app_dir,
                timeout_seconds,
                target,
                clean_bundle_id,
                _expo_dev_client_url(dev_client_scheme, packager_url),
                proof_dir,
                setup_ready_app,
            )
        finally:
            restore_fingerprint_config()

    def _setup_android_auth(
        self,
        *,
        worktree: Path,
        proof_dir: Path,
        android_serial: str,
        android_avd: str,
        android_package: str,
        dev_client_scheme: str,
        timeout_seconds: int,
    ) -> None:
        serial = android_serial.strip() or ("emulator-5554" if android_avd.strip() else "")
        adb = ("adb", "-s", serial) if serial else ("adb",)
        package = android_package.strip()
        if not package:
            raise RuntimeError("MONICA_ANDROID_PACKAGE is required for proof auth setup.")

        avds = self._run_text(("emulator", "-list-avds"), worktree, timeout_seconds)
        if android_avd.strip() and android_avd.strip() not in set(avds.splitlines()):
            raise RuntimeError(f"Android AVD was not found: {android_avd.strip()}")
        emulator_cleanup = _ensure_android_emulator(
            android_avd.strip(),
            adb,
            worktree,
            timeout_seconds,
        )
        app_dir = _expo_project_dir(worktree)
        try:
            self._run_text((*adb, "version"), worktree, timeout_seconds)
            self._run_text((*adb, "reverse", "tcp:8081", "tcp:8081"), worktree, timeout_seconds)

            def open_dev_client() -> None:
                _open_android_url(
                    self._run_text,
                    adb,
                    worktree,
                    timeout_seconds,
                    _expo_dev_client_url(
                        dev_client_scheme,
                        _packager_url(_android_packager_hostname(), 8081),
                    ),
                    package,
                )
                self._sleep(_android_settle_seconds())

            def capture_foreground() -> None:
                _wait_for_auth_setup_marker(
                    proof_dir / "android-metro.stdout.log",
                    proof_dir / "android-metro.stderr.log",
                    timeout_seconds=timeout_seconds,
                    platform="Android",
                    sleep=self._sleep,
                )

            command = (
                _android_metro_command()
                if _android_package_is_installed(adb, worktree, package)
                and not _worktree_has_native_sensitive_changes(worktree)
                else _android_run_command(package)
            )
            self._run_android_until_foreground(
                command,
                app_dir,
                timeout_seconds,
                adb,
                package,
                proof_dir,
                open_dev_client,
                capture_foreground,
                open_dev_client_before_foreground=True,
            )
        finally:
            emulator_cleanup()


def _patch_root_layout(text: str) -> str:
    if _BOOTSTRAP_IMPORT in text:
        return text
    if _BOOTSTRAP_ANCHOR in text:
        return text.replace(_BOOTSTRAP_ANCHOR, f"{_BOOTSTRAP_ANCHOR}\n{_BOOTSTRAP_IMPORT}", 1)
    return f"{_BOOTSTRAP_IMPORT}\n{text}"


def _remove_bootstrap_import(text: str) -> str:
    return "".join(
        line
        for line in text.splitlines(keepends=True)
        if line.strip() != _BOOTSTRAP_IMPORT
    )


def _wait_for_auth_setup_marker(
    stdout_path: Path,
    stderr_path: Path,
    *,
    timeout_seconds: int,
    platform: str,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        text = "\n".join((_tail_text(stdout_path, 12000), _tail_text(stderr_path, 12000)))
        if _MONICA_AUTH_FAILED_MARKER in text:
            raise RuntimeError(
                "\n".join(
                    [
                        f"{platform} proof auth setup failed.",
                        f"stdout tail: {_tail_text(stdout_path)}",
                        f"stderr tail: {_tail_text(stderr_path)}",
                    ]
                )
            )
        if MONICA_AUTH_LOG_MARKER in text or _auth_setup_restored_session_observed(text):
            return
        sleep(1)
    raise RuntimeError(
        "\n".join(
            [
                f"timed out waiting for {platform} proof auth setup to complete.",
                f"stdout log: {stdout_path}",
                f"stderr log: {stderr_path}",
                f"stdout tail: {_tail_text(stdout_path)}",
                f"stderr tail: {_tail_text(stderr_path)}",
            ]
        )
    )


def _auth_setup_restored_session_observed(text: str) -> bool:
    # The bootstrap can start after a valid session has already restored. Treat
    # that as setup-complete; target-screen simulator proof still fail-closes.
    return _AUTH_RESTORED_MARKER in text and _AUTH_TOKEN_VALID_MARKER in text


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _split_platforms(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.replace("\n", ",").split(",") if part.strip())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed Monica mobile proof test auth.")
    parser.add_argument("--worktree", default=_env("MONICA_WORKTREE"))
    parser.add_argument("--proof-dir", default=_env("MONICA_PROOF_DIR"))
    parser.add_argument("--platform", action="append", default=[])
    parser.add_argument("--phone", default=_env("MONICA_TEST_PHONE"))
    parser.add_argument("--otp", default=_env("MONICA_TEST_OTP"))
    parser.add_argument("--dev-client-scheme", default=_env("MONICA_DEV_CLIENT_SCHEME"))
    parser.add_argument("--ios-simulator-udid", default=_env("MONICA_IOS_SIMULATOR_UDID"))
    parser.add_argument("--ios-bundle-id", default=_env("MONICA_IOS_BUNDLE_ID"))
    parser.add_argument("--android-serial", default=_env("MONICA_ANDROID_SERIAL"))
    parser.add_argument("--android-avd", default=_env("MONICA_ANDROID_AVD"))
    parser.add_argument("--android-package", default=_env("MONICA_ANDROID_PACKAGE"))
    parser.add_argument("--timeout-seconds", type=int, default=600)
    args = parser.parse_args(argv)

    platform_values = tuple(args.platform) or _split_platforms(_env("MONICA_PROOF_PLATFORM_ORDER"))
    if not platform_values:
        platform_values = ("ios", "android")

    try:
        completed = ProofAuthSetupHarness().run(
            worktree=args.worktree,
            proof_dir=args.proof_dir,
            platforms=platform_values,
            phone=args.phone,
            otp=args.otp,
            dev_client_scheme=args.dev_client_scheme,
            ios_simulator_udid=args.ios_simulator_udid,
            ios_bundle_id=args.ios_bundle_id,
            android_serial=args.android_serial,
            android_avd=args.android_avd,
            android_package=args.android_package,
            timeout_seconds=args.timeout_seconds,
        )
    except Exception as exc:
        print(f"Monica proof auth setup failed: {exc}", file=sys.stderr)
        return 1

    print(f"Monica proof auth setup complete for: {', '.join(completed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
