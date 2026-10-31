# Envelope encryption and the key ring

How ownvitals encrypts stored secrets and holds its keys: [ADR 0004](../adr/0004-connections-and-encryption.md)
§2 to §4 (PLAN §3 row `crypto/*`, §4.4). Built by #8; the `connections` table, `keys rewrap` and
`keys wipe` are #13's, the webhook secrets that use the seed and the pepper are M1's (#21, #22, #24).

## The pieces

| Path | What |
|---|---|
| `backend/src/ownvitals/crypto/envelope.py` | pure (no database, files or logging): AES-256-GCM `seal`/`open_sealed`/`wrap`/`unwrap`/`rewrap`, the AAD builders, `canonical_json`, the key classes and `check_value` |
| `backend/src/ownvitals/crypto/keks.py` | the key ring: key files, the `connection_keks` check, `load_keyring(factory, settings, role=...)`, `KeyRing` |
| `backend/migrations/versions/0003_connection_keks.py` | the check table; the app role gets `SELECT, INSERT` only |
| `backend/tests/crypto_scan.py` | the crypto guard (below) |

## Envelope (ADR 0004 §2)

A payload is encrypted under a **data key** (DEK, 32 random bytes) that is new for every write,
with a random 96-bit nonce; the DEK is wrapped under the KEK named by the row's `key_id`, with its
own nonce. What is stored is `Sealed(ciphertext, nonce, wrapped_dek, dek_nonce, key_id)`: a
12-byte nonce each and a 48-byte wrapped DEK. Every failure (wrong key, wrong AAD, tampered or
truncated bytes) is `DecryptFailed("decrypt_failed")`, with no cause or context.

