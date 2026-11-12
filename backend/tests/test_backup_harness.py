"""A throwaway installation of ``deploy/backup/ownvitals-backup`` and
``ownvitals-restore-drill`` for their tests (``BackupHost``), and checks of the scripts as files.

``BackupHost`` copies a script into a temporary directory and rewrites its installation lines
(and nothing else) to point there: its configuration, its lock and its PATH. The keypairs are
generated with ``age-keygen`` in the test's temporary directory and never leave it; nothing in
the repository is a key. Every key file, dump and row is synthetic.

age, tar and flock are needed (``need``): a test that lacks one fails in CI (``CI=true``) and
is skipped anywhere else, like the database tests (``dbtools.unavailable``).
"""

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from dbtools import unavailable

ROOT = Path(__file__).resolve().parents[2]
BACKUP = ROOT / "deploy" / "backup" / "ownvitals-backup"
DRILL = ROOT / "deploy" / "backup" / "ownvitals-restore-drill"
SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
# The installation lines and their production values (docs/dev/backup.md, "Install").
BACKUP_INSTALL = {
    "CONF_FILE": "/etc/ownvitals-backup/backup.conf",
    "LOCK_FILE": "/run/lock/ownvitals-backup.lock",
    "SAFE_PATH": SAFE_PATH,
}
DRILL_INSTALL = {"SAFE_PATH": SAFE_PATH}
# The project's real identity format, assembled so this file never holds a match itself.
IDENTITY_RE = re.compile("AGE-SECRET-" + r"KEY-1[0-9A-Z]{58}")
RECIPIENT_RE = re.compile(r"^age1[02-9ac-hj-np-z]{58}$")

# docker for the unit tests: answers exactly the two dumps the backup runs, with synthetic
# bytes, and records its arguments. A flag file beside it makes a dump fail after writing a
# synthetic "row" to stderr, which the script must never show.
FAKE_DOCKER = """#!/bin/bash
here=$(cd "$(dirname "$0")" && pwd)
printf '%s\\n' "$*" >> "$here/docker.log"
prefix="compose --project-directory {app} -f {app}/compose.yaml -p {project} exec -T -u postgres db"
case "$*" in
  "$prefix pg_dumpall --roles-only --no-role-passwords")
    if [[ -e $here/fail-roles ]]; then echo "synthetic_row_value_42" >&2; exit 1; fi
    printf -- '-- synthetic roles dump\\nCREATE ROLE ownvitals_app;\\n' ;;
  "$prefix pg_dump --format=custom --dbname=ownvitals")
    if [[ -e $here/fail-dump ]]; then echo "synthetic_row_value_42" >&2; exit 1; fi
    printf 'PGDMP synthetic dump\\n' ;;
  *) echo "fake docker: unexpected arguments" >&2; exit 64 ;;
esac
"""


def need(*tools: str) -> None:
    for tool in tools:
        if shutil.which(tool) is None:
            unavailable(f"{tool} is not installed; the backup tests need it")


def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    """One process of the test's own: the first word resolved on PATH, no shell."""
    resolved = shutil.which(argv[0])
    if resolved is None:
        unavailable(f"{argv[0]} is not installed; the backup tests need it")
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("timeout", 120)
    return subprocess.run([resolved, *argv[1:]], **kwargs)  # noqa: S603


def install(script: Path, target: Path, values: dict[str, str]) -> Path:
    """A copy of ``script`` at ``target`` whose installation lines hold ``values``."""
    text = script.read_text(encoding="utf-8")
    for key, value in values.items():
        pattern = re.compile(rf"^readonly {key}=.*$", re.MULTILINE)
        line = f"readonly {key}={value}"
        text, count = pattern.subn(lambda _m, line=line: line, text)
        assert count == 1, key
    target.write_bytes(text.encode("utf-8"))
    target.chmod(0o755)
    return target


def keypair(directory: Path, name: str) -> tuple[Path, str]:
    """A throwaway age identity (mode 600) and its recipient."""
    need("age-keygen")
    identity = directory / f"{name}.identity"
    run(["age-keygen", "-o", str(identity)], check=True)
    identity.chmod(0o600)
    recipient = run(["age-keygen", "-y", str(identity)], check=True, text=True).stdout.strip()
    assert RECIPIENT_RE.match(recipient)
    return identity, recipient


def decrypt(identity: Path, archive: Path) -> bytes:
    return run(["age", "--decrypt", "--identity", str(identity), str(archive)], check=True).stdout


