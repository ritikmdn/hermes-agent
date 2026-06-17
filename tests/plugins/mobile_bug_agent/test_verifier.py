from __future__ import annotations

import subprocess

from plugins.mobile_bug_agent.verifier import VerificationRunner


def _mark_git_worktree(path):
    (path / ".git").write_text("gitdir: /tmp/fake-worktree-git-dir", encoding="utf-8")
    return path


def test_verifier_passes_all_commands(tmp_path):
    commands = []
    runner = VerificationRunner(
        run_command=lambda cmd, cwd, timeout: commands.append((cmd, cwd, timeout)) or (0, "ok", "")
    )

    result = runner.run(_mark_git_worktree(tmp_path), ["npm test", "npm run lint"])

    assert result.passed is True
    assert [cmd for cmd, _cwd, _timeout in commands] == ["npm test", "npm run lint"]
    assert result.summary == "Verification passed."


def test_verifier_stops_on_first_failure(tmp_path):
    calls = []

    def run(cmd, cwd, timeout):
        calls.append(cmd)
        return 1, "", "lint failed"

    runner = VerificationRunner(run_command=run)

    result = runner.run(_mark_git_worktree(tmp_path), ["npm run lint", "npm test"])

    assert result.passed is False
    assert calls == ["npm run lint"]
    assert "lint failed" in result.output


def test_verifier_treats_command_timeout_as_failed_verification(tmp_path, monkeypatch):
    def timeout_run(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="npm test", timeout=3)

    monkeypatch.setattr(subprocess, "run", timeout_run)
    runner = VerificationRunner(timeout_seconds=3)

    result = runner.run(_mark_git_worktree(tmp_path), ["npm test", "npm run lint"])

    assert result.passed is False
    assert result.summary == "Verification failed: npm test"
    assert result.commands == ("npm test", "npm run lint")
    assert "$ npm test" in result.output
    assert "timed out after 3s" in result.output


def test_verifier_fails_closed_when_worktree_is_missing(tmp_path):
    calls = []
    runner = VerificationRunner(
        run_command=lambda cmd, cwd, timeout: calls.append((cmd, cwd, timeout)) or (0, "ok", "")
    )

    result = runner.run(tmp_path / "missing-worktree", ["npm test"])

    assert result.passed is False
    assert result.summary == "Verification failed: worktree does not exist"
    assert str(tmp_path / "missing-worktree") in result.output
    assert result.commands == ("npm test",)
    assert calls == []


def test_verifier_fails_closed_when_directory_is_not_git_worktree(tmp_path):
    calls = []
    runner = VerificationRunner(
        run_command=lambda cmd, cwd, timeout: calls.append((cmd, cwd, timeout)) or (0, "ok", "")
    )

    result = runner.run(tmp_path, ["npm test"])

    assert result.passed is False
    assert result.summary == "Verification failed: worktree is not a git worktree"
    assert str(tmp_path) in result.output
    assert result.commands == ("npm test",)
    assert calls == []
