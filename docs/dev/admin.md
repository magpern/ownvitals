# Admin: the gate, the audit log and the stored flags

The admin area (#12): [ADR 0002](../adr/0002-tenant-isolation.md) §4 and §5 (the `ownvitals_maint`
role, **admins cannot read health data**), [ADR 0003](../adr/0003-accounts-and-sessions.md) §4, §6
and §7 (the admin window, step-up for admin writes, stricter-wins flags), and classbeat ADR 0011 at
`d806051`. It ships the gate and router shell, the audit log, the stored flags (`signup_mode`,
`experimental_findings`, `ai_insights`, `banner`) with `/api/settings/public`, and a Jobs view.
The Users view and the account helpers it needs are #127 (after #11, which owns `auth/**`).
The maint role's password, compose, `db-functions`, the `Dockerfile` and CI are wired (#126).

## The pieces

| Path | What |
|---|---|
| `backend/migrations/versions/0006_admin.py` | `app_settings` and `admin_audit` (system tables); the app role reads the flags and holds nothing on the audit log |
| `docker/postgres/init/10-roles.sql` | creates `ownvitals_maint` `LOGIN` and sets its password from the `database_maint_password` secret, beside the owner and app roles |
| `docker/postgres/maint/30-maint.sql` | the role `ownvitals_maint`: attributes, settings, revoke-all, the column allowlist; plain SQL, idempotent, run as the superuser after the migrations. `db-functions` installs it after `20-queue.sql` in the same transaction. It sets no password |
| `docker/postgres/maint/70-admin-accounts.sql` | the admin account family (#314, ADR 0002 §3): `admin_account_disable`, `admin_account_enable`, `admin_account_delete` and `admin_account_revoke` (#365), the abuse view's read `admin_session_anomalies` (#461), and the admin page's `admin_account_signout` and `admin_invite_create` (issue 550; the role's one more grant: `INSERT (code_hash, expires_at)` on `invites`), owned by the `NOLOGIN` role `ownvitals_admin_accounts`, executable by maint only. Each refuses unless its transaction already holds the matching `ok` audit row, so an account change commits with its row. `admin_account_revoke` queues the delete's Withings revoke through the queue's `queue_enqueue_admin_revoke`, which only that role may execute. `db-functions` runs it last (it re-grants what `30-maint.sql` revokes); a test that re-runs `30-maint.sql` re-runs it too (`admintools.maint_scripts`) |
| `backend/src/ownvitals/admin/user_revoke.py` | the admin delete's wait for its revoke job (#365): reads only the job's `state` (maint's column grant on `jobs`) in a `user.revoke_wait` read transaction, for up to `ADMIN_REVOKE_WAIT_SECONDS` (25 s); the `user.delete` row then records `revoke`: `none`, `revoked` or `failed`. The job itself is `ownvitals.auth.admin_revoke_job` (`withings_admin_revoke`), run by the worker |
| `backend/src/ownvitals/admin/db.py` | the maint engine and its role check, `admin_session` (the audited transaction), `audit_refusal` |
| `backend/src/ownvitals/admin/audit.py` | the action codes, each action's `params` model, the refusal and request limiters |
| `backend/src/ownvitals/admin/routes.py` | `admin_gate`, `admin_router()`, `install(app)` (handlers, body limit) |
| `backend/src/ownvitals/admin/route_guard.py` | the route guard's helpers (`admin_area_routes`, `gate_problems`) |
| `backend/src/ownvitals/admin/anomaly_routes.py` | `GET /api/admin/anomalies` (#461, ADR 0020): the abuse view, behind the gate **and** a step-up (a read of other people's accounts), audited as `anomaly.list`, read through the definer `admin_session_anomalies` of `70-admin-accounts.sql`, which refuses without that transaction's `ok` row; never an address |
| `docker/postgres/maint/70-admin-accounts.sql`, last section | the admin connection-state reads (issue 549, ADR 0002 §3 and §4): `admin_withings_states(uuid[])` (each asked-about account's Withings state, at most 101 ids, in a `user.list` transaction or a `user.read` of that one account) and `admin_withings_counts()` (accounts per state, in an `overview.read` transaction), owned by the `NOLOGIN` role `ownvitals_admin_connections` (`BYPASSRLS`, since `connections` is an owned table; `SELECT (owner_id, provider, status)` on `connections`, `(id, role, disabled_at)` on `users` and the audit check's columns of `admin_audit`; nothing else), executable by maint only. Each refuses without its transaction's `ok` row, inside a user session (`bad_context`) and for a bad argument. What leaves them: an account id the caller passed with `healthy`, `needs_reconnect` or `not_connected`, or the three words with counts. Pinned with the admin account family (`queue_expected_extra.py`, `queue_digests.py`) |
| `backend/src/ownvitals/admin/connection_states.py` | the calls to those two definers (`withings_states`, `withings_counts`); the words are the user's own Connections page's (`views.states`: no live Withings row is `not_connected`, `needs_reconnect`, and connected is the design's "Healthy") |
| `backend/src/ownvitals/admin/overview.py` | `GET /api/admin/overview` (`overview.read`, issue 549): accounts and confirmed second factors (`auth.admin_counts`, the app role's `system_session`), the effective sign-up mode, accounts per Withings state (`admin_withings_counts`) and the `queued`, `running` and `dead_letter` job counts (maint's grant on `jobs.state`). Counts only |
| `backend/src/ownvitals/admin/withings_app.py` | `GET /api/admin/withings-app` (`withings_app.read`, issue 549): whether an app is configured, the last four characters of the newest row's client id (`…a91f`, none for an id under 12 characters) and the API host (`egress.WITHINGS_API_HOST`); never the secret, its envelope, the webhook secrets, the callback URL or the whole id |
| `backend/src/ownvitals/admin/account_writes.py` | the admin page's writes (issue 550), behind the gate **and** `require_admin_stepup`: `POST /api/admin/invites` (`invite.create`, params `expires_days` only) makes one invite as `ownvitals invite create` stores it (the code's SHA-256 and a 7-day expiry, no `created_by`) and answers 201 with the code, shown once and in no row, log or other response; `DELETE /api/admin/users/{user_id}/sessions` (`sessions.revoke_all`, the account as the target) signs that account out everywhere, an admin's (the caller's own included) too, and answers the count; 404 `not_found` for an unknown account. Each runs `ownvitals.auth.admin_writes` in its `admin_session`, through the definers `admin_invite_create` and `admin_account_signout` of `70-admin-accounts.sql`, which refuse without that transaction's `ok` row |
| `backend/src/ownvitals/admin/audit_routes.py`, `settings.py`, `jobs.py` | `GET /api/admin/audit`; `GET`, `PUT`, `DELETE /api/admin/settings[/{key}]`; `GET /api/admin/jobs` |
| `backend/src/ownvitals/flags/` | `core` (keys, validation, stricter wins), `effective` (per request, from `app_settings`), `routes` (`GET /api/settings/public`) |
| `backend/tests/test_maint_privileges.py` | guard 11: maint's exact allowlist, its secret-column and forbidden-table registry, append-only |
| `backend/tests/test_admin_routes_guard.py` | every route under `/api/admin` carries the gate and refuses each kind of caller |

**A database without `70-admin-accounts.sql`** (the `db-functions` step not run, or an older
volume) has none of the three `admin_account_*` definers, so every admin account change
(disable, enable, delete) fails when it calls one and answers 500; the other admin routes are
unaffected. Run `db-functions` against the database to fix it.

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
| the admin functions of `70-admin-accounts.sql` | `EXECUTE` | the account changes and the abuse view (#314, #365, #461), the two connection-state reads (issue 549), and "Sign out all" and the invite (issue 550); maint reads no column of `connections` itself and holds nothing on `sessions` or `invites` |

Nothing on any account table, `job_events`, `connection_keks` or any health-data table. The
registry in guard 11 names the secret columns and forbidden tables; a grant on one of them fails
CI even if the allowlist is edited too. A new grant goes into `30-maint.sql` and guard 11's
`EXPECTED` in the same PR, with its reason; a health table never does without a new ADR.

`db` creates the LOGIN role on the first start of an empty volume and sets its password from the
`database_maint_password` secret (`docker/postgres/init/10-roles.sql`). `db-functions` then
installs `30-maint.sql` after `20-queue.sql`, in one transaction through
`ownvitals.db.install_functions` (no psql). That script sets the attributes and the allowlist
and does not change the password.

Compose gives that secret to `db` and `web`, and `OWNVITALS_MAINT_DATABASE_URL`
(`postgresql+psycopg://ownvitals_maint@db:5432/ownvitals`) to `web` only. With both set, web
builds the maint engine and the admin routes do not answer 503 `admin_unavailable`. Without
the URL or the secret file there is no maint engine and every admin route answers 503
`admin_unavailable`.

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
length, an AI cap's amount in micro-USD, a filter; never free text, a secret, a token or a health value), the outcome and reason
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
| `ai_user_monthly_cap_microusd` | an integer, micro-USD | the lower of `OWNVITALS_AI_USER_MONTHLY_CAP_MICROUSD` and the stored value, and never above the effective global cap (#528, `ownvitals.flags.caps`) |
| `ai_global_monthly_cap_microusd` | an integer, micro-USD | the lower of `OWNVITALS_AI_GLOBAL_MONTHLY_CAP_MICROUSD` and the stored value |

A stored AI cap above the environment's is refused (`above_environment`), as is a per-user cap
above the effective global cap or a global cap below the effective per-user cap
(`user_above_global`, checked against the other cap as stored in the same transaction). An invalid
stored cap counts as 0: no request can be reserved. The worker reads both on every reservation
([ai.md](ai.md), "Units, month and caps").

No row (or the JSON `null` of a cleared flag) means the environment's value. A stored value that
doesn't validate falls back to the stricter default (`invite`, `off`, no banner, a cap of 0) and is logged by
key only. `GET /api/settings/public` (no sign-in) returns the banner and the effective sign-up mode,
never the switches.

## Tests

The admin tests create `ownvitals_maint` themselves (`admintools.admin_schema`: run `30-maint.sql`
as the test superuser, a random password, the role dropped afterwards), so no other test meets it.
With the database of [backend.md](backend.md) and its roles:

```bash
uvx uv@0.13.6 run pytest -q tests/test_admin_*.py tests/test_maint_privileges.py tests/test_flags*.py
```

## Frontend (issue 548)

`/admin?tab=overview|accounts|jobs|withings|flags|audit` (`pages/admin/AdminTabs.tsx`; the tab is
in the address, an unknown value is Overview). Read-only views on the endpoints above, decoded in
`src/api/adminViews.ts`: **Jobs** shows the state counts of `GET /api/admin/jobs` only (the
failures list, with its kinds, is never decoded); **Flags** shows `GET /api/admin/settings` as
effective value and source (the controls came with issue 550, below); **Audit log**
shows when, action, target and result of `GET /api/admin/audit`, never params or the request id.
Overview is the whole page: the four tiles of `GET /api/admin/overview` (`pages/admin/OverviewTiles.tsx`, issue 549), then Accounts, Jobs and the Withings app card (`pages/admin/WithingsAppCard.tsx`, `GET /api/admin/withings-app`), Flags and Audit log side by side on wide screens. The Accounts table's Withings column shows each account's `withings` state as a pill: Healthy, Needs reconnect or Not connected (`src/api/adminOverview.ts` refuses any other word). The new strings are the
`admin` catalogue area (en and sv); `pages/AdminPage.tsx` itself stays on the English allowlist.
Tests: `npx vitest run src/pages/admin src/pages/AdminPage.test.tsx`.

**The writes (issue 550).** Every write carries the lock and runs through `useProtected()`, the
shared "Confirm it's you" dialog, when the server answers `step_up_required` (a code from the last
5 minutes). `src/api/adminWrites.ts` sends them. **Flags** (`pages/admin/FlagControls.tsx`):
sign-up is the mock-up's Closed / Invite only choice (open is never offered; invite only is not
offered while the server settings say closed); an on/off flag the server has off is an off switch
that can't be pressed, otherwise "Change…" opens `pages/admin/FlagDialog.tsx`, which knows nothing
about the flag (fields, Save and the optional "Use the server setting" come from the caller, so a
later flag such as the AI monthly caps reuses it); the banner's "Change…" takes the text and the
level, or removes it; the two AI monthly caps (`pages/admin/CapControl.tsx`, #528) take an
amount in USD (at most two decimals, never above the server setting) and store micro-USD, with the
server's `above_environment` and `user_above_global` refusals worded in the dialog. **Accounts** (`pages/admin/AccountWrites.tsx`): "Invite someone…" shows the
new code once in a dialog and forgets it when it closes; "Sign out all…" is on each account with a
session and asks first; on the signed-in admin's own row it says this session ends too. Role changes and second-factor resets stay with the operator CLI. The
Withings pills and the writes' words are in the `admin` catalogue (en and sv). Tests:
`npx vitest run src/pages/admin`, backend `tests/test_admin_writes.py`, e2e
`e2e/specs/admin-writes.spec.ts`.
