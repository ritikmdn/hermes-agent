from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from plugins.mobile_bug_agent.config import RepoConfig
from plugins.mobile_bug_agent.repo_manager import RepoManager, RepoManagerError


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=False,
        text=True,
        capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _init_dev_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init")
    _git(path, "checkout", "-b", "dev")
    _git(path, "config", "user.email", "monica@example.test")
    _git(path, "config", "user.name", "Monica Test")
    return path


def _commit_file(repo: Path, name: str, content: str, message: str) -> str:
    (repo / name).write_text(content)
    _git(repo, "add", name)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


def _create_bare_origin(tmp_path: Path) -> tuple[Path, Path, str]:
    source = _init_dev_repo(tmp_path / "source")
    first_commit = _commit_file(source, "app.txt", "one\n", "initial dev")
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "clone", "--bare", str(source), str(bare)],
        check=True,
        text=True,
        capture_output=True,
    )
    _git(source, "remote", "add", "origin", str(bare))
    return source, bare, first_commit


def test_repo_manager_clones_missing_repo_and_creates_named_worktree(tmp_path):
    commands: list[list[str]] = []

    def run(cmd: list[str], cwd: Path | None = None) -> str:
        commands.append(cmd if cwd is None else ["cwd=" + str(cwd), *cmd])
        if cmd[:2] == ["git", "clone"]:
            Path(cmd[-1]).mkdir(parents=True)
        if cmd[:4] == ["git", "-C", str(tmp_path / "repos" / "mobile-app"), "rev-parse"]:
            return "abc1234\n"
        return ""

    manager = RepoManager(
        config=RepoConfig(
            url="git@github.com:acme/mobile-app.git",
            local_name="mobile-app",
            default_branch="main",
            branch_prefix="monica",
        ),
        workspace_root=tmp_path,
        run_command=run,
    )

    worktree = manager.prepare_worktree(
        linear_identifier="MOB-123",
        summary="Android checkout crash after promo code",
    )

    assert worktree.branch_name == "monica/MOB-123-android-checkout-crash-after-promo-code"
    assert worktree.path == tmp_path / "worktrees" / "monica-MOB-123-android-checkout-crash-after-promo-code"
    assert worktree.base_ref == "origin/main"
    assert worktree.base_commit == "abc1234"
    assert commands == [
        [
            "git",
            "clone",
            "git@github.com:acme/mobile-app.git",
            str(tmp_path / "repos" / "mobile-app"),
        ],
        [
            "git",
            "-C",
            str(tmp_path / "repos" / "mobile-app"),
            "fetch",
            "--prune",
            "origin",
        ],
        [
            "git",
            "-C",
            str(tmp_path / "repos" / "mobile-app"),
            "status",
            "--porcelain",
        ],
        [
            "git",
            "-C",
            str(tmp_path / "repos" / "mobile-app"),
            "rev-parse",
            "origin/main",
        ],
        [
            "git",
            "-C",
            str(tmp_path / "repos" / "mobile-app"),
            "branch",
            "--list",
            "monica/MOB-123-android-checkout-crash-after-promo-code",
        ],
        [
            "git",
            "-C",
            str(tmp_path / "repos" / "mobile-app"),
            "branch",
            "-r",
            "--list",
            "origin/monica/MOB-123-android-checkout-crash-after-promo-code",
        ],
        [
            "git",
            "-C",
            str(tmp_path / "repos" / "mobile-app"),
            "worktree",
            "add",
            "-B",
            "monica/MOB-123-android-checkout-crash-after-promo-code",
            str(tmp_path / "worktrees" / "monica-MOB-123-android-checkout-crash-after-promo-code"),
            "abc1234",
        ],
    ]


