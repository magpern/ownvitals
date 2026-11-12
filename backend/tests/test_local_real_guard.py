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
]
ALLOWED = [
    ".env.example",
    "docs/dev/local-real-account.md",
    "scripts/local-real-guard.sh",
    "backend/src/ownvitals/config_secrets.py",
    "backend/migrations/versions/0001_initial.py",
    "docker/postgres/init/10-roles.sql",
    "backend/tests/fixtures/synthetic_local_shapes.json",
]


IDENTITY = ("-c", "user.name=t", "-c", "user.email=t@example.invalid")


def run(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run(  # noqa: S603 - our own script, fixed arguments
        [BASH, str(SCRIPT), *args], cwd=cwd, capture_output=True, text=True, check=False
    )


def git(repo: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603 - git on a throwaway repository
        ["git", "-C", str(repo), *args],  # noqa: S607
        check=True,
        capture_output=True,
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
