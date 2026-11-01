# Admin: the gate, the audit log and the stored flags

The admin area (#12): [ADR 0002](../adr/0002-tenant-isolation.md) §4 and §5 (the `ownvitals_maint`
role, **admins cannot read health data**), [ADR 0003](../adr/0003-accounts-and-sessions.md) §4, §6
and §7 (the admin window, step-up for admin writes, stricter-wins flags), and classbeat ADR 0011 at
`d806051`. It ships the gate and router shell, the audit log, the stored flags (`signup_mode`,
`experimental_findings`, `ai_insights`, `banner`) with `/api/settings/public`, and a Jobs view.
The Users view and the account helpers it needs are #127 (after #11, which owns `auth/**`); the
stack wiring of the maint role (its password, compose, `db-functions`, the `Dockerfile`, CI) is
#126.

## The pieces

| Path | What |
|---|---|
| `backend/migrations/versions/0006_admin.py` | `app_settings` and `admin_audit` (system tables); the app role reads the flags and holds nothing on the audit log |
| `docker/postgres/maint/30-maint.sql` | the role `ownvitals_maint`: attributes, settings, revoke-all, the column allowlist; plain SQL, idempotent, run as the superuser after the migrations |
| `backend/src/ownvitals/admin/db.py` | the maint engine and its role check, `admin_session` (the audited transaction), `audit_refusal` |
| `backend/src/ownvitals/admin/audit.py` | the action codes, each action's `params` model, the refusal and request limiters |
| `backend/src/ownvitals/admin/routes.py` | `admin_gate`, `admin_router()`, `install(app)` (handlers, body limit), the route guard's helpers |
| `backend/src/ownvitals/admin/audit_routes.py`, `settings.py`, `jobs.py` | `GET /api/admin/audit`; `GET`, `PUT`, `DELETE /api/admin/settings[/{key}]`; `GET /api/admin/jobs` |
| `backend/src/ownvitals/flags/` | `core` (keys, validation, stricter wins), `effective` (per request, from `app_settings`), `routes` (`GET /api/settings/public`) |
| `backend/tests/test_maint_privileges.py` | guard 11: maint's exact allowlist, its secret-column and forbidden-table registry, append-only |
| `backend/tests/test_admin_routes_guard.py` | every route under `/api/admin` carries the gate and refuses each kind of caller |

## The role `ownvitals_maint`