def test_repo_manager_fetches_existing_repo_before_worktree(tmp_path):
    commands: list[list[str]] = []
    (tmp_path / "repos" / "mobile-app").mkdir(parents=True)

    def run(cmd: list[str], cwd: Path | None = None) -> str:
        commands.append(cmd)
        if cmd[:4] == ["git", "-C", str(tmp_path / "repos" / "mobile-app"), "rev-parse"]:
            return "abc1234\n"
        return ""

    manager = RepoManager(
        config=RepoConfig(url="git@github.com:acme/mobile-app.git", local_name="mobile-app"),
        workspace_root=tmp_path,
        run_command=run,
    )

    manager.prepare_worktree(linear_identifier="MOB-123", summary="Checkout crash")

    assert commands[:2] == [
        ["git", "-C", str(tmp_path / "repos" / "mobile-app"), "remote", "get-url", "origin"],
        [
        "git",
        "-C",
        str(tmp_path / "repos" / "mobile-app"),
        "fetch",
        "--prune",
        "origin",
        ],
    ]


def test_repo_manager_fetches_latest_remote_base_before_creating_worktree(tmp_path):
    source, bare, first_commit = _create_bare_origin(tmp_path)
    workspace_root = tmp_path / "runtime"

    manager = RepoManager(
        config=RepoConfig(
            url=str(bare),
            local_name="mobile-app",
            default_branch="dev",
        ),
        workspace_root=workspace_root,
    )

    first = manager.prepare_worktree(linear_identifier="MOB-123", summary="Checkout crash")
    assert first.base_ref == "origin/dev"
    assert first.base_commit == first_commit

    second_commit = _commit_file(source, "app.txt", "two\n", "latest dev")
    _git(source, "push", "origin", "dev")

    second = manager.prepare_worktree(linear_identifier="MOB-124", summary="Search copy")

    assert second.base_ref == "origin/dev"
    assert second.base_commit == second_commit
    assert _git(second.path, "rev-parse", "HEAD").strip() == second_commit


def test_repo_manager_prunes_deleted_remote_monica_branch_before_collision_check(tmp_path):
    source, bare, _first_commit = _create_bare_origin(tmp_path)
    stale_branch = "monica/MOB-123-checkout-crash"
    _git(source, "checkout", "-b", stale_branch)
    _git(source, "push", "origin", stale_branch)
    _git(source, "checkout", "dev")

    workspace_root = tmp_path / "runtime"
    repo_path = workspace_root / "repos" / "mobile-app"
    repo_path.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "clone", str(bare), str(repo_path)],
        check=True,
        text=True,
        capture_output=True,
    )
    assert stale_branch in _git(repo_path, "branch", "-r", "--list", f"origin/{stale_branch}")

    _git(source, "push", "origin", "--delete", stale_branch)

    manager = RepoManager(
        config=RepoConfig(
            url=str(bare),
            local_name="mobile-app",
            default_branch="dev",
        ),
        workspace_root=workspace_root,
    )

    worktree = manager.prepare_worktree(linear_identifier="MOB-123", summary="Checkout crash")

    assert worktree.branch_name == stale_branch
    assert _git(repo_path, "branch", "-r", "--list", f"origin/{stale_branch}") == ""
    assert _git(worktree.path, "branch", "--show-current").strip() == stale_branch


def test_repo_manager_rejects_existing_repo_with_wrong_origin_before_fetch(tmp_path):
    commands: list[list[str]] = []
    repo_path = tmp_path / "repos" / "mobile-app"
    repo_path.mkdir(parents=True)

    def run(cmd: list[str], cwd: Path | None = None) -> str:
        commands.append(cmd)
        if cmd == ["git", "-C", str(repo_path), "remote", "get-url", "origin"]:
            return "git@github.com:acme/chandler-app.git\n"
        if cmd == ["git", "-C", str(repo_path), "rev-parse", "origin/main"]:
            return "abc1234\n"
        return ""

    manager = RepoManager(
        config=RepoConfig(url="git@github.com:acme/mobile-app.git", local_name="mobile-app"),
        workspace_root=tmp_path,
        run_command=run,
    )

    with pytest.raises(RepoManagerError, match="cached repo origin"):
        manager.prepare_worktree(linear_identifier="MOB-123", summary="Checkout crash")

    assert commands == [
        ["git", "-C", str(repo_path), "remote", "get-url", "origin"],
    ]


