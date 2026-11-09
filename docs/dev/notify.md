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
value or finding text: the check constraints of migration `0012_outbox` admit exactly the lists in
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

# Finding notifications and the E4/E6 hold (#195)

PLAN §7.1 step 7, ADR 0010 §4 ("only live episodes notify, once, at first write"), ADR 0006
section 11 ("E4 and E6 notifications wait for the gate").

## The pass

`notify_findings` (`ownvitals.notify.findings_job`, worker only) runs one owner's pass
(`ownvitals.notify.findings.notify_owner`) in one fenced transaction, as the owner under RLS. The
worker's registry wraps `derive_evaluate` and `ecg_recompute` with `then_notify`, so a pass is
queued (one per owner, singleton `notify_findings`) after each successful attempt of either. The
6-hourly poll (sync, normalise, daily, evaluate) and the nightly reconciliation therefore
re-check every hold; a hold at its bound ends at the next pass, so the bound is met within the
poll's interval.

| Module | Does |
|---|---|
| `notify.findings_state` | the only module of the step that names `findings`: the waiting ids, and every `notify_state` move as a conditional, row-locking `UPDATE … RETURNING`; queues the outbox row only after the move returned the row (listed in `tests/test_findings_read_guard.py`) |
| `views.findings_read.read_notification_candidates` | what the step may show: the `notification` consumer (experimental only with the owner-only switch), hidden findings kept and marked, each trigger group's links (`TriggerLink`) |
| `notify.hold` (pure) | the hold decision and its reason code |
| `notify.findings` | the pass: candidates, preferences, `ecg` cursors and completed `ecg` sync jobs, holds, decisions |

`notify_state`: `pending` (a live episode just written) → `held` (E4, E6) → `sent` or `none`.
Nothing moves a finding back, so a sent notification is never recalled and an update (which
keeps the state) never notifies; a backfill episode is `none` by the schema's check.

**Delivery.** A tier in `user_profiles.notify_tiers` (every tier by default) becomes `sent`
(the in-app list) and, with `notify_email`, queues one `findings_waiting` row (route `findings`,
coalesced per owner); any other tier becomes `none`. A candidate the read function does not
return (experimental without the switch) becomes `none` and is never sent later.

**Exactly once.** Findings are taken in id order and a finding's row is locked before its hold
row; of two concurrent passes the second finds every row moved.

## The hold

`notify_holds` (migration `0020_notify_holds`; owned, forced RLS; purged with the derived data
and gone with the account): one row per held finding, `finding_id` without a foreign key so a
withdrawn (deleted) finding's hold stays. `started_at` is the first revision's start (the bound
counts from it), `revision_at` the current revision's (a new `last_seen` or a new set of trigger
groups restarts it). No value, evidence or text.

Per trigger group: **linked** (it waits while a linked recording has no current `estimated`; a
`signal_missing` one is current and holds nothing), **no link possible** (`ecg` capability
`unsupported`, `not_authorized` or `not_available`; or an `ecg_fetch`, or a `withings_sync` or
`withings_backfill` of the `ecg` stream, `completed`, that started at or after `revision_at`,
with the stream's cursor complete and its `last_ok_at` no earlier than the job's start),
**missing** (no live group has the grpid), else it can still be linked and the finding waits.
Baseline recordings are never read: only the trigger counts.

| Reason | When the hold ended |
|---|---|
| `linked` | nothing waits and at least one trigger group is linked (a gate result, or none) |
| `capability_unsupported` | nothing linked; a group's connection cannot list recordings |
| `ecg_sync_no_match` | nothing linked; a counted `ecg` sync found no match |
| `no_trigger` | no trigger group has a live measure group |
| `timeout` | still waiting at `started_at + OWNVITALS_NOTIFY_HOLD_HOURS` (default 24, at most 24; `ownvitals.config_notify`) |
| `withdrawn` | the finding was deleted, or is no longer waiting (e.g. the switch turned off) |

`outcome`: `sent`, `sent_not_evaluated` (section 11's mark: no trigger recording has a gate
result), `hidden` (the gate failed: nothing is sent, ever, whatever a later recompute says) or
`none` (withdrawn, or a tier the user does not notify). A link that appears after a
`capability_unsupported` or `ecg_sync_no_match` end is what A9 / OC5's late-link count counts.

**Assumptions** (not verified against Withings): the revision start stands in for "the group was
stored" (it is never earlier, so the hold can only be longer); `jobs.started_at` is the first
attempt's start; the nightly `withings_reconcile` is not counted as an `ecg` sync.

## The in-app list

`GET /api/notifications` (`views.notifications_routes`): the user's sent live findings, through
the read function as the `notification` consumer (a finding the overlay now hides is left out),
newest `notified_at` first, `limit` 1 to 50 (default 20), with `seen` and `not_evaluated` and an
`unseen` count. No value, evidence or text.

## Tests

| File | What |
|---|---|
| `tests/test_notify_hold.py` | the pure decision: rules held, gate results, capabilities, syncs, the bound, restarts |
| `tests/test_notify_hold_db.py` | every hold case of `docs/adr/0006-ecg-subsystem-tests.md` with a fake clock, each reason, the vocabularies against 0020's checks |
| `tests/test_notify_findings_db.py` | backfill and updates never notify, preferences, the experimental tier, exactly once under concurrent passes, RLS |
| `tests/test_notify_findings_job.py` | a pass after a real rule evaluation, the job in the worker's context, `then_notify` |
| `tests/test_notify_findings_api.py` | `GET /api/notifications` |
