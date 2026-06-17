from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class VerificationResult:
    passed: bool
    summary: str
    output: str
    commands: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "summary": self.summary,
            "output": self.output,
            "commands": list(self.commands),
        }


RunCommand = Callable[[str, Path, int], tuple[int, str, str]]


class VerificationRunner:
    def __init__(
        self,
        *,
        run_command: RunCommand | None = None,
        timeout_seconds: int = 900,
    ) -> None:
        self._run_command = run_command or self._default_run
        self.timeout_seconds = timeout_seconds

    def run(self, worktree: str | Path, commands: list[str] | tuple[str, ...]) -> VerificationResult:
        worktree_path = Path(worktree)
        command_list = tuple(command for command in commands if command.strip())
        if not worktree_path.is_dir():
            return VerificationResult(
                passed=False,
                summary="Verification failed: worktree does not exist",
                output=f"Worktree does not exist: {worktree_path}",
                commands=command_list,
            )
        if not (worktree_path / ".git").exists():
            return VerificationResult(
                passed=False,
                summary="Verification failed: worktree is not a git worktree",
                output=f"Worktree is not a git worktree: {worktree_path}",
                commands=command_list,
            )
        if not command_list:
            return VerificationResult(
                passed=False,
                summary="No verification commands configured.",
                output="mobile_bug_agent.verification.commands is empty.",
                commands=(),
            )

        output_parts: list[str] = []
        for command in command_list:
            code, stdout, stderr = self._run_command(command, worktree_path, self.timeout_seconds)
            output_parts.append(
                "\n".join(
                    [
                        f"$ {command}",
                        stdout.strip(),
                        stderr.strip(),
                    ]
                ).strip()
            )
            if code != 0:
                return VerificationResult(
                    passed=False,
                    summary=f"Verification failed: {command}",
                    output="\n\n".join(output_parts),
                    commands=command_list,
                )

        return VerificationResult(
            passed=True,
            summary="Verification passed.",
            output="\n\n".join(output_parts),
            commands=command_list,
        )

    @staticmethod
    def _default_run(command: str, cwd: Path, timeout: int) -> tuple[int, str, str]:
        try:
            proc = subprocess.run(
                command,
                cwd=str(cwd),
                shell=True,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return 124, "", f"command timed out after {timeout}s"
        return proc.returncode, proc.stdout, proc.stderr