def test_repo_manager_uses_configured_remote_default_branch_and_records_base_commit(tmp_path):
    commands: list[list[str]] = []
    (tmp_path / "repos" / "mobile-app").mkdir(parents=True)

    def run(cmd: list[str], cwd: Path | None = None) -> str:
        commands.append(cmd)
        if cmd[:4] == ["git", "-C", str(tmp_path / "repos" / "mobile-app"), "rev-parse"]:
            return "def4567\n"
        return ""

    manager = RepoManager(
        config=RepoConfig(
            url="git@github.com:acme/mobile-app.git",
            local_name="mobile-app",
            default_branch="dev",
        ),
        workspace_root=tmp_path,
        run_command=run,
    )

    worktree = manager.prepare_worktree(linear_identifier="MOB-123", summary="Checkout crash")

    assert worktree.base_ref == "origin/dev"
    assert worktree.base_commit == "def4567"
    assert [
        "git",
        "-C",
        str(tmp_path / "repos" / "mobile-app"),
        "worktree",
        "add",
        "-B",
        "monica/MOB-123-checkout-crash",
        str(tmp_path / "worktrees" / "monica-MOB-123-checkout-crash"),
        "def4567",
    ] in commands


def test_repo_manager_creates_worktree_from_resolved_base_commit_not_moving_ref(tmp_path):
    commands: list[list[str]] = []
    repo_path = tmp_path / "repos" / "mobile-app"
    repo_path.mkdir(parents=True)

    def run(cmd: list[str], cwd: Path | None = None) -> str:
        commands.append(cmd)
        if cmd == ["git", "-C", str(repo_path), "rev-parse", "origin/dev"]:
            return "def4567\n"
        return ""

    manager = RepoManager(
        config=RepoConfig(
            url="git@github.com:acme/mobile-app.git",
            local_name="mobile-app",
            default_branch="dev",
        ),
        workspace_root=tmp_path,
        run_command=run,
    )

    worktree = manager.prepare_worktree(linear_identifier="MOB-123", summary="Checkout crash")

    assert worktree.base_ref == "origin/dev"
    assert worktree.base_commit == "def4567"
    assert [
        "git",
        "-C",
        str(repo_path),
        "worktree",
        "add",
        "-B",
        "monica/MOB-123-checkout-crash",
        str(tmp_path / "worktrees" / "monica-MOB-123-checkout-crash"),
        "def4567",
    ] in commands


def test_repo_manager_accepts_origin_prefixed_default_branch(tmp_path):
    commands: list[list[str]] = []
    repo_path = tmp_path / "repos" / "mobile-app"
    repo_path.mkdir(parents=True)

    def run(cmd: list[str], cwd: Path | None = None) -> str:
        commands.append(cmd)
        if cmd == ["git", "-C", str(repo_path), "rev-parse", "origin/dev"]:
            return "def4567\n"
        return ""

    manager = RepoManager(
        config=RepoConfig(
            url="git@github.com:acme/mobile-app.git",
            local_name="mobile-app",
            default_branch="origin/dev",
        ),
        workspace_root=tmp_path,
        run_command=run,
    )

    worktree = manager.prepare_worktree(linear_identifier="MOB-123", summary="Checkout crash")

    assert worktree.base_ref == "origin/dev"
    assert worktree.base_commit == "def4567"
    assert [
        "git",
        "-C",
        str(repo_path),
        "worktree",
        "add",
        "-B",
        "monica/MOB-123-checkout-crash",
        str(tmp_path / "worktrees" / "monica-MOB-123-checkout-crash"),
        "def4567",
    ] in commands
    assert not any("origin/origin/dev" in cmd for cmd in commands)


