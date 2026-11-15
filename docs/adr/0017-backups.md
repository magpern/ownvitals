# ADR 0017: Backups, key custody and the restore drill

Status: Proposed. Issue #463 (PLAN-operations §9.2 "Backups / DR", PLAN §11 M2, threat model
T33 and T61).

## Context

The M2 real-data gate needs "backups with a passing restore drill". §9.2 asks for a nightly
encrypted `pg_dump` and a KEK backup kept separately, on another machine, with RPO 24 h, RTO 4 h
and a quarterly restore drill. The database holds health data in plaintext columns (accepted
risk 2) and enveloped tokens, seeds and the client secret (ADR 0004). The key ring (KEKs, the
webhook seed and pepper) opens every envelope. A thief with a dump and the key ring has everything;
a thief with only the dump has the health data but no credential.

The app host (192.168.1.222) is shared with other software (T60). Whatever runs the nightly
backup there must not be able to read old backups: a host compromise should not also expose every
dump on the backup machine.

## Decision

**1. Encryption tool: age (X25519 recipients).** Each backup is encrypted on the host with
`age --recipients-file` to one or more public keys (`age1...`). The host holds only recipients;
the identities (private keys) stay on the owner's machines, offline. age was chosen over GnuPG
because it has one key type, no keyring, no trust model and no configuration to get wrong; a
recipients file is a plain list the script can validate line by line; and Ubuntu ships it as a
signed package (`apt install age`). GnuPG would also work but needs a keyring per user,
`--trust-model` handling and key-expiry care, and its import step makes "the host holds no
private key" harder to check. The script accepts only `age1` X25519 recipients and refuses a
file that holds anything else, an identity included (without echoing it).

**2. Key custody: two keypairs, disjoint.** The database backups are encrypted to the *db*
recipients, the key-ring backup to the *keys* recipients, and the script refuses a recipient that
appears in both. One leaked identity therefore opens either the dumps or the key ring, never both.
The owner keeps each identity on a different medium (for example the db identity on the drill
machine and a printed or USB copy, the keys identity offline only), with a second copy of each in
a separate place. Losing the db identity loses the dumps; losing the keys identity loses the key
backups (T33): both are written down in the runbook.

**3. Separation of the two backups.** `ownvitals-backup db` and `ownvitals-backup keys` are
separate commands writing separate files to separate directories (`DB_TARGET`, `KEYS_TARGET`).
The script refuses to run when the two targets or the live secrets directory resolve (symlinks
followed) to the same directory or one inside another. The owner mounts the two targets from
different machines or shares; the script cannot see where a mount comes from, so that part is a
runbook step.

**4. What a backup holds.** A db backup set is a directory `ownvitals-db-<UTC time>/` with
`roles.sql.age` (`pg_dumpall --roles-only --no-role-passwords`: the roles several migrations
create, without password hashes) and `db.dump.age` (`pg_dump --format=custom` of `ownvitals`),
written under a `.partial-` name and renamed once both are complete. The key backup is one
`ownvitals-keys-<UTC time>.tar.age` holding exactly the key-ring files (`connection_kek*`,
`webhook_seed*`, `webhook_pepper*`). Other secrets (database passwords, `app_secret_key`, the
Anthropic key) are not backed up: they are recreated after a disaster (new passwords; a new app
secret signs everyone out; a new API key).

**5. Retention.** The newest `DB_RETAIN` db sets are kept (default 30, one a night: a month).
Older dumps age out (accepted risk 13). Key backups are never removed by the script: they are
small and written only when the ring changes, and a retired KEK may still be needed to open a
dump made before it was retired. The owner prunes them by hand after every dump that needs an
older key has aged out, and after a compromise (T32) treats every older backup as compromised.

**6. Monitoring.** `ownvitals-backup --check` exits 1 when the newest db set is missing,
incomplete or older than 26 h (RPO plus two hours), or when no key backup is newer than every
key-ring file. The owner wires it, and the existing `/api/health` and `/api/ready`, into their
alerting.

**7. The restore drill.** `ownvitals-restore-drill <set> <identity>` runs where the identity is,
not on the app host. It starts a Postgres of the production image (the same digest as
`compose.yaml`, checked by a test) in its own compose project with an internal network, no
published port and its own volume; restores the roles and the dump; prints the Alembic revision
(`alembic_version`), the row count of every table and an RLS check (every table with an
`owner_id` keeps forced row-level security, and `ownvitals_app` without `app.user_id` sees no
owned row); reports the elapsed time against the RTO; and removes the project and its volume. It
prints counts only, never a value, and no tool's output (a restore error can quote a row).

## Alternatives

- **GnuPG** (above). Also the default on the runner and the server, but more to get wrong.
- **Symmetric encryption with a passphrase on the host** (`age -p`, `gpg -c`, `openssl enc`): the
  host would hold the secret that decrypts every backup. Rejected.
- **Encrypting the backup with a KEK of the ring**: ties the two together, so the KEK backup and
  the db backup could no longer be kept apart. Rejected.
- **restic or borg** with repository encryption: deduplication and pruning for free, but the
  repository key (or its passphrase) lives on the host, and the tools are larger. Could be
  revisited with the VPS move before M8.
- **Running Alembic from the app image in the drill**: it needs the image and an owner
  password inside the throwaway; reading `alembic_version` gives the same revision that
  `alembic current` prints without either.

## Consequences

- The host can write backups but never read one; a restore needs an identity from the owner.
- The drill runs on any machine with Docker, age and the repository's scripts.
- A roles dump without passwords means a restore sets new login passwords (the init script or
  `ALTER ROLE`), which the runbook says.
- The tests (`backend/tests/test_backup_*.py`) generate throwaway keypairs at runtime; no key is
  committed. The round trip needs Docker and age, both present in CI.
- Nothing here is verified on the home server: the target, the schedule and the drill there are
  the owner's (`docs/status.md`, "Blocked on the owner").
