from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from plugins.mobile_bug_agent.codex_worker import (
    CodexCliWorker,
    CodexWorkerError,
    InternalCodexWorker,
    build_code_worker,
)
from plugins.mobile_bug_agent.config import MonicaConfig, RuntimeConfig, WorkerConfig


@dataclass
class FakeRun:
    id: str = "run-id"
    linear_identifier: str = "MOB-42"


class FakeAgent:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def run_conversation(self, user_message, system_message=None, conversation_history=None, task_id=None):
        assert "You are Monica's code worker" in system_message
        assert "Do not commit" in system_message
        assert "Do not push" in system_message
        assert "Do not create a pull request" in system_message
        assert "Slack thread" in user_message
        assert task_id == "monica-run-id"
        return {"final_response": "Changed src/Checkout.tsx and added a regression test."}


def _mark_git_worktree(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").write_text("gitdir: /tmp/fake-worktree-git-dir", encoding="utf-8")
    return path


@dataclass
class FakeWorktree:
    path: Path
    branch_name: str = "monica/MOB-42-checkout-crash"
    base_ref: str = ""
    base_commit: str = ""


def test_codex_worker_runs_in_worktree(tmp_path):
    worker = InternalCodexWorker(config=MonicaConfig(), agent_factory=FakeAgent)
    worktree = _mark_git_worktree(tmp_path)

    result = worker.run(
        run=FakeRun(),
        worktree=worktree,
        brief="Slack thread: https://slack/thread\nBug: Android checkout crash",
    )

    assert result["summary"].startswith("Changed")
    assert result["worktree"] == str(worktree)
    assert result["changed"] is True
    assert result["worker"] == "internal_agent"


def test_codex_workers_return_worktree_base_metadata(tmp_path):
    worktree_path = _mark_git_worktree(tmp_path / "worktree")
    worktree = FakeWorktree(
        path=worktree_path,
        base_ref="origin/dev",
        base_commit="abc1234",
    )

    def fake_run(command, _cwd, _prompt, _timeout):
        output_file = Path(command[command.index("--output-last-message") + 1])
        output_file.write_text("Changed marketplace PDP copy.", encoding="utf-8")
        return ""

    cli_result = CodexCliWorker(
        config=MonicaConfig(runtime=RuntimeConfig(home_subdir=str(tmp_path / "runtime"))),
        run_command=fake_run,
    ).run(
        run=FakeRun(),
        worktree=worktree,
        brief="Slack thread: https://slack/thread\nBug: Marketplace PDP copy is wrong",
    )
    internal_result = InternalCodexWorker(
        config=MonicaConfig(),
        agent_factory=FakeAgent,
    ).run(
        run=FakeRun(),
        worktree=worktree,
        brief="Slack thread: https://slack/thread\nBug: Marketplace PDP copy is wrong",
    )

    assert cli_result["base_ref"] == "origin/dev"
    assert cli_result["base_commit"] == "abc1234"
    assert internal_result["base_ref"] == "origin/dev"
    assert internal_result["base_commit"] == "abc1234"


def test_codex_worker_uses_isolated_monica_session(tmp_path):
    captured = {}

    class CapturingAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run_conversation(self, user_message, system_message=None, conversation_history=None, task_id=None):
            return {"final_response": "Changed src/Checkout.tsx."}

    worker = InternalCodexWorker(config=MonicaConfig(), agent_factory=CapturingAgent)
    worktree = _mark_git_worktree(tmp_path)

    worker.run(
        run=FakeRun(id="abc123"),
        worktree=worktree,
        brief="Slack thread: https://slack/thread\nBug: Android checkout crash",
    )

    assert captured["platform"] == "monica"
    assert captured["session_id"] == "monica-abc123"
    assert captured["skip_context_files"] is True
    assert captured["skip_memory"] is True


def test_codex_cli_worker_rejects_branch_mismatch_before_invoking_codex(tmp_path):
    calls = []
    worker = CodexCliWorker(config=MonicaConfig(), run_command=lambda *args: calls.append(args) or "")
    worktree_path = _mark_git_worktree(tmp_path)
    worktree = FakeWorktree(path=worktree_path, branch_name="chandler/MOB-42-checkout-crash")
    run = FakeRun(linear_identifier="MOB-42")

    with pytest.raises(CodexWorkerError, match="worktree branch mismatch"):
        worker.run(
            run=run,
            worktree=worktree,
            brief="Slack thread: https://slack/thread\nBug: Android checkout crash",
        )

    assert calls == []


def test_internal_codex_worker_rejects_non_monica_session_prefix_before_invoking_agent(tmp_path):
    created_agents = []

    class CapturingAgent:
        def __init__(self, **kwargs):
            created_agents.append(kwargs)

        def run_conversation(self, user_message, system_message=None, conversation_history=None, task_id=None):
            return {"final_response": "Changed src/Checkout.tsx."}

    worker = InternalCodexWorker(
        config=MonicaConfig(
            runtime=RuntimeConfig(worker_session_prefix="chandler"),
            worker=WorkerConfig(backend="internal_agent"),
        ),
        agent_factory=CapturingAgent,
    )
    worktree = _mark_git_worktree(tmp_path)

    with pytest.raises(CodexWorkerError, match="worker_session_prefix must include monica"):
        worker.run(
            run=FakeRun(id="abc123"),
            worktree=worktree,
            brief="Slack thread: https://slack/thread\nBug: Android checkout crash",
        )

    assert created_agents == []


def test_codex_cli_worker_invokes_codex_exec_in_worktree(tmp_path):
    calls = []

    def fake_run(command, cwd, prompt, timeout):
        calls.append((command, cwd, prompt, timeout))
        output_file = Path(command[command.index("--output-last-message") + 1])
        output_file.write_text("Changed via Codex CLI.", encoding="utf-8")
        return "stdout fallback"

    worker = CodexCliWorker(
        config=MonicaConfig(
            runtime=RuntimeConfig(home_subdir=str(tmp_path / "runtime")),
            worker=WorkerConfig(
                codex_model="gpt-5-codex",
                codex_profile="monica",
                timeout_minutes=7,
            ),
        ),
        run_command=fake_run,
    )
    worktree = _mark_git_worktree(tmp_path)

    result = worker.run(
        run=FakeRun(id="abc123"),
        worktree=worktree,
        brief="Slack thread: https://slack/thread\nBug: Android checkout crash",
    )

    command, cwd, prompt, timeout = calls[0]
    assert command[:2] == ["codex", "exec"]
    assert command[command.index("-c") + 1] == 'approval_policy="never"'
    assert command[command.index("--cd") + 1] == str(worktree)
    assert command[command.index("--sandbox") + 1] == "workspace-write"
    assert "--ask-for-approval" not in command
    assert command[command.index("--model") + 1] == "gpt-5-codex"
    assert command[command.index("--profile") + 1] == "monica"
    assert command[-1] == "-"
    assert cwd == worktree
    assert "You are Monica's code worker" in prompt
    assert "marketplace copy/design fixes" in prompt
    assert "Do not push" in prompt
    assert "Android checkout crash" in prompt
    assert timeout == 420
    assert result["worker"] == "codex_cli"
    assert result["summary"] == "Changed via Codex CLI."
    assert result["output_file"].endswith("abc123.md")


def test_codex_cli_worker_prompt_requires_both_proof_target_lines(tmp_path):
    calls = []

    def fake_run(command, cwd, prompt, timeout):
        calls.append((command, cwd, prompt, timeout))
        output_file = Path(command[command.index("--output-last-message") + 1])
        output_file.write_text("Changed the marketplace PDP copy.", encoding="utf-8")
        return ""

    worker = CodexCliWorker(
        config=MonicaConfig(runtime=RuntimeConfig(home_subdir=str(tmp_path / "runtime"))),
        run_command=fake_run,
    )
    worktree = _mark_git_worktree(tmp_path)

    worker.run(
        run=FakeRun(id="abc123"),
        worktree=worktree,
        brief="Slack thread: https://slack/thread\nBug: Marketplace PDP copy is wrong",
    )

    prompt = calls[0][2]
    assert "must include both proof target final lines" in prompt
    assert "Monica proof deep link: <url>" in prompt
    assert "Monica proof expected text: <text visible on the fixed screen>" in prompt
    assert "Monica proof screen: <route or screen name>" in prompt


def test_codex_cli_worker_extracts_proof_deep_link_from_summary(tmp_path):
    def fake_run(command, _cwd, _prompt, _timeout):
        output_file = Path(command[command.index("--output-last-message") + 1])
        output_file.write_text(
            "\n".join(
                [
                    "Changed the offer PDP tags.",
                    "Monica proof deep link: elixir-card://marketplace/offer/fitness-first",
                    "Monica proof expected text: Fitness First",
                ]
            ),
            encoding="utf-8",
        )
        return ""

    worker = CodexCliWorker(
        config=MonicaConfig(runtime=RuntimeConfig(home_subdir=str(tmp_path / "runtime"))),
        run_command=fake_run,
    )
    worktree = _mark_git_worktree(tmp_path)

    result = worker.run(
        run=FakeRun(id="abc123"),
        worktree=worktree,
        brief="Slack thread: https://slack/thread\nBug: hard-coded PDP tags",
    )

    assert result["proof_deep_link"] == "elixir-card://marketplace/offer/fitness-first"
    assert result["proof_expected_text"] == "Fitness First"


def test_codex_cli_worker_extracts_optional_proof_screen_from_summary(tmp_path):
    def fake_run(command, _cwd, _prompt, _timeout):
        output_file = Path(command[command.index("--output-last-message") + 1])
        output_file.write_text(
            "\n".join(
                [
                    "Changed the offer PDP tags.",
                    "Monica proof deep link: elixir-card://marketplace/offer/fitness-first",
                    "Monica proof expected text: Fitness First",
                    "Monica proof screen: /MarketplacePdp",
                ]
            ),
            encoding="utf-8",
        )
        return ""

    worker = CodexCliWorker(
        config=MonicaConfig(runtime=RuntimeConfig(home_subdir=str(tmp_path / "runtime"))),
        run_command=fake_run,
    )
    worktree = _mark_git_worktree(tmp_path)

    result = worker.run(
        run=FakeRun(id="abc123"),
        worktree=worktree,
        brief="Slack thread: https://slack/thread\nBug: hard-coded PDP tags",
    )

    assert result["proof_screen"] == "/MarketplacePdp"


def test_codex_cli_worker_ignores_placeholder_proof_expected_text(tmp_path):
    def fake_run(command, _cwd, _prompt, _timeout):
        output_file = Path(command[command.index("--output-last-message") + 1])
        output_file.write_text(
            "\n".join(
                [
                    "Changed the offer PDP tags.",
                    "Monica proof deep link: elixir-card://marketplace/offer/fitness-first",
                    "Monica proof expected text: <text visible on the fixed screen>",
                ]
            ),
            encoding="utf-8",
        )
        return ""

    worker = CodexCliWorker(
        config=MonicaConfig(runtime=RuntimeConfig(home_subdir=str(tmp_path / "runtime"))),
        run_command=fake_run,
    )
    worktree = _mark_git_worktree(tmp_path)

    result = worker.run(
        run=FakeRun(id="abc123"),
        worktree=worktree,
        brief="Slack thread: https://slack/thread\nBug: hard-coded PDP tags",
    )

    assert result["proof_deep_link"] == "elixir-card://marketplace/offer/fitness-first"
    assert "proof_expected_text" not in result


def test_codex_cli_worker_extracts_bulleted_proof_target_lines(tmp_path):
    def fake_run(command, _cwd, _prompt, _timeout):
        output_file = Path(command[command.index("--output-last-message") + 1])
        output_file.write_text(
            "\n".join(
                [
                    "Changed the offer PDP tags.",
                    "- Monica proof deep link: elixir-card://marketplace/offer/fitness-first",
                    "- Monica proof expected text: Fitness First",
                ]
            ),
            encoding="utf-8",
        )
        return ""

    worker = CodexCliWorker(
        config=MonicaConfig(runtime=RuntimeConfig(home_subdir=str(tmp_path / "runtime"))),
        run_command=fake_run,
    )
    worktree = _mark_git_worktree(tmp_path)

    result = worker.run(
        run=FakeRun(id="abc123"),
        worktree=worktree,
        brief="Slack thread: https://slack/thread\nBug: hard-coded PDP tags",
    )

    assert result["proof_deep_link"] == "elixir-card://marketplace/offer/fitness-first"
    assert result["proof_expected_text"] == "Fitness First"


def test_codex_cli_worker_extracts_markdown_bold_proof_target_labels(tmp_path):
    def fake_run(command, _cwd, _prompt, _timeout):
        output_file = Path(command[command.index("--output-last-message") + 1])
        output_file.write_text(
            "\n".join(
                [
                    "Changed the offer PDP tags.",
                    "- **Monica proof deep link:** elixir-card://marketplace/offer/fitness-first",
                    "- **Monica proof expected text:** **Fitness First**.",
                    "- **Monica proof screen:** `/MarketplacePdp`.",
                ]
            ),
            encoding="utf-8",
        )
        return ""

    worker = CodexCliWorker(
        config=MonicaConfig(runtime=RuntimeConfig(home_subdir=str(tmp_path / "runtime"))),
        run_command=fake_run,
    )
    worktree = _mark_git_worktree(tmp_path)

    result = worker.run(
        run=FakeRun(id="abc123"),
        worktree=worktree,
        brief="Slack thread: https://slack/thread\nBug: hard-coded PDP tags",
    )

    assert result["proof_deep_link"] == "elixir-card://marketplace/offer/fitness-first"
    assert result["proof_expected_text"] == "Fitness First"
    assert result["proof_screen"] == "/MarketplacePdp"


def test_codex_cli_worker_normalizes_wrapped_proof_target_lines(tmp_path):
    def fake_run(command, _cwd, _prompt, _timeout):
        output_file = Path(command[command.index("--output-last-message") + 1])
        output_file.write_text(
            "\n".join(
                [
                    "Changed the offer PDP tags.",
                    "Monica proof deep link: <`elixir-card://marketplace/offer/fitness-first`>.",
                    "Monica proof expected text: \"Fitness First\"",
                ]
            ),
            encoding="utf-8",
        )
        return ""

    worker = CodexCliWorker(
        config=MonicaConfig(runtime=RuntimeConfig(home_subdir=str(tmp_path / "runtime"))),
        run_command=fake_run,
    )
    worktree = _mark_git_worktree(tmp_path)

    result = worker.run(
        run=FakeRun(id="abc123"),
        worktree=worktree,
        brief="Slack thread: https://slack/thread\nBug: hard-coded PDP tags",
    )

    assert result["proof_deep_link"] == "elixir-card://marketplace/offer/fitness-first"
    assert result["proof_expected_text"] == "Fitness First"


def test_codex_cli_worker_trims_sentence_punctuation_after_wrapped_expected_text(tmp_path):
    def fake_run(command, _cwd, _prompt, _timeout):
        output_file = Path(command[command.index("--output-last-message") + 1])
        output_file.write_text(
            "\n".join(
                [
                    "Changed the offer PDP tags.",
                    "Monica proof deep link: elixir-card://marketplace/offer/fitness-first",
                    "Monica proof expected text: `Fitness First`.",
                ]
            ),
            encoding="utf-8",
        )
        return ""

    worker = CodexCliWorker(
        config=MonicaConfig(runtime=RuntimeConfig(home_subdir=str(tmp_path / "runtime"))),
        run_command=fake_run,
    )
    worktree = _mark_git_worktree(tmp_path)

    result = worker.run(
        run=FakeRun(id="abc123"),
        worktree=worktree,
        brief="Slack thread: https://slack/thread\nBug: hard-coded PDP tags",
    )

    assert result["proof_expected_text"] == "Fitness First"


def test_codex_cli_worker_extracts_markdown_proof_deep_link(tmp_path):
    def fake_run(command, _cwd, _prompt, _timeout):
        output_file = Path(command[command.index("--output-last-message") + 1])
        output_file.write_text(
            "\n".join(
                [
                    "Changed the offer PDP tags.",
                    "Monica proof deep link: [PDP](elixir-card://marketplace/offer/fitness-first).",
                    "Monica proof expected text: Fitness First",
                ]
            ),
            encoding="utf-8",
        )
        return ""

    worker = CodexCliWorker(
        config=MonicaConfig(runtime=RuntimeConfig(home_subdir=str(tmp_path / "runtime"))),
        run_command=fake_run,
    )
    worktree = _mark_git_worktree(tmp_path)

    result = worker.run(
        run=FakeRun(id="abc123"),
        worktree=worktree,
        brief="Slack thread: https://slack/thread\nBug: hard-coded PDP tags",
    )

    assert result["proof_deep_link"] == "elixir-card://marketplace/offer/fitness-first"
    assert result["proof_expected_text"] == "Fitness First"


def test_codex_cli_worker_extracts_markdown_proof_deep_link_with_spaced_label(tmp_path):
    def fake_run(command, _cwd, _prompt, _timeout):
        output_file = Path(command[command.index("--output-last-message") + 1])
        output_file.write_text(
            "\n".join(
                [
                    "Changed the offer PDP tags.",
                    "Monica proof deep link: [Fitness First PDP](elixir-card://marketplace/offer/fitness-first)",
                    "Monica proof expected text: Fitness First",
                ]
            ),
            encoding="utf-8",
        )
        return ""

    worker = CodexCliWorker(
        config=MonicaConfig(runtime=RuntimeConfig(home_subdir=str(tmp_path / "runtime"))),
        run_command=fake_run,
    )
    worktree = _mark_git_worktree(tmp_path)

    result = worker.run(
        run=FakeRun(id="abc123"),
        worktree=worktree,
        brief="Slack thread: https://slack/thread\nBug: hard-coded PDP tags",
    )

    assert result["proof_deep_link"] == "elixir-card://marketplace/offer/fitness-first"
    assert result["proof_expected_text"] == "Fitness First"


def test_codex_cli_worker_requires_existing_worktree_before_invoking_codex(tmp_path):
    calls = []
    worker = CodexCliWorker(config=MonicaConfig(), run_command=lambda *args: calls.append(args) or "")

    with pytest.raises(CodexWorkerError, match="worktree does not exist"):
        worker.run(
            run=FakeRun(id="abc123"),
            worktree=tmp_path / "missing-worktree",
            brief="Slack thread: https://slack/thread\nBug: Android checkout crash",
        )

    assert calls == []


def test_codex_cli_worker_rejects_unsafe_sandbox_before_invoking_codex(tmp_path):
    calls = []
    worker = CodexCliWorker(
        config=MonicaConfig(worker=WorkerConfig(codex_sandbox="danger-full-access")),
        run_command=lambda *args: calls.append(args) or "",
    )
    worktree = _mark_git_worktree(tmp_path)

    with pytest.raises(CodexWorkerError, match="codex_sandbox must be workspace-write"):
        worker.run(
            run=FakeRun(id="abc123"),
            worktree=worktree,
            brief="Slack thread: https://slack/thread\nBug: Android checkout crash",
        )

    assert calls == []


def test_codex_cli_worker_rejects_interactive_approval_before_invoking_codex(tmp_path):
    calls = []
    worker = CodexCliWorker(
        config=MonicaConfig(worker=WorkerConfig(codex_approval_policy="on-request")),
        run_command=lambda *args: calls.append(args) or "",
    )
    worktree = _mark_git_worktree(tmp_path)

    with pytest.raises(CodexWorkerError, match="codex_approval_policy must be never"):
        worker.run(
            run=FakeRun(id="abc123"),
            worktree=worktree,
            brief="Slack thread: https://slack/thread\nBug: Android checkout crash",
        )

    assert calls == []


def test_codex_workers_require_git_worktree_before_invoking_tools(tmp_path):
    plain_dir = tmp_path / "plain-directory"
    plain_dir.mkdir()
    codex_calls = []
    worker = CodexCliWorker(config=MonicaConfig(), run_command=lambda *args: codex_calls.append(args) or "")

    with pytest.raises(CodexWorkerError, match="worktree is not a git worktree"):
        worker.run(
            run=FakeRun(id="abc123"),
            worktree=plain_dir,
            brief="Slack thread: https://slack/thread\nBug: Android checkout crash",
        )

    created_agents = []

    class CapturingAgent:
        def __init__(self, **kwargs):
            created_agents.append(kwargs)

        def run_conversation(self, user_message, system_message=None, conversation_history=None, task_id=None):
            return {"final_response": "Changed src/Checkout.tsx."}

    internal_worker = InternalCodexWorker(config=MonicaConfig(), agent_factory=CapturingAgent)

    with pytest.raises(CodexWorkerError, match="worktree is not a git worktree"):
        internal_worker.run(
            run=FakeRun(id="abc123"),
            worktree=plain_dir,
            brief="Slack thread: https://slack/thread\nBug: Android checkout crash",
        )

    assert codex_calls == []
    assert created_agents == []


def test_internal_codex_worker_requires_existing_worktree_before_invoking_agent(tmp_path):
    created_agents = []

    class CapturingAgent:
        def __init__(self, **kwargs):
            created_agents.append(kwargs)

        def run_conversation(self, user_message, system_message=None, conversation_history=None, task_id=None):
            return {"final_response": "Changed src/Checkout.tsx."}

    worker = InternalCodexWorker(config=MonicaConfig(), agent_factory=CapturingAgent)

    with pytest.raises(CodexWorkerError, match="worktree does not exist"):
        worker.run(
            run=FakeRun(id="abc123"),
            worktree=tmp_path / "missing-worktree",
            brief="Slack thread: https://slack/thread\nBug: Android checkout crash",
        )

    assert created_agents == []


def test_codex_cli_worker_nonzero_exit_includes_command_context(tmp_path, monkeypatch):
    def failed_run(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["codex", "exec"],
            returncode=2,
            stdout="stdout detail",
            stderr="stderr detail",
        )

    monkeypatch.setattr(subprocess, "run", failed_run)
    worker = CodexCliWorker(config=MonicaConfig())
    worktree = _mark_git_worktree(tmp_path)

    with pytest.raises(CodexWorkerError) as exc_info:
        worker.run(
            run=FakeRun(id="abc123"),
            worktree=worktree,
            brief="Slack thread: https://slack/thread\nBug: Android checkout crash",
        )

    message = str(exc_info.value)
    assert "Codex CLI failed (2): codex exec" in message
    assert f"cwd: {worktree}" in message
    assert "stdout: stdout detail" in message
    assert "stderr: stderr detail" in message


def test_build_code_worker_defaults_to_codex_cli():
    worker = build_code_worker(MonicaConfig())

    assert isinstance(worker, CodexCliWorker)


def test_build_code_worker_can_use_internal_agent():
    worker = build_code_worker(MonicaConfig(worker=WorkerConfig(backend="internal_agent")))

    assert isinstance(worker, InternalCodexWorker)


def test_build_code_worker_rejects_unknown_backend():
    with pytest.raises(CodexWorkerError, match="unknown worker backend"):
        build_code_worker(MonicaConfig(worker=WorkerConfig(backend="mystery")))