def test_repo_manager_rejects_empty_remote_base_commit_before_worktree(tmp_path):
    commands: list[list[str]] = []
    repo_path = tmp_path / "repos" / "mobile-app"
    repo_path.mkdir(parents=True)

    def run(cmd: list[str], cwd: Path | None = None) -> str:
        commands.append(cmd)
        if cmd == ["git", "-C", str(repo_path), "rev-parse", "origin/dev"]:
            return "\n"
        return ""

    manager = RepoManager(
        config=RepoConfig(
            url="git@github.com:acme/mobile-app.git",
            local_name="mobile-app",
            default_branch="dev",
        ),
        workspace_root=tmp_path,
        run_command=run,
    )

    with pytest.raises(RepoManagerError, match="could not resolve valid latest base commit"):
        manager.prepare_worktree(linear_identifier="MOB-123", summary="Checkout crash")

    assert not any("worktree" in cmd for cmd in commands)


def test_repo_manager_rejects_non_sha_remote_base_commit_before_worktree(tmp_path):
    commands: list[list[str]] = []
    repo_path = tmp_path / "repos" / "mobile-app"
    repo_path.mkdir(parents=True)

    def run(cmd: list[str], cwd: Path | None = None) -> str:
        commands.append(cmd)
        if cmd == ["git", "-C", str(repo_path), "rev-parse", "origin/dev"]:
            return "abc123base\n"
        return ""

    manager = RepoManager(
        config=RepoConfig(
            url="git@github.com:acme/mobile-app.git",
            local_name="mobile-app",
            default_branch="dev",
        ),
        workspace_root=tmp_path,
        run_command=run,
    )

    with pytest.raises(RepoManagerError, match="could not resolve valid latest base commit"):
        manager.prepare_worktree(linear_identifier="MOB-123", summary="Checkout crash")

    assert not any("worktree" in cmd for cmd in commands)


def test_repo_manager_rejects_dirty_cached_repo_before_creating_worktree(tmp_path):
    commands: list[list[str]] = []
    repo_path = tmp_path / "repos" / "mobile-app"
    repo_path.mkdir(parents=True)

    def run(cmd: list[str], cwd: Path | None = None) -> str:
        commands.append(cmd)
        if cmd == ["git", "-C", str(repo_path), "status", "--porcelain"]:
            return " M package.json\n"
        if cmd == ["git", "-C", str(repo_path), "rev-parse", "origin/main"]:
            return "abc1234\n"
        return ""

    manager = RepoManager(
        config=RepoConfig(url="git@github.com:acme/mobile-app.git", local_name="mobile-app"),
        workspace_root=tmp_path,
        run_command=run,
    )

    with pytest.raises(RepoManagerError, match="cached repo has uncommitted changes"):
        manager.prepare_worktree(linear_identifier="MOB-123", summary="Checkout crash")

    assert not any("worktree" in cmd for cmd in commands)


def test_repo_manager_rejects_existing_repo_file(tmp_path):
    commands: list[list[str]] = []
    repo_path = tmp_path / "repos" / "mobile-app"
    repo_path.parent.mkdir(parents=True)
    repo_path.write_text("not a git repo")

    manager = RepoManager(
        config=RepoConfig(url="git@github.com:acme/mobile-app.git", local_name="mobile-app"),
        workspace_root=tmp_path,
        run_command=lambda cmd, cwd=None: commands.append(cmd) or "",
    )

    with pytest.raises(RepoManagerError, match="repo path exists but is not a directory"):
        manager.prepare_worktree(linear_identifier="MOB-123", summary="Checkout crash")

    assert commands == []


def test_repo_manager_rejects_unsafe_local_name_before_commands(tmp_path):
    for local_name in ("../outside-runtime", "/tmp/outside-runtime", "nested/mobile-app", ".", "..", ""):
        commands: list[list[str]] = []
        manager = RepoManager(
            config=RepoConfig(url="git@github.com:acme/mobile-app.git", local_name=local_name),
            workspace_root=tmp_path,
            run_command=lambda cmd, cwd=None: commands.append(cmd) or "",
        )

        with pytest.raises(RepoManagerError, match="repo.local_name must be a simple directory name"):
            manager.prepare_worktree(linear_identifier="MOB-123", summary="Checkout crash")

        assert commands == []


