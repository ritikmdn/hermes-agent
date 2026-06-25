from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import MonicaConfig, runtime_root


class CodexWorkerError(RuntimeError):
    pass


RunCodexCommand = Callable[[list[str], Path, str, int], str]


def build_code_worker(config: MonicaConfig) -> Any:
    if config.worker.backend == "codex_cli":
        return CodexCliWorker(config=config)
    if config.worker.backend == "internal_agent":
        return InternalCodexWorker(config=config)
    raise CodexWorkerError(f"unknown worker backend: {config.worker.backend}")


@dataclass
class CodexCliWorker:
    config: MonicaConfig
    run_command: RunCodexCommand | None = None

    def run(self, *, run: Any, worktree: Any, brief: str) -> dict[str, Any]:
        worktree_path = Path(getattr(worktree, "path", worktree))
        _require_worktree(worktree_path)
        _require_expected_branch(run=run, worktree=worktree)
        _require_safe_codex_cli_settings(self.config)
        output_dir = runtime_root(self.config) / "worker-output"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_file = output_dir / f"{getattr(run, 'id', 'run')}.md"
        command = self._command(worktree_path=worktree_path, output_file=output_file)
        prompt = "\n\n".join([_system_prompt(), _user_prompt(brief=brief, worktree_path=worktree_path)])
        output = (self.run_command or self._default_run)(
            command,
            worktree_path,
            prompt,
            self.config.worker.timeout_minutes * 60,
        )
        summary = _read_text(output_file) or output.strip()
        result = {
            "changed": bool(summary),
            "summary": summary or "Codex CLI completed without a final message.",
            "worktree": str(worktree_path),
            "worker": "codex_cli",
            "output_file": str(output_file),
        }
        result.update(_worktree_base_metadata(worktree))
        proof_deep_link = _extract_proof_deep_link(summary)
        if proof_deep_link:
            result["proof_deep_link"] = proof_deep_link
        proof_expected_text = _extract_proof_expected_text(summary)
        if proof_expected_text:
            result["proof_expected_text"] = proof_expected_text
        proof_screen = _extract_proof_screen(summary)
        if proof_screen:
            result["proof_screen"] = proof_screen
        return result

    def _command(self, *, worktree_path: Path, output_file: Path) -> list[str]:
        command = [
            self.config.worker.codex_command,
            "exec",
            "-c",
            f'approval_policy="{self.config.worker.codex_approval_policy}"',
            "--cd",
            str(worktree_path),
            "--sandbox",
            self.config.worker.codex_sandbox,
            "--color",
            "never",
            "--output-last-message",
            str(output_file),
        ]
        if self.config.worker.codex_model:
            command.extend(["--model", self.config.worker.codex_model])
        if self.config.worker.codex_profile:
            command.extend(["--profile", self.config.worker.codex_profile])
        command.append("-")
        return command

    @staticmethod
    def _default_run(command: list[str], cwd: Path, prompt: str, timeout: int) -> str:
        try:
            proc = subprocess.run(
                command,
                cwd=str(cwd),
                input=prompt,
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise CodexWorkerError(f"Codex CLI executable not found: {command[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise CodexWorkerError("Codex CLI worker timed out.") from exc
        if proc.returncode != 0:
            raise CodexWorkerError(_command_failure_message(command, cwd, proc))
        return proc.stdout


@dataclass
class InternalCodexWorker:
    config: MonicaConfig
    agent_factory: Any | None = None

    def run(self, *, run: Any, worktree: Any, brief: str) -> dict[str, Any]:
        worktree_path = Path(getattr(worktree, "path", worktree))
        _require_worktree(worktree_path)
        _require_expected_branch(run=run, worktree=worktree)
        _require_monica_worker_session_prefix(self.config)
        system_message = _system_prompt()
        user_message = _user_prompt(brief=brief, worktree_path=worktree_path)

        previous_cwd = os.environ.get("TERMINAL_CWD")
        os.environ["TERMINAL_CWD"] = str(worktree_path)
        try:
            agent = self._agent(run_id=str(getattr(run, "id", "") or "run"))
            result = agent.run_conversation(
                user_message=user_message,
                system_message=system_message,
                task_id=f"monica-{getattr(run, 'id', '')}",
            )
        finally:
            if previous_cwd is None:
                os.environ.pop("TERMINAL_CWD", None)
            else:
                os.environ["TERMINAL_CWD"] = previous_cwd

        summary = str((result or {}).get("final_response") or "").strip()
        payload = {
            "changed": bool(summary),
            "summary": summary or "Monica worker completed without a summary.",
            "worktree": str(worktree_path),
            "worker": "internal_agent",
        }
        payload.update(_worktree_base_metadata(worktree))
        proof_deep_link = _extract_proof_deep_link(summary)
        if proof_deep_link:
            payload["proof_deep_link"] = proof_deep_link
        proof_expected_text = _extract_proof_expected_text(summary)
        if proof_expected_text:
            payload["proof_expected_text"] = proof_expected_text
        proof_screen = _extract_proof_screen(summary)
        if proof_screen:
            payload["proof_screen"] = proof_screen
        return payload

    def _agent(self, *, run_id: str) -> Any:
        session_id = f"{self.config.runtime.worker_session_prefix}-{run_id}"
        if self.agent_factory is not None:
            try:
                return self.agent_factory(
                    max_iterations=self.config.loop.max_iterations,
                    quiet_mode=True,
                    platform="monica",
                    session_id=session_id,
                    skip_context_files=True,
                    skip_memory=self.config.runtime.skip_memory,
                )
            except TypeError:
                return self.agent_factory()
        from run_agent import AIAgent

        return AIAgent(
            max_iterations=self.config.loop.max_iterations,
            quiet_mode=True,
            platform="monica",
            session_id=session_id,
            skip_context_files=True,
            skip_memory=self.config.runtime.skip_memory,
        )


def _system_prompt() -> str:
    return (
        "You are Monica's code worker for a React Native mobile app.\n"
        "Handle only marketplace copy/design fixes for Monica.\n"
        "Work only inside the provided repository worktree.\n"
        "Fix the bug described in the brief with the smallest maintainable change.\n"
        "Add or update focused tests when the repo has a nearby pattern.\n"
        "Do not commit. Do not push. Do not create a pull request. Do not modify Hermes.\n"
        "For marketplace copy/design fixes, your final response must include both proof "
        "target final lines exactly, plus the optional screen line when you know the "
        "route or screen name:\n"
        "Monica proof deep link: <url>\n"
        "Monica proof expected text: <text visible on the fixed screen>\n"
        "Monica proof screen: <route or screen name>\n"
        "Stop and report blockers if the brief lacks enough information."
    )


def _user_prompt(*, brief: str, worktree_path: Path) -> str:
    return "\n".join(
        [
            brief,
            "",
            "Worker sandbox:",
            f"Repository worktree: {worktree_path}",
        ]
    )


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _proof_target_re(label: str) -> re.Pattern[str]:
    escaped = re.escape(label)
    return re.compile(
        rf"(?im)^\s*(?:[-*]\s*)?(?:\*\*{escaped}(?::\*\*|\*\*\s*:)|{escaped}\s*:)\s*(?P<value>.+?)\s*$"
    )


_PROOF_DEEP_LINK_RE = _proof_target_re("Monica proof deep link")
_PROOF_EXPECTED_TEXT_RE = _proof_target_re("Monica proof expected text")
_PROOF_SCREEN_RE = _proof_target_re("Monica proof screen")
_MARKDOWN_LINK_RE = re.compile(r"^\[[^\]]+\]\((?P<url>[^)\s]+)\)$")


def _extract_proof_deep_link(summary: str) -> str:
    for match in _PROOF_DEEP_LINK_RE.finditer(str(summary or "")):
        value = _normalized_proof_deep_link_value(match.group("value"))
        if value.lower() in {"none", "n/a", "na", "unknown", "unavailable"}:
            continue
        if "://" in value or value.startswith(("exp+", "http://", "https://")):
            return value
    return ""


def _extract_proof_expected_text(summary: str) -> str:
    for match in _PROOF_EXPECTED_TEXT_RE.finditer(str(summary or "")):
        value = _normalized_proof_expected_text_value(match.group("value"))
        if value.lower() in _UNUSABLE_PROOF_EXPECTED_TEXT_VALUES:
            continue
        return value
    return ""


def _extract_proof_screen(summary: str) -> str:
    for match in _PROOF_SCREEN_RE.finditer(str(summary or "")):
        value = _normalized_proof_screen_value(match.group("value"))
        if value.lower() in _UNUSABLE_PROOF_SCREEN_VALUES:
            continue
        return value
    return ""


def _normalized_proof_deep_link_value(value: str) -> str:
    text = _normalized_proof_line_value(value, trim_sentence_punctuation=True)
    match = _MARKDOWN_LINK_RE.fullmatch(text)
    if match:
        text = _normalized_proof_line_value(
            match.group("url"),
            trim_sentence_punctuation=True,
        )
    return text


def _normalized_proof_expected_text_value(value: str) -> str:
    text = str(value or "").strip()
    while len(text) >= 2 and text[-1] in ".,;" and text[-2] in "*`>\"'":
        text = text[:-1].strip()
    return _normalized_proof_line_value(text)


def _normalized_proof_screen_value(value: str) -> str:
    return _normalized_proof_line_value(value, trim_sentence_punctuation=True)


def _normalized_proof_line_value(value: str, *, trim_sentence_punctuation: bool = False) -> str:
    text = str(value or "").strip()
    wrappers = (("**", "**"), ("`", "`"), ("<", ">"), ('"', '"'), ("'", "'"))
    while text:
        original = text
        if trim_sentence_punctuation:
            text = text.rstrip(".,;").strip()
        for opener, closer in wrappers:
            if len(text) >= 2 and text.startswith(opener) and text.endswith(closer):
                text = text[len(opener) : -len(closer)].strip()
        if text == original:
            break
    return text


_UNUSABLE_PROOF_EXPECTED_TEXT_VALUES = {
    "n/a",
    "na",
    "none",
    "text visible on fixed screen",
    "text visible on the fixed screen",
    "unknown",
    "unavailable",
}
_UNUSABLE_PROOF_SCREEN_VALUES = {
    "n/a",
    "na",
    "none",
    "route or screen name",
    "unknown",
    "unavailable",
}


def _require_worktree(path: Path) -> None:
    if not path.is_dir():
        raise CodexWorkerError(f"worktree does not exist: {path}")
    if not (path / ".git").exists():
        raise CodexWorkerError(f"worktree is not a git worktree: {path}")


def _worktree_base_metadata(worktree: Any) -> dict[str, str]:
    metadata: dict[str, str] = {}
    base_ref = str(getattr(worktree, "base_ref", "") or "").strip()
    base_commit = str(getattr(worktree, "base_commit", "") or "").strip()
    if base_ref:
        metadata["base_ref"] = base_ref
    if base_commit:
        metadata["base_commit"] = base_commit
    return metadata


def _require_expected_branch(*, run: Any, worktree: Any) -> None:
    branch_name = str(getattr(worktree, "branch_name", "") or "").strip()
    linear_identifier = str(getattr(run, "linear_identifier", "") or "").strip()
    if not branch_name or not linear_identifier:
        return
    if linear_identifier not in branch_name or "monica" not in branch_name.lower():
        raise CodexWorkerError(
            f"worktree branch mismatch: {branch_name} does not look like Monica branch for {linear_identifier}"
        )


def _require_safe_codex_cli_settings(config: MonicaConfig) -> None:
    if config.worker.codex_sandbox != "workspace-write":
        raise CodexWorkerError("codex_sandbox must be workspace-write for Monica codex_cli runs.")
    if config.worker.codex_approval_policy != "never":
        raise CodexWorkerError("codex_approval_policy must be never for Monica codex_cli runs.")


def _require_monica_worker_session_prefix(config: MonicaConfig) -> None:
    if "monica" not in config.runtime.worker_session_prefix.lower():
        raise CodexWorkerError("worker_session_prefix must include monica for Monica internal_agent runs.")


def _command_failure_message(
    command: list[str],
    cwd: Path,
    proc: subprocess.CompletedProcess[str],
) -> str:
    return "\n".join(
        part
        for part in [
            f"Codex CLI failed ({proc.returncode}): {' '.join(command)}",
            f"cwd: {cwd}",
            f"stdout: {_tail(proc.stdout)}" if _tail(proc.stdout) else "",
            f"stderr: {_tail(proc.stderr)}" if _tail(proc.stderr) else "",
        ]
        if part
    )


def _tail(value: str | None, *, limit: int = 2000) -> str:
    return str(value or "").strip()[-limit:]