| Value | Payload AAD | Wrap AAD |
|---|---|---|
| Connection tokens (#13) | `connection_aad(owner_id, provider, id)` = `ownvitals-conn-v1\|<owner_id>\|<provider>\|<id>` | `connection_wrap_aad(key_id, …)` = `ownvitals-dek-v1\|<key_id>\|<owner_id>\|<provider>\|<id>` |
| TOTP seed (#10) | `totp_aad(user_id)` = `ownvitals-totp-v1\|<user_id>` | `totp_wrap_aad(key_id, user_id)` = `ownvitals-totp-dek-v1\|<key_id>\|<user_id>` |

UUIDs must be `uuid.UUID` (canonical text), a provider matches `^[a-z][a-z0-9]{0,31}$` (the
`connections` check constraint holds the list), and a wrap AAD's `key_id` must be a KEK id, so no
field can carry the `|` separator. The Withings app client secret's AADs (ADR 0004 §2,
`ownvitals-app-v1`) come with its module in M1 (#22), once `withings_apps` fixes the type of its
id. Payloads are `canonical_json(...)` bytes.

## Keys: three kinds (ADR 0004 §3, §4)

| Kind | Class | Current file | Retired file | Id setting (default) | Id pattern | Who loads it |
|---|---|---|---|---|---|---|
| KEK | `Kek` | `connection_kek` | `connection_kek_<id>` | `connection_kek_id` (`kek-1`) | `^kek-[a-z0-9-]{1,28}$` | web and worker; **required** |
| Webhook seed | `WebhookSeed` | `webhook_seed` | `webhook_seed_<id>` | `webhook_seed_id` (`seed-1`) | `^seed-[a-z0-9-]{1,28}$` | **the worker only** |
| Webhook pepper | `WebhookPepper` | `webhook_pepper` | `webhook_pepper_<id>` | `webhook_pepper_id` (`pep-1`) | `^pep-[a-z0-9-]{1,28}$` | web and worker |

- A key file holds exactly 64 lowercase hex characters (32 bytes), optionally one `\n`; anything
  else (upper case, CRLF, a second newline, spaces, raw bytes) is refused without saying what it
  holds. Files live in `OWNVITALS_SECRETS_DIR` (default `/run/secrets`), never in the environment.
- Only a validated id of a kind names a file of that kind, so no other input builds a file name,
  and an id of one kind never opens another's file. Only a `Kek` wraps or unwraps a data key (a
  `TypeError` otherwise); the pepper never encrypts anything.
- **M0:** the seed and pepper slots are optional: a file that is present is validated and
  checked, an absent one leaves the slot empty (`KeyUnavailable` on use). M1 makes web require
  the pepper and the worker the seed and the pepper; nothing uses them before (#24).
- Two kinds holding the same key refuse to start.
- `repr` of a key or the ring shows ids only; neither can be pickled or copied.

### The check table

`connection_keks (key_id text PRIMARY KEY, check_value bytea, created_at)` holds, per id of any
kind, the first 16 bytes of `HMAC-SHA256(key, "ownvitals-<kind>-check-v1")` (`kek`, `seed`,
`pepper`): the same bytes under another kind give another value. Its constraints admit only ids
of the three patterns and 16-byte values. It is a system table (`db/tables.py`), reached only
through `ownvitals.crypto.keks` (the crypto guard). The app role may `SELECT` and `INSERT`, never
`UPDATE`, `DELETE` or `TRUNCATE` (`role_guard.RESTRICTED_APP_PRIVILEGES`, restored by
`10-roles.sql` when it runs again); deleting rows is #13's `keys wipe`, as the owner role.

## Startup: web and worker refuse

`load_keyring(factory, settings, role="web" | "worker")` reads the current keys of the role and,
in one `system_session` of its own, inserts each one's check row if its id has none and compares
it (`KeyRingError` on a mismatch: another key under an unchanged id). It refuses to start
(`KeyRingError`) when:

- `connection_kek` is missing or malformed, or a present `webhook_seed`/`webhook_pepper` is
  malformed, or an id setting doesn't match its pattern (`Settings` refuses that first);
- two kinds share a key;
- **web** can see any seed file (`webhook_seed` or `webhook_seed_*`) in its secrets directory, or
  can't list the directory to check;
- the `connection_keks` table is missing: "run the migrations (up to `0003_connection_keks`)";
- a check row doesn't match.

An **unreachable** database is not an error at startup, as for the role check (ADR 0002 §5): the
ring starts unverified and the first use of a current key runs the check in its own committed
session (a second pool connection, once per process); a mismatch then refuses every use.

**Web:** `create_app`'s lifespan loads the ring after the role check, as `app.state.keyring`.
Its ring has no seed: `current_seed`/`seed_for` raise `SeedRefused`, and so does building a web
`KeyRing` with one. **Worker:** #9's worker calls `load_keyring(factory, settings,
role="worker")` at start (if #9 merges first, the Architect adds that call in a follow-up).

## Using the ring (#10, #13, M1)

| Call | Returns |
|---|---|
| `await ring.current(db)` | the current `Kek`, for writes |
| `await ring.kek_for(db, row.key_id)` | the KEK a row names: current, or a retired file whose check row matches; else `KeyUnavailable` (`key_unavailable`: the request fails, the row is not changed, ADR 0004 §3) |
| `await ring.current_pepper(db)`, `await ring.pepper_for(db, pepper_id)` | the pepper (M1 verification, #24) |
| `await ring.current_seed(db)`, `await ring.seed_for(db, seed_id)` | the seed, worker only (M1 derivation, #24) |

A retired key is loaded on first use and cached only once it matched; a missing or mismatching
one is retried on every use, so restoring its file fixes it. `db` may be any session; the
deferred check never uses it.

**Rotation** (ADR 0004 §3): rename `connection_kek` to `connection_kek_<old id>`, install the
new key as `connection_kek`, set `OWNVITALS_CONNECTION_KEK_ID` to the new id, restart. Rows keep
opening under the retired file until #13's `keys rewrap` has moved them; then the old file goes.

## The crypto guard (`test_crypto_guard.py`)

`crypto_scan.scan_source` refuses, outside `ownvitals.connections`, `ownvitals.auth` and the two
crypto modules (`ownvitals.crypto.keks`, `ownvitals.crypto.envelope`):

- importing `ownvitals.crypto.envelope` in any form (also `from ownvitals.crypto import envelope`,
  relative imports, the dotted name in a string as `importlib` takes it);
- importing a cryptographic primitive package (`cryptography`, `Crypto`, `Cryptodome`, `nacl`),
  in the same forms. The standard library's `hmac`, `hashlib` and `secrets` are not guarded;

and, everywhere but `ownvitals.crypto.keks`, the `connection_keks` table object, `keks.metadata`
or SQL naming `connection_keks`; outside `ownvitals.connections`, the `connections` table module
or SQL naming `connections` (#13). M1 adds the module that holds the Withings app secret to the
allowlist.

`crypto_scan.check_secret_columns` checks the migrated schema: a column whose name has a secret
word (`token`, `secret`, `password`, `seed`, `pepper`, `kek`, `dek`, `totp`, `credential`, …)
must be `bytea`, unless its last word says it is about a secret (`_id`, `_hash`, `_hmac`,
`_nonce`, `_generation`, `_at`, …) or `NOT_SECRET_COLUMNS` lists it with a reason (#21 adds
`withings_apps.webhook_secrets`, HMACs and ids only). It catches a plaintext column type, not a
plaintext value in a `bytea` column. Both guards have planted-violation tests.

## Sessions

`ownvitals.crypto.keks` is the one module of the package on the `system_session` allowlist
(`tests/session_scan.py`): it records and reads check rows before any user is known. It never
opens a `user_session`, and `envelope` opens nothing.

## Local stack and tests

`scripts/worktree.sh init` writes `secrets/connection_kek`; compose mounts it into web. The seed
and pepper are not mounted in M0 (M1 mounts the pepper into web and the worker, the seed into the
worker only). In the tests `conftest.secrets_dir` writes a random `connection_kek` for each test
(`current_kek` gives its bytes); tests that start web on a real database use the `schema` fixture
(the migrated schema), and `owned_tables` brings it too. `keystools.py` holds the key-file and
check-row helpers; every key in the tests is synthetic.

```bash
uvx uv@0.13.6 run pytest -q tests/test_envelope.py tests/test_keks.py tests/test_crypto_guard.py
```
