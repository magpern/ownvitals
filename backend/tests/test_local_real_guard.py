"""``scripts/local-real-guard.sh`` and ``.gitignore`` keep the owner's local real-account work out
of git (#450, AGENTS.md "Health data", ``docs/dev/local-real-account.md``).

Runs on the checkout (nothing forbidden is tracked) and on throwaway repositories with synthetic
file names only; no file here holds real data and nothing touches a network.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "local-real-guard.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="needs bash")

FORBIDDEN = [
    ".env",
    ".env.local",
    "backend/.env.LOCAL",
    "secrets/withings_client_secret",
    "deploy/secrets/x",
    "local-real/REAL-ACCOUNT",
    "local-real/probe.json",
    "backend/local-real/notes.txt",
    "probe-output.txt",
    "db.dump",
    "ownvitals.PGDUMP",
    "data.backup",
    "old.bak",
    "x.sql.gz",
    "x.sql.zst",
    "pg_dump_2026.sql",
    "ownvitals-dump.sql",
    ".env.example.local",  # a near miss of the allowed .env.example
    "backend/.env.example.bak",
    ".envrc",
    "frontend/.envrc",
    ".envrc.local",
    "secret/connection_kek",
    "deploy/secret/x",
    "backup.sql",
    "backend/real.sql",
    "docker/postgres/stray.sql",
    "docker/postgres/init/dump.sql",
    "export.tar",
    "export.tar.gz",
    "export.tgz",
    "ownvitals-export.zip",
    "export.csv",
    "local/MEASURES.CSV",
    "session.har",
]
ALLOWED = [
    ".env.example",
    "docs/dev/local-real-account.md",
    "scripts/local-real-guard.sh",
    "backend/src/ownvitals/config_secrets.py",
    "backend/migrations/versions/0001_initial.py",
    "docker/postgres/init/10-roles.sql",
    "backend/tests/fixtures/synthetic_local_shapes.json",
    "docker/postgres/withings/rate.sql",
    "backend/tests/fixtures/synthetic_shapes.csv",
    "backend/src/ownvitals/crypto/secret_box.py",
    "docs/environment.md",
    "docs/dev/envrc.md",
]


IDENTITY = ("-c", "user.name=t", "-c", "user.email=t@example.invalid")


def run(
    *args: str, cwd: Path = ROOT, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run(  # noqa: S603 - our own script, fixed arguments
        [BASH, str(SCRIPT), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **(env or {})},
    )


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - git on a throwaway repository
        ["git", "-C", str(repo), *IDENTITY, *args],  # noqa: S607
        check=check,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    shutil.copy(ROOT / ".gitignore", root / ".gitignore")
    (root / "scripts").mkdir()
    shutil.copy(SCRIPT, root / "scripts" / SCRIPT.name)
    return root


def touch(repo: Path, rel: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("synthetic: true\n")


def test_nothing_forbidden_is_tracked_in_this_checkout() -> None:
    result = run("tracked")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("rel", FORBIDDEN)
def test_each_forbidden_path_is_refused_and_ignored(repo: Path, rel: str) -> None:
    assert run("paths", rel).returncode == 1
    touch(repo, rel)
    ignored = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "check-ignore", "-q", rel],  # noqa: S607
        check=False,
    )
    if rel == rel.lower():  # .gitignore is case-sensitive on Linux; the guard is not
        assert ignored.returncode == 0, f"{rel} is not git-ignored"
    git(repo, "add", "-f", rel)  # forcing past .gitignore is what the guard is for
    staged = run("staged", cwd=repo)
    assert staged.returncode == 1
    assert rel in staged.stderr
    assert run("tracked", cwd=repo).returncode == 1


@pytest.mark.parametrize("rel", ALLOWED)
def test_near_misses_pass(repo: Path, rel: str) -> None:
    assert run("paths", rel).returncode == 0, rel
    touch(repo, rel)
    git(repo, "add", "-f", rel)
    assert run("staged", cwd=repo).returncode == 0
    assert run("tracked", cwd=repo).returncode == 0


def test_a_real_account_worktree_never_commits_or_pushes(repo: Path) -> None:
    touch(repo, "docs/note.md")
    git(repo, "add", "docs/note.md")
    assert run("pre-push", cwd=repo).returncode == 0
    assert run("staged", cwd=repo).returncode == 0
    touch(repo, "local-real/REAL-ACCOUNT")  # the marker, git-ignored
    push = run("pre-push", cwd=repo)
    assert push.returncode == 1
    assert "never pushes" in push.stderr
    assert run("staged", cwd=repo).returncode == 1


def test_install_writes_both_hooks_and_keeps_a_foreign_one(repo: Path) -> None:
    assert run("install", cwd=repo).returncode == 0
    hooks = repo / ".git" / "hooks"
    assert '"$guard" staged' in (hooks / "pre-commit").read_text()
    assert '"$guard" pre-push' in (hooks / "pre-push").read_text()
    assert run("install", cwd=repo).returncode == 0  # ours: replaced
    (hooks / "pre-commit").write_text("#!/bin/sh\necho someone else's hook\n")
    again = run("install", cwd=repo)
    assert again.returncode == 1
    assert "not ours" in again.stderr
    assert "someone else" in (hooks / "pre-commit").read_text()


def test_the_installed_hook_blocks_a_commit(repo: Path) -> None:
    assert run("install", cwd=repo).returncode == 0
    touch(repo, "secrets/connection_kek")
    git(repo, "add", "-f", "secrets/connection_kek")
    commit = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), *IDENTITY, "commit", "-q", "-m", "x"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
    )
    assert commit.returncode != 0
    assert "refused: secrets/connection_kek" in commit.stderr


def test_no_tracked_file_is_git_ignored() -> None:
    """The ignore rules for dumps and exports leave every tracked source file (docker/postgres SQL
    included) visible to git."""
    listed = subprocess.run(  # noqa: S603
        ["git", "-C", str(ROOT), "ls-files", "-ci", "--exclude-standard"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    assert listed.stdout == ""


def test_the_hook_refuses_when_the_guard_is_missing_in_a_real_worktree(repo: Path) -> None:
    assert run("install", cwd=repo).returncode == 0
    (repo / "scripts" / SCRIPT.name).unlink()  # as in a checkout from before the guard
    touch(repo, "docs/a.md")
    git(repo, "add", "docs/a.md")
    assert git(repo, "commit", "-q", "-m", "a", check=False).returncode == 0
    touch(repo, "local-real/REAL-ACCOUNT")
    touch(repo, "docs/b.md")
    git(repo, "add", "docs/b.md")
    commit = git(repo, "commit", "-q", "-m", "b", check=False)
    assert commit.returncode != 0
    assert "without the guard script" in commit.stderr
    hooks = repo / ".git" / "hooks"
    push = subprocess.run(  # noqa: S603 - the installed hook itself
        [str(hooks / "pre-push")], cwd=repo, capture_output=True, text=True, check=False
    )
    assert push.returncode != 0


def test_seal_breaks_pushes_from_the_real_worktree_only(repo: Path, tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    touch(repo, "docs/a.md")
    git(repo, "add", "docs/a.md")
    git(repo, "commit", "-q", "-m", "a")
    real = tmp_path / "real"
    git(repo, "worktree", "add", "-q", "--detach", str(real))
    unmarked = run("seal", cwd=real)
    assert unmarked.returncode == 1
    assert "seal only the real-account worktree" in unmarked.stderr
    touch(real, "local-real/REAL-ACCOUNT")
    assert run("seal", cwd=real).returncode == 0
    blocked = git(real, "push", "--no-verify", "origin", "HEAD:refs/heads/x", check=False)
    assert blocked.returncode != 0
    assert "local-real-no-push" in blocked.stderr
    assert git(remote, "branch", "--list", "x").stdout == ""
    # The other worktrees keep their push URL.
    assert git(repo, "push", "-q", "--no-verify", "origin", "HEAD:refs/heads/y").returncode == 0
    assert git(remote, "branch", "--list", "y").stdout.strip() == "y"


def real_worktree(repo: Path, tmp_path: Path) -> tuple[Path, Path]:
    """A bare remote, one commit, and a marked linked worktree (synthetic files only)."""
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    touch(repo, "docs/a.md")
    git(repo, "add", "docs/a.md")
    git(repo, "commit", "-q", "-m", "a")
    real = tmp_path / "real"
    git(repo, "worktree", "add", "-q", "--detach", str(real))
    touch(real, "local-real/REAL-ACCOUNT")
    return remote, real


def test_seal_refuses_a_shared_pushurl_and_no_ref_reaches_the_remote(
    repo: Path, tmp_path: Path
) -> None:
    """pushurl is multi-valued: a shared one would be pushed to before the sealed one fails."""
    remote, real = real_worktree(repo, tmp_path)
    git(repo, "config", "remote.origin.pushurl", str(remote))
    refused = run("seal", cwd=real)
    assert refused.returncode == 1
    assert "pushurl is set outside this worktree" in refused.stderr
    assert str(remote) not in refused.stderr + refused.stdout  # a URL could hold a token
    assert git(real, "config", "--get-all", "remote.origin.pushurl").stdout.strip() == str(remote)
    git(repo, "config", "--unset", "remote.origin.pushurl")  # as the message says
    assert run("seal", cwd=real).returncode == 0
    assert run("seal", cwd=real).returncode == 0  # sealing again is fine
    pushurls = git(real, "config", "--get-all", "remote.origin.pushurl").stdout.split()
    assert pushurls == ["local-real-no-push://this-worktree-never-pushes"]
    blocked = git(real, "push", "--no-verify", "origin", "HEAD:refs/heads/leak", check=False)
    assert blocked.returncode != 0
    assert git(remote, "for-each-ref").stdout == ""  # no ref was created


def test_seal_refuses_a_global_pushurl(repo: Path, tmp_path: Path) -> None:
    remote, real = real_worktree(repo, tmp_path)
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(f'[remote "origin"]\n\tpushurl = {remote}\n')
    refused = run("seal", cwd=real, env={"GIT_CONFIG_GLOBAL": str(gitconfig)})
    assert refused.returncode == 1
    assert "pushurl is set outside this worktree" in refused.stderr
