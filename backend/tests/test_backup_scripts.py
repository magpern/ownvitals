"""``ownvitals-backup`` with a fake docker (synthetic dumps): the db and key backups, their
separation, the recipients, retention, ``--check`` and what the script prints. The real
round trip through Postgres is ``test_backup_roundtrip.py``. Synthetic data only."""

import fcntl
import io
import os
import secrets
import subprocess
import tarfile
import time
from pathlib import Path

import pytest

from test_backup_harness import IDENTITY_RE, BackupHost, decrypt, keypair, lines_of

OLD_SETS = [f"ownvitals-db-2026010{d}T020000Z" for d in range(1, 5)]


@pytest.fixture
def host(tmp_path: Path) -> BackupHost:
    host = BackupHost(tmp_path)
    # Synthetic key ring: the current KEK, a retired one, the seed and the pepper, plus files
    # that are not key-ring files and must stay out of the key backup.
    for name in ("connection_kek", "connection_kek_kek-0", "webhook_seed", "webhook_pepper"):
        (host.secrets / name).write_text(secrets.token_hex(32) + "\n")
    (host.secrets / "postgres_password").write_text("synthetic-not-a-key\n")
    (host.secrets / "app_secret_key").write_text("synthetic-not-a-key\n")
    return host


def secret_values(host: BackupHost) -> list[str]:
    values = [p.read_text().strip() for p in host.secrets.iterdir()]
    for identity in (host.db_identity, host.keys_identity):
        values += IDENTITY_RE.findall(identity.read_text())
    return values


def assert_prints_no_secret(host: BackupHost, *outputs: str) -> None:
    for value in secret_values(host):
        for out in outputs:
            assert value not in out
    for out in outputs:
        assert "synthetic_row_value_42" not in out  # a tool's stderr is never shown
        assert not IDENTITY_RE.search(out)


def make_old_set(host: BackupHost, name: str, hours_ago: float = 0) -> Path:
    path = host.db_target / name
    path.mkdir()
    for part in ("roles.sql.age", "db.dump.age"):
        (path / part).write_bytes(b"synthetic placeholder\n")
        stamp = time.time() - hours_ago * 3600
        os.utime(path / part, (stamp, stamp))
    return path


# --- db -------------------------------------------------------------------------------------


def test_a_db_backup_is_one_encrypted_set_that_only_the_db_identity_opens(
    host: BackupHost,
) -> None:
    result = host.run("db")
    assert result.returncode == 0, result.stderr
    out = lines_of(result)
    assert len(out) == 1
    assert out[0].startswith("ok    db: ownvitals-db-")
    (backup,) = host.sets()
    assert sorted(p.name for p in backup.iterdir()) == ["db.dump.age", "roles.sql.age"]
    assert decrypt(host.db_identity, backup / "db.dump.age") == b"PGDMP synthetic dump\n"
    assert b"CREATE ROLE ownvitals_app" in decrypt(host.db_identity, backup / "roles.sql.age")
    # Encrypted on the host: the plaintext is not in the file, and the keys identity can't open it.
    assert b"PGDMP" not in (backup / "db.dump.age").read_bytes()
    with pytest.raises(subprocess.CalledProcessError):
        decrypt(host.keys_identity, backup / "db.dump.age")
    assert oct(backup.stat().st_mode & 0o777) == "0o700"
    assert (backup / "db.dump.age").stat().st_mode & 0o077 == 0
    assert list(host.keys_target.iterdir()) == []
    assert_prints_no_secret(host, result.stdout, result.stderr)
    log = (host.bin / "docker.log").read_text().splitlines()
    assert len(log) == 2  # the roles, then the database: nothing else ran in docker


def test_retention_keeps_the_newest_db_retain_sets(host: BackupHost) -> None:
    host.set_conf("DB_RETAIN", "3")
    for name in OLD_SETS:
        make_old_set(host, name, hours_ago=48)
    (host.db_target / "unrelated-file").write_text("kept\n")
    result = host.run("db")
    assert result.returncode == 0, result.stderr
    assert "3 kept, 2 removed" in result.stdout
    names = [p.name for p in host.sets()]
    assert names[:2] == OLD_SETS[2:]
    assert len(names) == 3
    assert (host.db_target / "unrelated-file").exists()