def test_repo_manager_rejects_chandler_local_name_before_commands(tmp_path):
    commands: list[list[str]] = []
    manager = RepoManager(
        config=RepoConfig(url="git@github.com:acme/mobile-app.git", local_name="chandler"),
        workspace_root=tmp_path,
        run_command=lambda cmd, cwd=None: commands.append(cmd) or "",
    )

    with pytest.raises(RepoManagerError, match="repo.local_name must not point at a Chandler directory"):
        manager.prepare_worktree(linear_identifier="MOB-123", summary="Checkout crash")

    assert commands == []


def test_repo_manager_rejects_unsafe_branch_prefix_before_commands(tmp_path):
    unsafe_prefixes = (
        "",
        "../monica",
        "/monica",
        "monica branch",
        "monica..bad",
        "monica.lock",
        "monica~bad",
        "monica:bad",
        "monica^bad",
        "monica?bad",
        "monica*bad",
        "monica[bad",
        "monica\\bad",
    )
    for branch_prefix in unsafe_prefixes:
        commands: list[list[str]] = []
        manager = RepoManager(
            config=RepoConfig(
                url="git@github.com:acme/mobile-app.git",
                local_name="mobile-app",
                branch_prefix=branch_prefix,
            ),
            workspace_root=tmp_path,
            run_command=lambda cmd, cwd=None: commands.append(cmd) or "",
        )

        with pytest.raises(RepoManagerError, match="repo.branch_prefix must be a safe git branch prefix"):
            manager.prepare_worktree(linear_identifier="MOB-123", summary="Checkout crash")

        assert commands == []


def test_repo_manager_rejects_chandler_branch_prefix_before_commands(tmp_path):
    commands: list[list[str]] = []
    manager = RepoManager(
        config=RepoConfig(
            url="git@github.com:acme/mobile-app.git",
            local_name="mobile-app",
            branch_prefix="chandler",
        ),
        workspace_root=tmp_path,
        run_command=lambda cmd, cwd=None: commands.append(cmd) or "",
    )

    with pytest.raises(RepoManagerError, match="repo.branch_prefix must not point at Chandler"):
        manager.prepare_worktree(linear_identifier="MOB-123", summary="Checkout crash")

    assert commands == []


def test_repo_manager_rejects_unsafe_default_branch_before_commands(tmp_path):
    unsafe_branches = (
        "",
        "../main",
        "/main",
        "main branch",
        "main..bad",
        "main.lock",
        "main~bad",
        "main:bad",
        "main^bad",
        "main?bad",
        "main*bad",
        "main[bad",
        "main\\bad",
    )
    for default_branch in unsafe_branches:
        commands: list[list[str]] = []
        manager = RepoManager(
            config=RepoConfig(
                url="git@github.com:acme/mobile-app.git",
                local_name="mobile-app",
                default_branch=default_branch,
            ),
            workspace_root=tmp_path,
            run_command=lambda cmd, cwd=None: commands.append(cmd) or "",
        )

        with pytest.raises(RepoManagerError, match="repo.default_branch must be a safe git branch name"):
            manager.prepare_worktree(linear_identifier="MOB-123", summary="Checkout crash")

        assert commands == []


def test_repo_manager_reuses_existing_worktree_only_when_it_is_at_latest_base(tmp_path):
    commands: list[list[str]] = []
    (tmp_path / "repos" / "mobile-app").mkdir(parents=True)
    existing = tmp_path / "worktrees" / "monica-MOB-123-checkout-crash"
    existing.mkdir(parents=True)
    (existing / ".git").write_text("gitdir: /tmp/fake-worktree-git-dir")

    def run(cmd: list[str], cwd: Path | None = None) -> str:
        commands.append(cmd)
        if cmd == ["git", "-C", str(tmp_path / "repos" / "mobile-app"), "rev-parse", "origin/main"]:
            return "abc1234\n"
        if cmd == ["git", "-C", str(existing), "branch", "--show-current"]:
            return "monica/MOB-123-checkout-crash\n"
        if cmd == ["git", "-C", str(existing), "rev-parse", "HEAD"]:
            return "abc1234\n"
        return ""

    manager = RepoManager(
        config=RepoConfig(url="git@github.com:acme/mobile-app.git", local_name="mobile-app"),
        workspace_root=tmp_path,
        run_command=run,
    )

    worktree = manager.prepare_worktree(linear_identifier="MOB-123", summary="Checkout crash")

    assert worktree.branch_name == "monica/MOB-123-checkout-crash"
    assert worktree.path == existing
    assert not any(cmd[:4] == ["git", "-C", str(tmp_path / "repos" / "mobile-app"), "worktree"] for cmd in commands)
    assert commands[-2:] == [
        ["git", "-C", str(existing), "status", "--porcelain"],
        ["git", "-C", str(existing), "rev-parse", "HEAD"],
    ]