@dataclass
class BackupHost:
    """A backup installation in ``root``: the app directory, the live secrets, the two backup
    targets on their own "machines", the recipients and the configuration."""

    root: Path
    fake_docker: bool = True
    project: str = "ownvitals"
    conf_lines: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        need("age", "tar", "flock")
        self.app = self.root / "opt" / "ownvitals"
        self.secrets = self.app / "secrets"
        self.db_target = self.root / "mnt" / "backup-a" / "ownvitals-db"
        self.keys_target = self.root / "mnt" / "backup-b" / "ownvitals-keys"
        self.etc = self.root / "etc"
        self.private = self.root / "private"  # stands in for the owner's other machine
        self.bin = self.root / "bin"
        for path in (self.secrets, self.db_target, self.keys_target, self.etc, self.bin):
            path.mkdir(parents=True, exist_ok=True)
        self.private.mkdir(mode=0o700)
        self.db_identity, db_recipient = keypair(self.private, "db")
        self.keys_identity, keys_recipient = keypair(self.private, "keys")
        self.db_recipients = self.etc / "db.recipients"
        self.keys_recipients = self.etc / "keys.recipients"
        self.db_recipients.write_text(f"# synthetic test recipient\n{db_recipient}\n")
        self.keys_recipients.write_text(f"{keys_recipient}\n")
        self.conf = self.etc / "backup.conf"
        if not self.conf_lines:
            self.conf_lines = [
                "# synthetic test configuration",
                f"APP_DIR={self.app}",
                f"PROJECT={self.project}",
                f"SECRETS_DIR={self.secrets}",
                f"DB_TARGET={self.db_target}",
                f"DB_RECIPIENTS={self.db_recipients}",
                f"KEYS_TARGET={self.keys_target}",
                f"KEYS_RECIPIENTS={self.keys_recipients}",
            ]
        self.write_conf(self.conf_lines)
        path = os.environ.get("PATH", SAFE_PATH)
        if self.fake_docker:
            docker = self.bin / "docker"
            docker.write_text(FAKE_DOCKER.format(app=self.app, project=self.project))
            docker.chmod(0o755)
            path = f"{self.bin}:{path}"
        self.lock = self.root / "backup.lock"
        self.backup = install(
            BACKUP,
            self.root / "ownvitals-backup",
            {"CONF_FILE": str(self.conf), "LOCK_FILE": str(self.lock), "SAFE_PATH": path},
        )
        self.drill = install(DRILL, self.root / "ownvitals-restore-drill", {"SAFE_PATH": path})

    def write_conf(self, lines: list[str]) -> None:
        self.conf.write_text("\n".join(lines) + "\n")

    def set_conf(self, key: str, value: str) -> None:
        lines = [line for line in self.conf_lines if not line.startswith(f"{key}=")]
        self.conf_lines = [*lines, f"{key}={value}"]
        self.write_conf(self.conf_lines)

    def run(self, *args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        return run([str(self.backup), *args], text=True, timeout=timeout)

    def run_drill(self, *args: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
        return run([str(self.drill), *args], text=True, timeout=timeout)

    def sets(self) -> list[Path]:
        return sorted(p for p in self.db_target.iterdir() if p.name.startswith("ownvitals-db-"))

    def key_archives(self) -> list[Path]:
        return sorted(self.keys_target.glob("ownvitals-keys-*.tar.age"))


def lines_of(result: subprocess.CompletedProcess[str]) -> list[str]:
    return (result.stdout + result.stderr).splitlines()


# --- The scripts as files --------------------------------------------------------------------


@pytest.mark.parametrize("script", [BACKUP, DRILL], ids=lambda p: p.name)
def test_the_script_is_bash_p_lf_and_executable(script: Path) -> None:
    data = script.read_bytes()
    assert data.startswith(b"#!/bin/bash -p\n")
    assert b"\r" not in data
    staged = run(
        ["git", "ls-files", "-s", "--", str(script.relative_to(ROOT))],
        cwd=ROOT,
        text=True,
        check=True,
    ).stdout
    assert staged.startswith("100755 "), "commit the script with mode 100755"
    run(["bash", "-n", str(script)], check=True)


@pytest.mark.parametrize(
    ("script", "values"),
    [(BACKUP, BACKUP_INSTALL), (DRILL, DRILL_INSTALL)],
    ids=["backup", "drill"],
)
def test_the_installation_lines_hold_the_production_values(
    script: Path, values: dict[str, str]
) -> None:
    text = script.read_text(encoding="utf-8")
    for key, value in values.items():
        assert re.findall(rf"^readonly {key}=(.*)$", text, re.MULTILINE) == [value]


def test_the_drill_runs_compose_yamls_db_image() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    images = re.findall(r"^\s+image: (postgres:\S+)$", compose, re.MULTILINE)
    assert len(images) == 1
    drill = re.findall(
        r"^readonly DB_IMAGE=(\S+)$", DRILL.read_text(encoding="utf-8"), re.MULTILINE
    )
    assert drill == images


def test_no_age_identity_is_tracked_in_the_repository() -> None:
    """gitleaks scans the commits; this names the one key format the backups use."""
    tracked = run(["git", "ls-files", "-z"], cwd=ROOT, check=True).stdout.split(b"\0")
    found = []
    for name in tracked:
        if not name:
            continue
        path = ROOT / name.decode()
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        if IDENTITY_RE.search(path.read_bytes().decode("utf-8", "replace")):
            found.append(name.decode())
    assert found == []
