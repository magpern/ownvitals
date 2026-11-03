# Mail outbox and notifications (ADR 0007, #194)

Mail that originates in the worker never leaves the worker as SMTP: the worker writes an outbox
row, and web sends it. Owner notices (mail to the admins) go the same way.

## What a row holds

| Table | Kind | Recipient | Route key |
|---|---|---|---|
| `mail_outbox` (owned, forced RLS) | `findings_waiting`, `connection_attention` | its `owner_id` | `overview`, `findings`, `connections` |
| `owner_notices` (system; no app-role privilege) | `connection_needs_reconnect` | the admins (no column) | `admin` (fixed per kind) |

Besides those, only the send state: `state` (`pending`, `claimed`, `sent`, `failed`), `attempts`,
`hold_until`, `created_at`, `updated_at`, `sent_at`. Never an address, a subject, free text, a
value or finding text: the check constraints of migration `0011_outbox` admit exactly the lists in
`ownvitals.notify.outbox`, and `tests/test_notify_outbox.py` holds the two together. At most one
pending (or claimed) row per owner and kind, and per owner-notice kind: a second write is
coalesced.

## Writing

| Code | Where | How |
|---|---|---|
| `queue_user_mail(db, owner_id, kind, route)` | a job's fenced session or a `user_session` | a plain insert; RLS's `WITH CHECK` refuses another owner |
| `queue_owner_notice(db, kind)` | any app-role session, in the caller's transaction | `owner_notice_queue(kind)`: the route comes from the function's own mapping |

`connections.providers.withings.give_up` (PLAN §4.3 step 4) queues `connection_needs_reconnect`
in the transaction that sets `needs_reconnect`.

## Sending (web only)

`ownvitals.notify.sender` runs in web's lifespan when `smtp_host` is set (without it, mail stays
queued and a warning is logged). Every 30 s it claims up to 5 rows (`mail_outbox_claim`, lease
300 s), resolves the address through `ownvitals.auth.recipients` (the account's verified email,
or every active admin's), renders the kind's template (`ownvitals.notify.templates`: "something
is waiting", a link to `public_base_url` plus the route's path) and sends through
`ownvitals.mail`. Then `mail_outbox_mark`: `sent`; `retry` on a delivery failure (held 2 minutes
times the attempts; failed after 5); `failed` when there is no one to send to. A row claimed and
never marked is claimed again when its lease ends. Sent and failed rows are deleted 30 days after
their last change. Logs carry counts only.

The worker cannot send: `run_once` refuses in the worker before it claims, `Mailer.send` refuses
there before connecting, and no worker module imports `ownvitals.notify.sender` (the outbox module
is in `tests/egress_worker.py`'s worker packages and entry points; the sender is not).

## The definer family

`docker/postgres/notify/50-outbox.sql`, run by `db-functions` after `rate.sql` as the superuser
(idempotent; it repairs the role, revokes whatever else it holds and re-grants exactly its list).
The role `ownvitals_mail_outbox` is `NOLOGIN BYPASSRLS NOINHERIT`, a member of nothing, and owns
the three functions. Web and the worker pin each function at startup (owner, `SECURITY DEFINER`,
`search_path`, comment `ownvitals-outbox v1`, ACL and `md5(pg_get_functiondef)`; ADR 0005 §3);
guard 2 (`tests/role_guard.py`) checks the role's attributes and grants, guard 9
(`tests/queue_guard.py`) the functions.

After changing `50-outbox.sql`, regenerate the digests against a database migrated to head with
the db-functions scripts applied (Postgres 17.11, as compose pins):

```bash
uv run python tests/outbox_digests.py --write   # --check fails when they are stale
```

`tests/test_notify_outbox_functions.py` runs the check in the backend suite. A change to a column
the functions read bumps the version comment in the script and `OUTBOX_VERSION` together.

## Tests

| File | What |
|---|---|
| `tests/test_notify_outbox.py` | columns, kinds and routes, the system table's closure, RLS, coalescing |
| `tests/test_notify_outbox_functions.py` | each function per caller role, claim, mark, retention, the owner-notice write, digests, drift and repair |
| `tests/test_notify_outbox_sender.py` | the sender against the SMTP fake: users, admins, failures, retries, the worker refusal, `give_up` |
| `tests/test_notify_outbox_templates.py` | the templates (no value, finding text, token or number) and the worker boundary |