def test_repo_manager_rejects_stale_clean_existing_worktree_for_new_run(tmp_path):
    commands: list[list[str]] = []
    (tmp_path / "repos" / "mobile-app").mkdir(parents=True)
    existing = tmp_path / "worktrees" / "monica-MOB-123-checkout-crash"
    existing.mkdir(parents=True)
    (existing / ".git").write_text("gitdir: /tmp/fake-worktree-git-dir")

    def run(cmd: list[str], cwd: Path | None = None) -> str:
        commands.append(cmd)
        if cmd == ["git", "-C", str(tmp_path / "repos" / "mobile-app"), "rev-parse", "origin/main"]:
            return "eee4567\n"
        if cmd == ["git", "-C", str(existing), "branch", "--show-current"]:
            return "monica/MOB-123-checkout-crash\n"
        if cmd == ["git", "-C", str(existing), "rev-parse", "HEAD"]:
            return "ddd4567\n"
        return ""

    manager = RepoManager(
        config=RepoConfig(url="git@github.com:acme/mobile-app.git", local_name="mobile-app"),
        workspace_root=tmp_path,
        run_command=run,
    )

    with pytest.raises(RepoManagerError, match="existing Monica worktree is not at latest base"):
        manager.prepare_worktree(linear_identifier="MOB-123", summary="Checkout crash")

    assert not any(cmd[:6] == ["git", "-C", str(tmp_path / "repos" / "mobile-app"), "worktree", "add"] for cmd in commands)
    assert commands[-3:] == [
        ["git", "-C", str(existing), "branch", "--show-current"],
        ["git", "-C", str(existing), "status", "--porcelain"],
        ["git", "-C", str(existing), "rev-parse", "HEAD"],
    ]


def test_repo_manager_rejects_existing_local_branch_without_worktree(tmp_path):
    commands: list[list[str]] = []
    repo_path = tmp_path / "repos" / "mobile-app"
    repo_path.mkdir(parents=True)

    def run(cmd: list[str], cwd: Path | None = None) -> str:
        commands.append(cmd)
        if cmd == ["git", "-C", str(repo_path), "rev-parse", "origin/main"]:
            return "abc1234\n"
        if cmd == [
            "git",
            "-C",
            str(repo_path),
            "branch",
            "--list",
            "monica/MOB-123-checkout-crash",
        ]:
            return "  monica/MOB-123-checkout-crash\n"
        return ""

    manager = RepoManager(
        config=RepoConfig(url="git@github.com:acme/mobile-app.git", local_name="mobile-app"),
        workspace_root=tmp_path,
        run_command=run,
    )

    with pytest.raises(RepoManagerError, match="local Monica branch already exists"):
        manager.prepare_worktree(linear_identifier="MOB-123", summary="Checkout crash")

    assert not any(cmd[:6] == ["git", "-C", str(repo_path), "worktree", "add"] for cmd in commands)


