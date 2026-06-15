from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import RepoConfig


class RepoManagerError(RuntimeError):
    pass


@dataclass(frozen=True)
class Worktree:
    branch_name: str
    path: Path
    base_ref: str = ""
    base_commit: str = ""


RunCommand = Callable[[list[str], Path | None], str]
_UNSAFE_GIT_REF_CHARS_RE = re.compile(r"[\s~^:?*\\[\]\x00-\x1f\x7f]")
_GIT_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


class RepoManager:
    def __init__(
        self,
        *,
        config: RepoConfig,
        workspace_root: str | Path,
        run_command: RunCommand | None = None,
        timeout_seconds: int = 300,
    ) -> None:
        self.config = config
        self.workspace_root = Path(workspace_root)
        self._run_command = run_command or self._default_run
        self.timeout_seconds = timeout_seconds

    def prepare_worktree(self, *, linear_identifier: str, summary: str) -> Worktree:
        if not self.config.url:
            raise RepoManagerError("mobile_bug_agent.repo.url is not configured.")

        local_name = safe_repo_local_name(self.config.local_name)
        base_ref = configured_remote_base_ref(self.config.default_branch)
        branch_prefix = safe_branch_prefix(self.config.branch_prefix)
        repo_path = self.workspace_root / "repos" / local_name
        worktrees_root = self.workspace_root / "worktrees"
        repo_path.parent.mkdir(parents=True, exist_ok=True)
        worktrees_root.mkdir(parents=True, exist_ok=True)

        if repo_path.exists():
            if not repo_path.is_dir():
                raise RepoManagerError(f"repo path exists but is not a directory: {repo_path}")
            origin_url = self._run_command(
                ["git", "-C", str(repo_path), "remote", "get-url", "origin"],
                None,
            ).strip()
            if origin_url and origin_url != self.config.url:
                raise RepoManagerError(
                    "cached repo origin does not match mobile_bug_agent.repo.url; "
                    f"expected {self.config.url}, got {origin_url}: {repo_path}"
                )
        else:
            self._run_command(["git", "clone", self.config.url, str(repo_path)], None)

        self._run_command(["git", "-C", str(repo_path), "fetch", "--prune", "origin"], None)
        repo_status = self._run_command(
            ["git", "-C", str(repo_path), "status", "--porcelain"],
            None,
        ).strip()
        if repo_status:
            raise RepoManagerError(
                "cached repo has uncommitted changes; clean or archive the Monica repo "
                f"before starting a fresh run: {repo_path}"
            )
        base_commit = self._run_command(
            ["git", "-C", str(repo_path), "rev-parse", base_ref],
            None,
        ).strip()
        if not _looks_like_git_commit(base_commit):
            raise RepoManagerError(
                f"could not resolve valid latest base commit for {base_ref}; "
                "check mobile_bug_agent.repo.default_branch and the origin remote."
            )

        branch_name = self._branch_name(
            branch_prefix=branch_prefix,
            linear_identifier=linear_identifier,
            summary=summary,
        )
        worktree_path = worktrees_root / branch_name.replace("/", "-")
        if worktree_path.exists():
            if not worktree_path.is_dir():
                raise RepoManagerError(f"worktree path exists but is not a directory: {worktree_path}")
            if not _looks_like_git_worktree(worktree_path):
                raise RepoManagerError(f"worktree path exists but is not a git worktree: {worktree_path}")
            current_branch = self._run_command(
                ["git", "-C", str(worktree_path), "branch", "--show-current"],
                None,
            ).strip()
            if current_branch != branch_name:
                raise RepoManagerError(
                    f"worktree branch mismatch: expected {branch_name}, got {current_branch or 'detached HEAD'}"
                )
            status = self._run_command(
                ["git", "-C", str(worktree_path), "status", "--porcelain"],
                None,
            ).strip()
            if status:
                raise RepoManagerError(
                    "worktree has uncommitted changes; clean or archive the existing Monica "
                    f"worktree before retrying: {worktree_path}"
                )
            current_head = self._run_command(
                ["git", "-C", str(worktree_path), "rev-parse", "HEAD"],
                None,
            ).strip()
            if current_head != base_commit:
                raise RepoManagerError(
                    "existing Monica worktree is not at latest base; archive the existing "
                    f"worktree before starting a fresh run: {worktree_path}"
                )
            return Worktree(
                branch_name=branch_name,
                path=worktree_path,
                base_ref=base_ref,
                base_commit=base_commit,
            )

        local_branch = self._run_command(
            ["git", "-C", str(repo_path), "branch", "--list", branch_name],
            None,
        )
        if _branch_list_contains(local_branch, branch_name):
            raise RepoManagerError(
                "local Monica branch already exists without its expected worktree; "
                f"archive or remove the stale branch before starting a fresh run: {branch_name}"
            )
        remote_branch = f"origin/{branch_name}"
        remote_branch_list = self._run_command(
            ["git", "-C", str(repo_path), "branch", "-r", "--list", remote_branch],
            None,
        )
        if _branch_list_contains(remote_branch_list, remote_branch):
            raise RepoManagerError(
                "remote Monica branch already exists; choose a new Linear issue/summary "
                f"or archive the stale remote branch before starting a fresh run: {remote_branch}"
            )

        self._run_command(
            [
                "git",
                "-C",
                str(repo_path),
                "worktree",
                "add",
                "-B",
                branch_name,
                str(worktree_path),
                base_commit,
            ],
            None,
        )
        return Worktree(
            branch_name=branch_name,
            path=worktree_path,
            base_ref=base_ref,
            base_commit=base_commit,
        )

    def _branch_name(self, *, branch_prefix: str, linear_identifier: str, summary: str) -> str:
        ident = _slug(linear_identifier, fallback="slack", lowercase=False)
        summary_slug = _slug(summary, fallback="bug", lowercase=True)
        return f"{branch_prefix}/{ident}-{summary_slug}"

    def _default_run(self, cmd: list[str], cwd: Path | None = None) -> str:
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(cwd) if cwd else None,
                check=False,
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise RepoManagerError(
                "\n".join(
                    part
                    for part in [
                        f"command timed out after {self.timeout_seconds}s: {' '.join(cmd)}",
                        f"cwd: {cwd}" if cwd else "",
                    ]
                    if part
                )
            ) from exc
        except FileNotFoundError as exc:
            executable = cmd[0] if cmd else "command"
            raise RepoManagerError(f"executable not found: {executable}") from exc
        if proc.returncode != 0:
            stdout = _tail(proc.stdout)
            stderr = _tail(proc.stderr)
            raise RepoManagerError(
                "\n".join(
                    part
                    for part in [
                        f"command failed ({proc.returncode}): {' '.join(cmd)}",
                        f"cwd: {cwd}" if cwd else "",
                        f"stdout: {stdout}" if stdout else "",
                        f"stderr: {stderr}" if stderr else "",
                    ]
                    if part
                )
            )
        return proc.stdout