`LOGIN NOSUPERUSER BYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT`, member of no role,
owning nothing, with exactly five role settings: `statement_timeout = 30s`, `lock_timeout = 5s`,
`idle_in_transaction_session_timeout = 60s`, `transaction_timeout = 5min` (ADR 0002 §5, #78) and
`default_transaction_read_only = on`. Web checks all of it at startup (`AdminDb.verify`, a
`MaintRoleError` refuses the start) and on every new connection, and checks each pooled
connection's read-only default and four timeouts on checkout.

Its privileges, all of them (guard 11 compares the catalog with this list):

| Object | Privilege | Why |
|---|---|---|
| the database | `CONNECT` | |
| schema `public` | `USAGE` | |
| `admin_audit` | `INSERT`; `SELECT` on every column but `ip_hash`; `USAGE` on `admin_audit_id_seq` | write and read the log |
| `app_settings` | `SELECT` (all columns), `INSERT (flag, value, updated_by)`, `UPDATE (value, updated_at, updated_by)` | read, set and clear flags (clearing stores the JSON `null`: no `DELETE`) |
| `jobs` | `SELECT (id, kind, state, error_category, created_at, finished_at)` | counts per state, failures by category; never the owner, input, keys or hashes |

Nothing on any account table, `job_events`, `connection_keks` or any health-data table. The
registry in guard 11 names the secret columns and forbidden tables; a grant on one of them fails
CI even if the allowlist is edited too. A new grant goes into `30-maint.sql` and guard 11's
`EXPECTED` in the same PR, with its reason; a health table never does without a new ADR.

Without `OWNVITALS_MAINT_DATABASE_URL` and the `database_maint_password` secret file there is no
maint engine and every admin route answers 503 `admin_unavailable` (until #126 wires them).

## Every transaction is audited

`admin_session(db, actor, action, write=..., target=..., params=...)` validates `params` against
the action's model (`ownvitals.admin.audit.ACTIONS`; unknown actions and bad params are a bug, refused
before any connection), then in one transaction: `SET TRANSACTION READ WRITE`, the `ok` audit row,
and for a read `SET TRANSACTION READ ONLY`. A write's row commits or rolls back with it. When the
body raises, the transaction rolls back and one `refused` (`AdminRefused`) or `error` row is written
in its own transaction; a statement that failed while the body went on (its error caught) is an
`error` too, never a silent rollback at commit. Refusal rows are limited to 20 per admin and 100 per
minute (per process).

**Append-only by privileges** (Architect decision, #12): maint may only insert and read, the app
role holds nothing, and no runtime role has `UPDATE`, `DELETE` or `TRUNCATE` (guard 11 checks it and
tries it). There is no trigger (ADR 0005 §3 would allow one), so **the owner role
(`ownvitals_owner`, used only by `migrate` and the operator's one-off containers) can still change
or delete rows**.

**What a row holds:** the admin's id and email, the action code, the target (a user id, or a kind
and an id as text), `params` (keys and meanings: a flag's key and value, a banner's level and
length, a filter; never free text, a secret, a token or a health value), the outcome and reason
code, a random request id and an HMAC of the client IP under the rate-limit key (maint can't read
it back).

## The gate

Every route under `/api/admin` but `whoami` (which `ownvitals.auth` serves with `require_admin`)
depends on `admin_gate`, in this order:

1. 401 `not_authenticated`; 403 `forbidden` for a non-admin (no audit row, no maint connection);
   403 `mfa_required` for an admin without TOTP within 12 hours;
2. for every method but `GET` and `HEAD`: 403 `step_up_required` without a step-up within **5
   minutes** (`stepped_up` calls `stepup_within` with that window, the same rule as
   `require_admin_stepup`; the dependency stays off the route so the refusal still writes its row);
3. 503 `admin_unavailable` without maint;
4. 429 `rate_limited` over 60 requests per admin and minute. The window is **per process and
   resets on restart**; web runs as a single process (#77's rule).

Refusals at 1 (`mfa_required`), 2 and 4, and input refusals after the gate (a 422, an unreadable or
oversized body, an `AdminRefused` before any session), get an `admin.request` `refused` row with the
route template and method. A body is refused 413 over 16 KiB. Every response is `no-store`.

## Flags

Stored in `app_settings`, one row per `flag` (the table's check refuses any other), joined with the environment per
request; the database tightens, never loosens:

| Key | Stored | Effective |
|---|---|---|
| `signup_mode` | `invite`, `closed` | the stricter of `OWNVITALS_SIGNUP_MODE` and the stored value; `open` is refused until M8 (`environment_only`) |
| `experimental_findings` | `off`, `on` | on only when `OWNVITALS_EXPERIMENTAL_FINDINGS` is on and the stored value isn't `off` (D9; who sees the tier is decided where findings are shown) |
| `ai_insights` | `off`, `on` | on only when `OWNVITALS_AI_INSIGHTS` is on and the stored value isn't `off` (D11) |
| `banner` | `{text, level}` | plain text of 1 to 280 characters (no control, format or separator character), `info` or `warning` |

No row (or the JSON `null` of a cleared flag) means the environment's value. A stored value that
doesn't validate falls back to the stricter default (`invite`, `off`, no banner) and is logged by
key only. `GET /api/settings/public` (no sign-in) returns the banner and the effective sign-up mode,
never the switches.

## Tests

The admin tests create `ownvitals_maint` themselves (`admintools.admin_schema`: run `30-maint.sql`
as the test superuser, a random password, the role dropped afterwards), so no other test meets it.
With the database of [backend.md](backend.md) and its roles:

```bash
uvx uv@0.13.6 run pytest -q tests/test_admin_*.py tests/test_maint_privileges.py tests/test_flags*.py
```