def test_repo_manager_rejects_existing_remote_branch_before_worktree(tmp_path):
    commands: list[list[str]] = []
    repo_path = tmp_path / "repos" / "mobile-app"
    repo_path.mkdir(parents=True)

    def run(cmd: list[str], cwd: Path | None = None) -> str:
        commands.append(cmd)
        if cmd == ["git", "-C", str(repo_path), "rev-parse", "origin/main"]:
            return "abc1234\n"
        if cmd == [
            "git",
            "-C",
            str(repo_path),
            "branch",
            "-r",
            "--list",
            "origin/monica/MOB-123-checkout-crash",
        ]:
            return "  origin/monica/MOB-123-checkout-crash\n"
        return ""

    manager = RepoManager(
        config=RepoConfig(url="git@github.com:acme/mobile-app.git", local_name="mobile-app"),
        workspace_root=tmp_path,
        run_command=run,
    )

    with pytest.raises(RepoManagerError, match="remote Monica branch already exists"):
        manager.prepare_worktree(linear_identifier="MOB-123", summary="Checkout crash")

    assert [
        "git",
        "-C",
        str(repo_path),
        "branch",
        "-r",
        "--list",
        "origin/monica/MOB-123-checkout-crash",
    ] in commands
    assert not any(cmd[:6] == ["git", "-C", str(repo_path), "worktree", "add"] for cmd in commands)


def test_repo_manager_rejects_dirty_existing_worktree_for_retry(tmp_path):
    commands: list[list[str]] = []
    (tmp_path / "repos" / "mobile-app").mkdir(parents=True)
    existing = tmp_path / "worktrees" / "monica-MOB-123-checkout-crash"
    existing.mkdir(parents=True)
    (existing / ".git").write_text("gitdir: /tmp/fake-worktree-git-dir")

    def run(cmd: list[str], cwd: Path | None = None) -> str:
        commands.append(cmd)
        if cmd == ["git", "-C", str(tmp_path / "repos" / "mobile-app"), "rev-parse", "origin/main"]:
            return "abc1234\n"
        if cmd == ["git", "-C", str(existing), "branch", "--show-current"]:
            return "monica/MOB-123-checkout-crash\n"
        if cmd == ["git", "-C", str(existing), "status", "--porcelain"]:
            return " M android/app/build.gradle\n"
        return ""

    manager = RepoManager(
        config=RepoConfig(url="git@github.com:acme/mobile-app.git", local_name="mobile-app"),
        workspace_root=tmp_path,
        run_command=run,
    )

    with pytest.raises(RepoManagerError, match="worktree has uncommitted changes"):
        manager.prepare_worktree(linear_identifier="MOB-123", summary="Checkout crash")

    assert commands[-2:] == [
        ["git", "-C", str(existing), "branch", "--show-current"],
        ["git", "-C", str(existing), "status", "--porcelain"],
    ]


def test_repo_manager_rejects_existing_worktree_on_unexpected_branch(tmp_path):
    commands: list[list[str]] = []
    (tmp_path / "repos" / "mobile-app").mkdir(parents=True)
    existing = tmp_path / "worktrees" / "monica-MOB-123-checkout-crash"
    existing.mkdir(parents=True)
    (existing / ".git").write_text("gitdir: /tmp/fake-worktree-git-dir")

    def run(cmd: list[str], cwd: Path | None = None) -> str:
        commands.append(cmd)
        if cmd == ["git", "-C", str(tmp_path / "repos" / "mobile-app"), "rev-parse", "origin/main"]:
            return "abc1234\n"
        if cmd == ["git", "-C", str(existing), "branch", "--show-current"]:
            return "main\n"
        return ""

    manager = RepoManager(
        config=RepoConfig(url="git@github.com:acme/mobile-app.git", local_name="mobile-app"),
        workspace_root=tmp_path,
        run_command=run,
    )

    with pytest.raises(RepoManagerError, match="worktree branch mismatch"):
        manager.prepare_worktree(linear_identifier="MOB-123", summary="Checkout crash")

    assert commands == [
        [
            "git",
            "-C",
            str(tmp_path / "repos" / "mobile-app"),
            "remote",
            "get-url",
            "origin",
        ],
        [
            "git",
            "-C",
            str(tmp_path / "repos" / "mobile-app"),
            "fetch",
            "--prune",
            "origin",
        ],
        ["git", "-C", str(tmp_path / "repos" / "mobile-app"), "status", "--porcelain"],
        ["git", "-C", str(tmp_path / "repos" / "mobile-app"), "rev-parse", "origin/main"],
        ["git", "-C", str(existing), "branch", "--show-current"],
    ]


