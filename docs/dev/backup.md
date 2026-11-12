# Backups, the key-ring backup and the restore drill

`deploy/backup/ownvitals-backup` and `deploy/backup/ownvitals-restore-drill` implement
PLAN-operations §9.2 "Backups / DR" (RPO 24 h, RTO 4 h, a quarterly drill) and the M2
monitoring hook. The decisions (age, key custody, retention) are [ADR 0017](../adr/0017-backups.md).

**Status: IMPLEMENTED, tested with synthetic data only.** Nothing is installed on the home
server, no target is chosen and no drill has run there. Those are the owner's steps below
(`docs/status.md`, "Blocked on the owner"). Agents never run these scripts against the server.

## Commands

| Command | Does | Exit |
|---|---|---|
| `ownvitals-backup db` | `pg_dumpall --roles-only --no-role-passwords` and `pg_dump --format=custom` from the compose `db` service, each piped through `age` to the db recipients, into `DB_TARGET/ownvitals-db-<UTC>/` (`roles.sql.age`, `db.dump.age`); then keeps the newest `DB_RETAIN` sets, never removing the set it just made (a clock stepped back) | 0 written; 1 failed (nothing left behind); 64 usage or configuration; 75 another backup runs |
| `ownvitals-backup keys` | the key-ring files of `SECRETS_DIR` (`connection_kek*`, `webhook_seed*`, `webhook_pepper*`) as one tar through `age` to the keys recipients, into `KEYS_TARGET/ownvitals-keys-<UTC>.tar.age`; never removes one | as above |
| `ownvitals-backup --check` | the monitoring hook: fails when the newest db set is missing, incomplete or older than **26 h**, when `SECRETS_DIR` has no `connection_kek`, or when a key-ring file's change time (`ctime`, which `cp -p` cannot set back) is not older than the newest key backup | 0 healthy; 1 not; 64 configuration |
| `ownvitals-restore-drill <set> <identity>` | restores one db set into a throwaway Postgres and checks it (below) | 0 passed within the RTO; 1 failed; 64 usage |

Every run prints one line per result, `ok    <what>: ...` or `FAIL  <what>: ...`, and never a key,
a recipient file's content, a dump or a tool's own output (an error of `pg_dump` or
`pg_restore` can quote a row). The drill also prints `count <table> <n>` for every table: counts
only.

Every run refuses (exit 64) when `DB_TARGET`, `KEYS_TARGET` and `SECRETS_DIR` are not three
separate directories (symlinks followed, none inside another, none of them `/`), when a recipients file holds
anything but `age1...` recipients (an identity is refused without being echoed), or when the
db and keys recipients share a key.

## Configuration

`/etc/ownvitals-backup/backup.conf` (root, `0600`), `KEY=value` lines, parsed and never sourced:

```ini
APP_DIR=/opt/ownvitals                       # the compose project directory (compose.yaml)
PROJECT=ownvitals                            # the compose project name
SECRETS_DIR=/opt/ownvitals/secrets           # the live key ring
DB_TARGET=/mnt/backup-db/ownvitals           # machine A
DB_RECIPIENTS=/etc/ownvitals-backup/db.recipients
DB_RETAIN=30                                 # db sets kept (1 to 999)
KEYS_TARGET=/mnt/backup-keys/ownvitals       # machine B (or at least another share)
KEYS_RECIPIENTS=/etc/ownvitals-backup/keys.recipients
```

The recipients files hold public keys only, one per line; they are not secret. An unknown key, a
relative path, a `.` or `..` component or a doubled slash is refused.

## Install (the owner's steps, on 192.168.1.222)

Nothing here is done by an agent.

**1. Keys, on your own machine, never on the server.** Install age (`apt install age`, or the
release for your OS) and make two keypairs:

```bash
umask 077
age-keygen -o ownvitals-db.identity      # prints "Public key: age1..." (the db recipient)
age-keygen -o ownvitals-keys.identity    # the keys recipient
```

Keep each identity in two places, apart from each other and from the server (for example the
db identity on the machine that runs the drill plus a USB stick in a drawer; the keys identity
offline only, plus a paper copy). Losing the db identity loses every dump; losing the keys
identity loses the key backups, and without the KEK every user reconnects and re-enrols (T33).

**2. On the server:** `sudo apt install age`, then

```bash
sudo install -o root -g root -m 0755 deploy/backup/ownvitals-backup /usr/local/bin/ownvitals-backup
sudo install -d -o root -g root -m 0700 /etc/ownvitals-backup
sudoedit /etc/ownvitals-backup/db.recipients      # the db public key, one line
sudoedit /etc/ownvitals-backup/keys.recipients    # the keys public key, one line
sudoedit /etc/ownvitals-backup/backup.conf        # as above
```

Never copy an identity to the server.

**3. The targets.** Mount the db target from one machine and the key target from another (or at
least another share with other credentials), for example with NFS or SMB entries in
`/etc/fstab` using `nofail`. The script checks that the directories exist and are separate; it
cannot tell where a mount comes from. Then run `sudo ownvitals-backup db`,
`sudo ownvitals-backup keys` and `sudo ownvitals-backup --check` once by hand.