def _slug(value: str, *, fallback: str, lowercase: bool) -> str:
    source = value.strip().lower() if lowercase else value.strip()
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", source).strip("-")
    slug = re.sub(r"-+", "-", slug)
    return (slug or fallback)[:80]


def _looks_like_git_worktree(path: Path) -> bool:
    return (path / ".git").exists()


def _branch_list_contains(output: str, branch_name: str) -> bool:
    expected = str(branch_name or "").strip()
    for line in str(output or "").splitlines():
        candidate = line.strip()
        if candidate.startswith("*"):
            candidate = candidate[1:].strip()
        if candidate == expected:
            return True
    return False


def _looks_like_git_commit(value: str) -> bool:
    return bool(_GIT_COMMIT_RE.fullmatch(str(value or "").strip()))


def safe_repo_local_name(value: str) -> str:
    name = value.strip()
    path = Path(name)
    if not name or path.is_absolute() or len(path.parts) != 1 or name in {".", ".."}:
        raise RepoManagerError("repo.local_name must be a simple directory name.")
    if "chandler" in name.lower():
        raise RepoManagerError("repo.local_name must not point at a Chandler directory.")
    return name


def safe_branch_prefix(value: str) -> str:
    prefix = str(value or "").strip()
    if not is_safe_git_branch_name(prefix):
        raise RepoManagerError("repo.branch_prefix must be a safe git branch prefix.")
    if "chandler" in prefix.lower():
        raise RepoManagerError("repo.branch_prefix must not point at Chandler.")
    return prefix


def safe_default_branch(value: str) -> str:
    branch = str(value or "").strip()
    if not is_safe_git_branch_name(branch):
        raise RepoManagerError("repo.default_branch must be a safe git branch name.")
    return branch


def configured_remote_base_ref(value: str) -> str:
    branch = safe_default_branch(value)
    return branch if branch.startswith("origin/") else f"origin/{branch}"


def is_safe_git_branch_name(value: str) -> bool:
    branch = str(value or "").strip()
    if not branch or branch.startswith("/") or branch.endswith("/") or "//" in branch:
        return False
    parts = branch.split("/")
    return not (
        branch == "@"
        or "@{" in branch
        or ".." in branch
        or any(
            not part
            or part in {".", ".."}
            or part.startswith((".", "-"))
            or part.endswith(".")
            or part.endswith(".lock")
            or _UNSAFE_GIT_REF_CHARS_RE.search(part)
            for part in parts
        )
    )


def _tail(value: str, *, limit: int = 2000) -> str:
    return value.strip()[-limit:]
