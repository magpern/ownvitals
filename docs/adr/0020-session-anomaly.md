# ADR 0020: Session anomaly signal, security log and admin abuse view

Status: Proposed (roadmap Amendment 7, P2; issue #461; port of classbeat #577, #632 and #750).

## Context

A stolen session cookie is the one credential that skips every sign-in check (threat model: a
stolen session). Sessions are bound to nothing the attacker cannot copy: the cookie is
`__Host-`, `HttpOnly` and `SameSite=Strict`, but malware or a copied browser profile replays it
as is. Scripted replay usually looks different from the person: another network and another
browser, or a request rate no person reaches. ownvitals already stores the address and the
`User-Agent` a session was issued to (`sessions.ip`, `sessions.user_agent`) and can know the
country of a request when a geo source is configured (`geo_source = cloudflare`, ADR 0011
section 6; the default is `none`).

## Decision

**1. A pure policy scores each request against its session's history**
(`backend/src/ownvitals/auth/session_anomaly_policy.py`, no I/O: the time and every value are
passed in). Three signals, each a code:

| Signal | Fires when | Threshold |
|---|---|---|
| `new_ip_and_agent` | the address is on another network than the session's (`clientip.same_network`: another IPv4 address, or another IPv6 /64) **and** the browser or OS family differs (`useragent.describe`) | both together; either alone is ordinary (roaming, a browser update) |
| `request_rate` | more requests in one fixed window of the session than a person makes | more than **300 per 60 s** (`RATE_LIMIT`, `RATE_WINDOW`) |
| `country_change` | the country differs from the one the session's first request came from | only with a geo source; with `geo_source = none` every country is unknown and it never fires; `XX` is unknown; Cloudflare's `T1` (Tor) is a country of its own, so a session that moves onto or off Tor counts as a country change |

An unknown value on either side is never a change. The thresholds are constants in the policy
module, not settings: changing one is a reviewed PR. 300 per minute is far above what the SPA
makes for a person (a page loads a handful of calls) and far below a script's replay loop.

**2. The hook** (`auth/session_anomaly.py`) runs from `deps.resolve_request` once per request
whose cookie resolved to a valid session, in its own `system_session` transaction: the user row
`FOR KEY SHARE`, then the session row `FOR NO KEY UPDATE` (the `userlock` order, so an account
delete or a rotation in flight finishes first), the policy, the new counter and country stored on
the session (`seen_window_at`, `seen_count`, `seen_country`; migration `0029_session_anomaly`).
The first time any signal fires for a session, `anomaly_at` is set and **one** `auth_events` row
of kind `session_anomaly` is written (`detail`: `{"signals": [...]}`; the address and coarse
origin as every event has them). A session is flagged at most once, so a replay loop writes one
row, not thousands. A rotation (re-authentication, step-up, a second factor) issues a new row and
so a fresh history: it follows a proof only the person can give.

**3. It is only a signal: nothing is signed out automatically.** The hook never deletes a
session, refuses a request or changes what the session may do. A false positive (a VPN switched
on with another browser, a CGNAT change on a phone) would otherwise sign a person out, and a
check whose failure locks people out is one they learn to fear; the signal has not yet been
measured on real traffic. The user sees the event in "Recent security activity" and can sign
the device out or sign out everywhere; the admin sees it in the abuse view and can disable the
account. Automatic sign-out is a reversible later step (a new ADR) once the signal's false
positive rate is known.

**The hook fails open.** Any error of its own transaction (a lock or statement timeout, a
deadlock, a lost connection) is caught around the hook alone: the transaction rolls back whole,
so no counter, flag or event is half written; one WARNING (`session anomaly check failed`, the
error's class and SQLSTATE only, never an address, token or cookie) is logged; and the request
goes on with its normal resolution. A signal must never turn a valid request into a 500, and
nothing it skips is a check: every auth check runs before the hook and none depends on it.

**4. The security log.** The event is shown to its user (`activity_model.SHOWN`, worded in
`i18n/*/security.ts`). One WARNING line on `ownvitals.auth.anomaly` follows the commit, with the
signal codes and the user and session ids only: never the address (not even in part), the
`User-Agent`, the place, a token or a cookie (log-redaction test).

**5. The admin abuse view.** `GET /api/admin/anomalies` lists the anomaly events of the last 30
days across users, newest first, paged by `(at, id)`. It is behind `admin_gate` and, although a
read, also needs a step-up within the admin window (5 minutes): it shows other people's
accounts. It runs in an audited `admin_session` (`anomaly.list`, params: page size and first or
next page) on the maint role, which holds no grant on `auth_events` or `users`; the rows come
from the `SECURITY DEFINER` function `admin_session_anomalies` of
`docker/postgres/maint/70-admin-accounts.sql` (the admin account family, owned by
`ownvitals_admin_accounts`, executable by maint only, pinned by digest), which refuses unless the
transaction holds this admin's `ok` `anomaly.list` row. The family's role gains column `SELECT`
on `auth_events` (`id, user_id, kind, at, detail, browser, os, place`), never `ip`. A row
shows the event's time, the account's id and address, the signal codes and the coarse origin.

**6. Retention.** The event is an ordinary `auth_events` row and is purged after 30 days with the
others (ADR 0003 section 10); the abuse view shows no more than 30 days either. The per-session
state goes with the session row (sign-out, rotation, expiry, account delete).

## Alternatives

- **Sign out on a signal** (classbeat's later option): rejected for now (section 3).
- **An in-memory request counter per process:** no write per request, but web may run more than
  one process later and the count would reset on every restart. The counter is one row update
  in a transaction web opens anyway for the session.
- **A geo-IP database for the country:** needs the owner's answer to ADR 0011's open question 2;
  until then the country signal works only behind Cloudflare.
- **Read the anomaly rows on the app role** (as the Users list reads accounts): the issue asks for
  a reviewed definer under the admin audit, so the read cannot happen outside the audited
  transaction.

## Consequences

- Every request with a valid session costs one more short transaction (three statements).
- A session is flagged once; a second, different anomaly in the same session is not recorded.
- Tests send requests from a fixed loopback address; a test whose session was issued to another
  address with another agent string sees one `session_anomaly` row, as a real replay would.
- The thresholds are a first estimate, documented here; changing them is a PR that updates
  this ADR.