def test_repo_manager_rejects_existing_worktree_directory_that_is_not_git_worktree(tmp_path):
    commands: list[list[str]] = []
    repo_path = tmp_path / "repos" / "mobile-app"
    repo_path.mkdir(parents=True)
    stale_path = tmp_path / "worktrees" / "monica-MOB-123-checkout-crash"
    stale_path.mkdir(parents=True)

    def run(cmd: list[str], cwd: Path | None = None) -> str:
        commands.append(cmd)
        if cmd == ["git", "-C", str(repo_path), "rev-parse", "origin/main"]:
            return "abc1234\n"
        return ""

    manager = RepoManager(
        config=RepoConfig(url="git@github.com:acme/mobile-app.git", local_name="mobile-app"),
        workspace_root=tmp_path,
        run_command=run,
    )

    with pytest.raises(RepoManagerError, match="worktree path exists but is not a git worktree"):
        manager.prepare_worktree(linear_identifier="MOB-123", summary="Checkout crash")

    assert commands == [
        [
            "git",
            "-C",
            str(repo_path),
            "remote",
            "get-url",
            "origin",
        ],
        [
            "git",
            "-C",
            str(repo_path),
            "fetch",
            "--prune",
            "origin",
        ],
        ["git", "-C", str(repo_path), "status", "--porcelain"],
        ["git", "-C", str(repo_path), "rev-parse", "origin/main"],
    ]


def test_repo_manager_rejects_existing_worktree_file(tmp_path):
    repo_path = tmp_path / "repos" / "mobile-app"
    repo_path.mkdir(parents=True)
    stale_path = tmp_path / "worktrees" / "monica-MOB-123-checkout-crash"
    stale_path.parent.mkdir(parents=True)
    stale_path.write_text("not a worktree")

    def run(cmd: list[str], cwd: Path | None = None) -> str:
        if cmd == ["git", "-C", str(repo_path), "rev-parse", "origin/main"]:
            return "abc1234\n"
        return ""

    manager = RepoManager(
        config=RepoConfig(url="git@github.com:acme/mobile-app.git", local_name="mobile-app"),
        workspace_root=tmp_path,
        run_command=run,
    )

    with pytest.raises(RepoManagerError, match="worktree path exists but is not a directory"):
        manager.prepare_worktree(linear_identifier="MOB-123", summary="Checkout crash")


def test_repo_manager_error_includes_command_and_stderr(tmp_path):
    manager = RepoManager(
        config=RepoConfig(url="git@github.com:acme/mobile-app.git", local_name="mobile-app"),
        workspace_root=tmp_path,
    )

    try:
        manager._default_run(["git", "definitely-not-a-command"], None)
    except RepoManagerError as exc:
        message = str(exc)
    else:  # pragma: no cover - git invariant
        raise AssertionError("expected RepoManagerError")

    assert "git definitely-not-a-command" in message
    assert "stderr:" in message or "stdout:" in message


def test_repo_manager_timeout_raises_readable_error(tmp_path, monkeypatch):
    manager = RepoManager(
        config=RepoConfig(url="git@github.com:acme/mobile-app.git", local_name="mobile-app"),
        workspace_root=tmp_path,
        timeout_seconds=1,
    )

    def timeout_run(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=["git", "fetch"], timeout=1)

    monkeypatch.setattr(subprocess, "run", timeout_run)

    with pytest.raises(RepoManagerError, match="command timed out") as exc_info:
        manager._default_run(["git", "fetch"], None)

    assert "git fetch" in str(exc_info.value)
    assert "1s" in str(exc_info.value)


def test_repo_manager_missing_executable_raises_readable_error(tmp_path, monkeypatch):
    manager = RepoManager(
        config=RepoConfig(url="git@github.com:acme/mobile-app.git", local_name="mobile-app"),
        workspace_root=tmp_path,
    )

    def missing_run(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", missing_run)

    with pytest.raises(RepoManagerError, match="executable not found: git"):
        manager._default_run(["git", "fetch"], None)