**4. The schedule.** A systemd timer (or root's cron) runs the db backup nightly and the check
each morning; the output goes to the journal:

```ini
# /etc/systemd/system/ownvitals-backup.service
[Unit]
Description=ownvitals nightly encrypted database backup
[Service]
Type=oneshot
ExecStart=/usr/local/bin/ownvitals-backup db

# /etc/systemd/system/ownvitals-backup.timer
[Timer]
OnCalendar=*-*-* 03:17:00
Persistent=true
[Install]
WantedBy=timers.target
```

`sudo systemctl enable --now ownvitals-backup.timer`. Run `ownvitals-backup keys` by hand
after every change to the key ring (a new KEK, a rotation, `keys wipe`); `--check` fails until you
do.

**5. Monitoring.** Wire these into whatever alerting you use (Uptime Kuma, healthchecks.io, a
cron mail, a Home Assistant sensor):

- `ownvitals-backup --check`, for example hourly or each morning: exit 1 means alert. With
  healthchecks.io: `ownvitals-backup --check && curl -fsS -m 10 <your ping URL>` (the ping is
  your alerting's outbound call, not the app's).
- `GET http://192.168.1.222:8096/api/health`: liveness, never touches the database.
- `GET http://192.168.1.222:8096/api/ready`: 200 when the database answers and the key ring is
  verified, 503 (no detail) otherwise.

## The quarterly restore drill (the owner's step)

On the machine that holds the db identity (not the server), with Docker, age and a checkout of
`main`:

```bash
deploy/backup/ownvitals-restore-drill /mnt/backup-db/ownvitals/ownvitals-db-<UTC> ~/ownvitals-db.identity
```

It starts `postgres:17.11` (the digest of `compose.yaml`) in its own compose project
`ownvitals-drill-<random>` (internal network, no published port, its own volume), restores the
roles and the dump, prints:

- `ok    alembic: current <revision>`: the revision in `alembic_version` (what `alembic current`
  prints); compare it with the release that made the backup;
- `count <table> <n>` for every table and `ok    counts: <tables> tables, <rows> rows`;
- `ok    rls: <n> owned tables forced; ownvitals_app without a user sees 0 rows in <n>`: every
  table with an `owner_id` keeps forced row-level security, and the app role without
  `app.user_id` sees none of their rows;
- `ok    rto: restored and checked in <s>s (RTO 14400s)`;

and removes the project and its volume (`ok    teardown: ...`; on failure it prints the
`docker compose -p ... down -v` to run). It refuses an identity readable by others or kept in
the backup directory. Record the date, the set, the elapsed time and the result (exit 0) in
`docs/status.md` (shape only: counts are fine, values never). Decrypt the newest key backup too
(`age -d -i ~/ownvitals-keys.identity <file> | tar -t`) to check that it opens and lists the
expected files.

## Restoring for real

1. A fresh host with the repository's `compose.yaml`, new `secrets/` passwords and the database
   volume empty: `docker compose up -d db` (the init script creates the three login roles).
2. The key ring: `age -d -i ownvitals-keys.identity ownvitals-keys-<UTC>.tar.age | tar -x -C
   /opt/ownvitals/secrets` (root, `0600` files).
3. The roles: `age -d -i ownvitals-db.identity roles.sql.age | grep -vx 'CREATE ROLE postgres;' |
   docker compose exec -T -u postgres db psql -v ON_ERROR_STOP=0 -d postgres` (roles the init
   script made report "already exists"; the dump has no passwords, so the init passwords stay).
4. The database: `age -d -i ownvitals-db.identity db.dump.age | docker compose exec -T -u postgres
   db pg_restore --exit-on-error --single-transaction --dbname=ownvitals`.
5. Deploy the release that made the backup (or a newer one) through the deploy mechanism
   ([deploy.md](deploy.md)); `db-functions` reinstalls the functions, `migrate` moves the schema
   forward.

## Tests

`backend/tests/test_backup_*.py`, in the backend-tests shards (a change under `deploy/` runs
them). CI installs `age` from Ubuntu's archive; Docker is on the runner. Without age or Docker a
test fails in CI and is skipped elsewhere.

| File | Covers |
|---|---|
| `test_backup_harness.py` | `BackupHost` (a copy of each script with its installation lines pointed into a temporary directory, throwaway age keypairs made at runtime, a fake docker for the unit tests); the scripts as files (`#!/bin/bash -p`, LF, mode 100755, `bash -n`), the production installation lines, the drill's image equal to `compose.yaml`'s, no age identity in any tracked file |
| `test_backup_scripts.py` | with the fake docker: a db set only the db identity opens, retention, a failed dump leaving nothing and showing no tool output, a leftover partial set, the lock; the key backup holding exactly the key ring, only in `KEYS_TARGET`, only for the keys identity; the db and key backups never sharing a path; every non-separate target (same, nested either way, a symlink, beside the live keys) refused for every command; a shared recipient and an identity in a recipients file refused; bad configuration lines; `--check` on no backup, fresh backups, 25.5 h / 26.5 h / 9 days, an incomplete set, a key file newer than the key backup; no key value or identity in any output; #469: `/` as any of the three directories, `--check` with no key ring, a key installed with an old mtime, retention after a clock step, the drill's refusal of an identity beside the backups or readable by others |
| `test_backup_roundtrip.py` | the real round trip: a compose Postgres of the production image with the init roles, migrated to head with the db-functions scripts and synthetic users and owned rows; `ownvitals-backup db`; the drill restores it with every table's count equal to the source's, Alembic at head, the RLS check passing and the project and volume removed; the wrong identity fails and still tears down; a dump of a table that lost forced RLS fails the drill |

Run them with `uv run pytest -q tests/test_backup_*.py` from `backend/` (needs age, and Docker
for the round trip).
