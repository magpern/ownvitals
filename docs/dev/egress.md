# Egress: the outbound allowlist and mail

PLAN §2 ("the worker owns every outbound call") and roadmap Amendment 1. The package
`ownvitals.egress` (`backend/src/ownvitals/egress/`) is the only code that builds an HTTP client,
a transport or a `urllib` opener; `ownvitals.mail` is the only module allowed to open an SMTP
connection. The lists are code: nothing in the environment widens one. Adding a host is a code
change in `egress/core.py` and a security-critical review.

## What each process can reach

| Process | Entry point | Reaches |
|---|---|---|
| worker | `withings_async_client()` | `wbsapi.withings.net`, `account.withings.com` (every Withings call: sync, refresh, revoke, notify) |
| worker | the process-wide urllib opener (`install_worker_allowlist`) | the same, for any library that calls `urlopen` |
| worker, AI enabled | `model_async_client()` | `api.anthropic.com` (M7) |
| web | `google_signin_transport()`, `google_async_client()` | `oauth2.googleapis.com` (token), `www.googleapis.com` (JWKS) |
| web | `withings_code_exchange_client()` (`egress/code_exchange.py`) | one request: `POST https://wbsapi.withings.net/v2/oauth2`, `action=requesttoken`, `grant_type=authorization_code` |
| web | `ownvitals.mail.Mailer` | the configured SMTP server (`smtp_host`, a setting, not a list entry) |

`tests/test_egress_runtime.py` enumerates it: every entry point against every candidate request,
in web and in the worker with AI off and on. A new client builder in the package fails that test
until it joins the enumeration.

**Process roles.** `install_worker_allowlist(ai_enabled=settings.ai_insights)` makes the process
the worker: call it first thing at worker start (the job queue issue wires it). From then on the
web-only entry points (Google, the code exchange, mail) raise `OutboundBlocked` before any
connection (`refuse_in_worker`), and the worker-only clients work only there: in the web process
`withings_async_client()` and `model_async_client()` raise `OutboundBlocked`, so web can make no
refresh, revoke or other Withings call. `ai_enabled` must be a real `bool`; with it false the
model host is on no list. Both checks run when a client is built and again on every request.

## Using it

```python
from ownvitals import egress

# Worker: Withings API calls (PLAN §4.2). Total deadline per request, body included (30 s).
async with egress.withings_async_client() as client:
    response = await client.post("https://wbsapi.withings.net/measure", data=form, headers=auth)

# Web: the OAuth callback's code exchange (#22). Anything but that one request is refused.
async with egress.withings_code_exchange_client() as client:
    response = await client.post(egress.WITHINGS_TOKEN_URL, data=exchange_form)

# Web: Google sign-in (#10). Authlib's client takes the transport; the guard requires both
# arguments and refuses proxy=, mounts= and ** on it.
AsyncOAuth2Client(..., transport=egress.google_signin_transport(inner=t), trust_env=False)
async with egress.google_async_client(inner=t) as client:  # the JWKS fetch
    ...
```

- Every client has `trust_env=False`, follows no redirect, refuses a redirect answer to another
  host (the code exchange refuses every redirect), verifies TLS against `certifi` and never
  writes a TLS key log (`SSLKEYLOGFILE` is ignored, `tls_client_context`).
- `total_seconds` (positive and finite) bounds a whole request with its body
  (`RequestDeadline`, a `TimeoutError`): Withings 30 s, Google 10 s, the code exchange 10 s (the
  code is valid for 30 s). urllib requests get theirs from `timeout` (fallback 60 s) and
  `call_deadline` / `call_deadline_until`.