@pytest.mark.parametrize("flag", ["fail-roles", "fail-dump"])
def test_a_failed_dump_leaves_nothing_and_shows_no_tool_output(host: BackupHost, flag: str) -> None:
    (host.bin / flag).touch()
    result = host.run("db")
    assert result.returncode == 1
    assert lines_of(result)[0].startswith("FAIL  db: ")
    assert list(host.db_target.iterdir()) == []
    assert_prints_no_secret(host, result.stdout, result.stderr)


def test_a_leftover_partial_set_is_removed_and_never_counted(host: BackupHost) -> None:
    partial = host.db_target / ".partial-ownvitals-db-20260101T020000Z"
    partial.mkdir()
    (partial / "db.dump.age").write_bytes(b"synthetic truncated\n")
    assert host.run("db").returncode == 0
    assert not partial.exists()
    assert len(host.sets()) == 1


def test_a_running_backup_holds_the_lock(host: BackupHost) -> None:
    with host.lock.open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = host.run("db")
    assert result.returncode == 75
    assert "another backup is running" in result.stderr
    assert host.sets() == []


# --- keys ------------------------------------------------------------------------------------


def test_the_key_backup_holds_only_the_key_ring_and_lands_only_in_keys_target(
    host: BackupHost,
) -> None:
    result = host.run("keys")
    assert result.returncode == 0, result.stderr
    assert len(lines_of(result)) == 1
    assert "4 key files; kept separately" in result.stdout
    (archive,) = host.key_archives()
    assert list(host.db_target.iterdir()) == []  # never beside the dumps
    assert list(host.keys_target.iterdir()) == [archive]
    with tarfile.open(fileobj=io.BytesIO(decrypt(host.keys_identity, archive))) as tar:
        members = {m.name: m for m in tar.getmembers()}
        assert sorted(members) == [
            "connection_kek",
            "connection_kek_kek-0",
            "webhook_pepper",
            "webhook_seed",
        ]
        for name, member in members.items():
            extracted = tar.extractfile(member)
            assert extracted is not None
            assert extracted.read() == (host.secrets / name).read_bytes()
    with pytest.raises(subprocess.CalledProcessError):
        decrypt(host.db_identity, archive)  # the db identity never opens the keys
    assert_prints_no_secret(host, result.stdout, result.stderr, archive.read_bytes().hex())


def test_the_db_and_key_backups_never_share_a_path(host: BackupHost) -> None:
    assert host.run("db").returncode == 0
    assert host.run("keys").returncode == 0
    db_files = {p.resolve() for p in host.db_target.rglob("*")}
    key_files = {p.resolve() for p in host.keys_target.rglob("*")}
    assert db_files
    assert key_files
    assert not db_files & key_files
    assert all(host.keys_target.resolve() not in p.parents for p in db_files)
    assert all(host.db_target.resolve() not in p.parents for p in key_files)
    assert not any(p.name.startswith("ownvitals-keys-") for p in db_files)


def test_a_key_backup_without_a_kek_is_refused(host: BackupHost) -> None:
    (host.secrets / "connection_kek").unlink()
    result = host.run("keys")
    assert result.returncode == 1
    assert host.key_archives() == []


# --- separation and configuration -----------------------------------------------------------


def separation_cases(host: BackupHost) -> dict[str, tuple[str, str]]:
    link = host.root / "mnt" / "link-to-db"
    link.symlink_to(host.db_target)
    nested = host.db_target / "keys"
    nested.mkdir()
    db_in_secrets = host.secrets / "dumps"
    db_in_secrets.mkdir()
    return {
        "same directory": ("KEYS_TARGET", str(host.db_target)),
        "keys inside db": ("KEYS_TARGET", str(nested)),
        "db inside keys": ("DB_TARGET", str(host.keys_target / "db")),
        "the same through a symlink": ("KEYS_TARGET", str(link)),
        "db beside the live keys": ("DB_TARGET", str(db_in_secrets)),
        "keys are the live keys": ("KEYS_TARGET", str(host.secrets)),
    }


