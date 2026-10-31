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
| web | `google_oauth_client()`, `google_async_client()` | `oauth2.googleapis.com` (token), `www.googleapis.com` (JWKS) |
| web | `exchange_withings_code()` (`egress/code_exchange.py`) | one request it builds itself: `POST https://wbsapi.withings.net/v2/oauth2`, `action=requesttoken`, `grant_type=authorization_code` |
| web | `ownvitals.mail.Mailer` | the configured SMTP server (`smtp_host`, a setting, not a list entry) |

`tests/test_egress_runtime.py` enumerates it: every entry point against every candidate request,
in web and in the worker with AI off and on. A new client builder in the package fails that test
until it joins the enumeration.

**Process roles.** `install_worker_allowlist(ai_enabled=settings.ai_insights)` makes the process
the worker. Only two modules may call it (the guard's `WORKER_ROLE_MODULES`): the worker's entry
point `ownvitals.worker.__main__` (#9), first thing at start, and the probe CLI
`ownvitals.withings.probe` (#29); tests may too. Neither module exists yet: an issue that names
its entry point differently changes that list in its PR. From then on the
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

# Web: the OAuth callback's code exchange (#22). The caller hands in four values; the package
# builds the request (body, headers, URL) and returns the answer, body read.
response = await egress.exchange_withings_code(
    client_id=cid, client_secret=secret, code=code, redirect_uri=callback_url
)

# Web: Google sign-in (#10): Authlib's AsyncOAuth2Client, built by the package (PKCE S256,
# client_secret_post) over Google's sign-in transport. Nothing else may import Authlib's
# network integrations (authlib.jose and authlib.oauth2 are fine).
async with egress.google_oauth_client(
    client_id=cid, client_secret=secret, scope="openid email", redirect_uri=callback_url
) as client:
    url, state = client.create_authorization_url(GOOGLE_AUTHORIZE_URL, code_verifier=verifier)
    token = await client.fetch_token(GOOGLE_TOKEN_URL, code=code, code_verifier=verifier)
async with egress.google_async_client() as client:  # the JWKS fetch
    ...
```

**The code exchange's request.** Each value must be a non-empty string of printable ASCII without
a space, `;`, `&`, `=` or `#` (so no CR, LF or other control character), checked before anything
is encoded. The body is `urlencode` of exactly `action`, `grant_type`, `client_id`,
`client_secret`, `code`, `redirect_uri`, and must parse back to exactly those fields. The
headers are `Content-Type` (form), `Accept`, `User-Agent` and httpx's own `Host` and
`Content-Length`. The transport checks the final bytes again: method, raw path, the header names
(each once, `Host` the API host, a matching `Content-Length`), and a body of at most 4 KiB that
is the canonical encoding of its own fields, with the fixed action and grant type and no
forbidden character in any decoded value. No redirect is followed.

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

`tests/egress_scan.py`, run by `tests/test_egress_guard.py` over the whole source tree. It
**denies by default**: outside the `ownvitals.egress` package a module may not

- import a network-capable module, a submodule or a name from one (`NETWORK_MODULES`: `socket`,
  `ssl`, `http.client`, `urllib.request`, `smtplib`, `httpx`, `httpcore`, `requests`, `aiohttp`,
  `urllib3`, `websockets`, `multiprocessing.connection`, `authlib.integrations`, `anthropic`,
  `openai`, `asyncio.streams` ...), also through `importlib.import_module` or `__import__`, nor
  use a name under one through a module that holds it (`egress.clients.httpx.AsyncClient`,
  `mail.smtplib.SMTP`);
- call or import a connection function of `asyncio` or `anyio` (`CONNECTION_FUNCTIONS`:
  `open_connection`, `loop.create_connection`, `loop.create_datagram_endpoint`,
  `loop.sock_connect`, `anyio.connect_tcp`, `create_udp_socket` ...), on whatever object;
- build an `Allowlist`, `AllowlistTransport`, opener or deadline handler, hand an entry point a
  list, or use a private name of `ownvitals.egress` or `ownvitals.mail`;
- call `install_worker_allowlist` (only `WORKER_ROLE_MODULES`, above);
- star-import anything.

Exceptions, each with its reason in the guard: `ALLOWED_IMPORTS` lets `ownvitals.mail` import
`smtplib`, and `EXEMPT` lets it build `smtplib.SMTP` and `SMTP_SSL` (a call or a subclass, for
the deadline) and nothing else. An import inside `if TYPE_CHECKING:` is allowed when its names
appear only in annotations (anywhere else they would run). Each name of the lists has a refused
snippet in the guard test, which fails when the name is removed.

**Exceptions callers catch.** The package's own (`OutboundBlocked`, `RequestDeadline`,
`RequestBudgetExhausted`) come from `ownvitals.egress`. A caller that needs an exception class of
a network library (an httpx transport error, say) gets it re-exported by `ownvitals.egress` in
its own PR, with the reason in the guard test; it never imports the library. None is needed yet.

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
   `accounts.google.com` (the browser's authorize page). *Revised in review:* the package builds
   Authlib's client itself (`google_oauth_client`); outside it nothing imports
   `authlib.integrations`, so sign-in needs no guard exemption. This supersedes #10's
   `transport=` wording. Authlib 1.9 runs its client on `httpx2` when that is installed; the
   helper then refuses to build it (`TypeError`), since the transport is httpx's.
4. **The code-exchange form is strict**: unknown or repeated fields are refused, not ignored
   (a PHP-style server folds `grant.type` and `grant_type[]` into `grant_type`). Allowed:
   `action`, `grant_type`, `client_id`, `client_secret`, `code`, `redirect_uri`. Withings'
   signature/nonce variant would widen it here, under review. *Revised in review:* the caller
   passes values, never a body or headers (`exchange_withings_code`).
5. **Mail errors carry no exception context**: `MailError` is raised after the `except` block,
   with the exception class only.