- `RequestBudget(hosts, limit, used)` with `request_budget(budget)` counts every request and
  redirect hop to `hosts` before it is sent; the one over the limit raises
  `RequestBudgetExhausted` and is never sent (per-job budgets, #18).
- A refusal raises `OutboundBlocked` (`host` only; a job fails `outbound_blocked`, ADR 0005) and
  logs `outbound request blocked` with the host, never the URL, a header or a body.
- `inner=` replaces the network transport in tests (an `httpx.MockTransport` or a fake); every
  check still runs.

## Mail

`Mailer(settings).send(MailMessage(to, subject, body))` over `smtplib` in a thread;
`send_logged` is the background task that sends after the response. Settings
([backend.md](backend.md)): `smtp_host` (unset: nothing is sent), `smtp_port` 587,
`smtp_sender`, `smtp_tls` `starttls` (default), `tls` or `none` (`none` only for loopback or
`mailpit`), `smtp_username`, and the secret file `smtp_password`. Locally the host is mailpit.
A failure is a `MailError` with the exception class only (no recipient, no cause, no context).
Tests use the in-process fake `tests/smtpfake.py` on loopback (trickling, stalling, STARTTLS and
implicit-TLS modes).

**Total deadline.** Mail runs inside web requests, so one delivery ends within
`SMTP_TOTAL_SECONDS` (20 s; `Mailer(settings, total_seconds=...)`, positive and finite). The
deadline is taken before the thread is scheduled, and every socket operation gets the time left
as its timeout: the connection, each `recv` of a reply, each send and the TLS handshake. A server
that trickles its replies a byte at a time, or stalls after the greeting, ends at the deadline
with `MailError("delivery failed (deadline passed)")`. It is a module constant in `mail.py` until
`config.py` can take a setting (the Architect moves it once the shared-file lock allows).

## The guard

`tests/egress_scan.py`, run by `tests/test_egress_guard.py` over the whole source tree. Outside
the `ownvitals.egress` package it refuses building a client, transport, opener, socket or SMTP
connection, importing a client library that bypasses it (`requests`, `aiohttp`, `urllib3`,
`httpcore`, `http.client` ...), building an `Allowlist` or `AllowlistTransport`, handing a
client builder a list, and using any private name of the package. `EXEMPT` holds only
`ownvitals.mail` (`smtplib.SMTP`, `SMTP_SSL`, built by a call or subclassed for the deadline).

- `WORKER_PACKAGES`: packages that run in the worker never import `ownvitals.mail`.
- `WORKER_ENTRYPOINTS`: imported in a fresh interpreter, they must not load `ownvitals.mail`,
  `smtplib`, `authlib` or another client library. **Add each new worker entry module here**
  (the worker loop, the job runner, the Withings client ...).
- Tests that call `install_worker_allowlist` reset the process state afterwards with an autouse
  fixture (`urllib.request._opener` and `ownvitals.egress.core._worker_list`), as each
  `test_egress*.py` file does.

```bash
cd backend
uvx uv@0.13.6 run pytest -q tests/test_egress.py tests/test_egress_guard.py \
  tests/test_egress_runtime.py tests/test_egress_code_exchange.py tests/test_egress_deadline.py \
  tests/test_egress_budget.py tests/test_mail.py
```

## Known limits

- The checks run inside the process: code in it could still open a socket around them (the guard
  makes that visible in review); a network-level egress rule is the deployment's.
- The web process installs no process-wide urllib opener yet: a third-party library calling
  `urlopen` in web is not stopped at run time (our own code is kept out by the guard). A
  deny-all opener at web start goes to #10, the next issue to take `web.py`.
- A DNS lookup in progress is not cut by a urllib deadline (the resolver's timeout ends it).
- SMTP: a DNS lookup in progress is not cut either, and a host name with several addresses
  gets the time left for each connection attempt.
- `account.withings.com` stays on the worker list because PLAN §2 lists it; the server has no
  known call to it (it hosts the authorize page the browser opens).
- The code exchange counts against the app's Withings rate budget once the rate limiter exists
  (#23).

## Decisions (accepted by the Architect, 2026-10-31, #6)

1. **Worker-only clients refuse outside the worker.** `withings_async_client` and
   `model_async_client` check the installed worker list when built and on every request, so the
   web process can make no Withings call but the code exchange. Every process that calls
   Withings (the worker, later the probe CLI) and the tests of such code install the worker list
   first (`install_worker_allowlist`), the tests with a reset fixture.
2. **The model host is gated through the installed list.** `model_async_client` builds only in a
   worker installed with `ai_enabled=True` (`ai_insights`); `ai_enabled` must be a real `bool`.
3. **Google sign-in has a host list in web**: the token endpoint and JWKS hosts, not
   `accounts.google.com` (the browser's authorize page). Authlib's `AsyncOAuth2Client` is allowed
   only with `transport=google_signin_transport(...)` and `trust_env=False`, without `proxy`,
   `mounts` or `**`, so sign-in needs no guard exemption.
4. **The code-exchange form is strict**: unknown or repeated fields are refused, not ignored
   (a PHP-style server folds `grant.type` and `grant_type[]` into `grant_type`). Allowed:
   `action`, `grant_type`, `client_id`, `client_secret`, `code`, `redirect_uri`. Withings'
   signature/nonce variant would widen it here, under review.
5. **Mail errors carry no exception context**: `MailError` is raised after the `except` block,
   with the exception class only.