@pytest.mark.parametrize(
    "case",
    [
        "same directory",
        "keys inside db",
        "db inside keys",
        "the same through a symlink",
        "db beside the live keys",
        "keys are the live keys",
    ],
)
@pytest.mark.parametrize("command", ["db", "keys", "--check"])
def test_targets_that_are_not_separate_are_refused(
    host: BackupHost, case: str, command: str
) -> None:
    (host.keys_target / "db").mkdir()
    key, value = separation_cases(host)[case]
    host.set_conf(key, value)
    result = host.run(command)
    assert result.returncode == 64, result.stderr
    assert "must be separate directories" in result.stderr
    assert host.sets() == []
    assert host.key_archives() == []
    assert not list(host.db_target.rglob("*.age"))
    assert not list(host.secrets.rglob("*.age"))


def test_a_shared_recipient_is_refused(host: BackupHost) -> None:
    host.keys_recipients.write_text(host.db_recipients.read_text())
    result = host.run("keys")
    assert result.returncode == 64
    assert "share a recipient" in result.stderr
    assert host.key_archives() == []


def test_an_identity_in_a_recipients_file_is_refused_and_not_echoed(
    host: BackupHost, tmp_path: Path
) -> None:
    identity, _ = keypair(host.private, "misplaced")
    host.db_recipients.write_text(identity.read_text())
    result = host.run("db")
    assert result.returncode == 64
    assert "not an age1 recipient" in result.stderr
    assert not IDENTITY_RE.search(result.stdout + result.stderr)
    assert host.sets() == []


@pytest.mark.parametrize(
    "line",
    [
        "UNKNOWN=1",
        "DB_TARGET=relative/path",
        "DB_TARGET=/mnt/../etc",
        "DB_TARGET=/mnt//x",
        "DB_TARGET=/mnt/$(id)",
        "PROJECT=Not A Project",
        "DB_RETAIN=0",
        "DB_RETAIN=-1",
        "not a key value line",
    ],
)
def test_a_bad_configuration_line_is_refused(host: BackupHost, line: str) -> None:
    host.write_conf([*host.conf_lines, line])
    result = host.run("db")
    assert result.returncode == 64
    assert result.stderr.startswith("FAIL  config: ")
    assert host.sets() == []


@pytest.mark.parametrize("args", [[], ["db", "keys"], ["restore"], ["--check", "x"]])
def test_anything_but_the_three_commands_is_refused(host: BackupHost, args: list[str]) -> None:
    result = host.run(*args)
    assert result.returncode == 64
    assert "usage" in result.stderr


# --- --check (the monitoring hook) ----------------------------------------------------------


def test_check_fails_when_there_is_no_backup(host: BackupHost) -> None:
    result = host.run("--check")
    assert result.returncode == 1
    assert "no db backup" in result.stderr
    assert "no key backup" in result.stderr


def test_check_passes_after_fresh_backups(host: BackupHost) -> None:
    assert host.run("db").returncode == 0
    assert host.run("keys").returncode == 0
    result = host.run("--check")
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("ok    check: ") == 2


@pytest.mark.parametrize(("hours", "passes"), [(25.5, True), (26.5, False), (24 * 9, False)])
def test_check_fails_when_the_newest_db_backup_is_older_than_26_hours(
    host: BackupHost, hours: float, passes: bool
) -> None:
    assert host.run("keys").returncode == 0
    make_old_set(host, OLD_SETS[0], hours_ago=24 * 10)
    make_old_set(host, OLD_SETS[1], hours_ago=hours)
    result = host.run("--check")
    assert (result.returncode == 0) is passes, result.stdout + result.stderr
    if not passes:
        assert "more than 26 h" in result.stderr


def test_check_fails_on_an_incomplete_newest_set(host: BackupHost) -> None:
    assert host.run("keys").returncode == 0
    (make_old_set(host, OLD_SETS[0]) / "db.dump.age").unlink()
    result = host.run("--check")
    assert result.returncode == 1
    assert "incomplete" in result.stderr


def test_check_fails_when_a_key_file_changed_after_the_key_backup(host: BackupHost) -> None:
    assert host.run("db").returncode == 0
    assert host.run("keys").returncode == 0
    (archive,) = host.key_archives()
    old = time.time() - 3600
    os.utime(archive, (old, old))
    (host.secrets / "connection_kek_kek-2").write_text(secrets.token_hex(32) + "\n")
    result = host.run("--check")
    assert result.returncode == 1
    assert "changed after the newest key backup" in result.stderr
    assert_prints_no_secret(host, result.stdout, result.stderr)
