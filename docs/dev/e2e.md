# End-to-end tests

Playwright (Chromium) against the real compose stack, with fakes only: no real Google, Withings
or mail provider, no real account and no real health data. Every address, subject and password a
spec uses is made up per run. M0's "done when" (PLAN §11) is the login-and-TOTP spec here.

| Path | What |
|---|---|
| `compose.e2e.yaml` | the overlay on `compose.yaml`: `fake-oidc`, Google sign-in switched on for web, web's trust bundle, the `localhost` origin |
| `e2e/fake-oidc/` | the stand-in for Google's token and JWKS hosts (Node, no dependencies) |
| `e2e/fake-withings/` | the stand-in for Withings (`server.py`, Python standard library, runs `backend/tests/withingscore.py`) |
| `e2e/scripts/` | `stack-up.sh`, `stack-prepare.sh`, `stack-checks.sh`, `stack-down.sh` |
| `e2e/helpers/` | `totp.ts` (the authenticator), `mailpit.ts`, `invite.ts` (operator CLI), `flows.ts` (page steps), `ratelimit.ts` (empties the rate-limit buckets) |
| `e2e/specs/` | `auth-password`, `auth-google`, `google-reauth`, `step-up`, `password-reset`, `fake-withings`, `i18n`, `passkeys`, `qr-login`, `onboarding` |

## Run it

From the repository root (Docker, Node 24, openssl, curl and jq):

```bash
e2e/scripts/stack-up.sh          # worktree.sh init, build, certificates, up, wait for /api/ready
e2e/scripts/stack-checks.sh      # ready, SPA, non-root, no secret value in docker inspect
cd e2e && npm ci && npx playwright install chromium && npm test
e2e/scripts/stack-down.sh        # down -v, removes e2e/.certs
```

Ports come from `.env` (`WEB_PORT`, `MAILPIT_PORT`; `FAKE_OIDC_PORT` defaults to 9100), which
`playwright.config.ts` reads. The browser must use `http://localhost:<WEB_PORT>`, the
`OWNVITALS_PUBLIC_BASE_URL` that `compose.e2e.yaml` sets for web and the worker, or the Origin
check refuses it: `localhost`, not `127.0.0.1`, because WebAuthn has no relying party on an IP
address (#180; the two fakes' redirect URIs follow it). One worker, no retries: the specs share
one stack and one client IP, and with them the sign-up (5 an hour) and sign-in rate limits (ADR
0003 §9). `passkeys` and `qr-login` sign up more accounts and start with `resetRateLimits()`
(`DELETE FROM rate_limits` as the database superuser, like `ageStepUp`); a rerun of the other
specs against the same stack still needs a fresh one (`stack-down.sh`, `stack-up.sh`) once those
budgets are spent.

## Image and SPA

The `Dockerfile` has a `spa` stage (`node:24.22.1-bookworm-slim`, pinned by index digest) that
runs `npm ci` and `npm run build` in `frontend/`. Its `dist/` is copied to `/app/static` in the
runtime image, owned by root, and `OWNVITALS_STATIC_DIR=/app/static` makes web serve it (any GET
outside `/api` that is not a file gets `index.html`). Node is not in the runtime image.

## How Google sign-in is faked

Web's Google calls go through `ownvitals.egress`, whose host list is code: they stay on the real
names, `oauth2.googleapis.com` (the code exchange) and `www.googleapis.com` (the JWKS), over
https with certifi's trust store (`SSL_CERT_FILE` is ignored on purpose). So:

1. `stack-prepare.sh` makes a throwaway CA and a certificate for those two names, reads certifi's
   bundle from the image (`certifi.where()`), appends the CA and writes `e2e/.certs/cacert.pem`.
   `compose.e2e.yaml` mounts that file read-only over the image's copy, for web only. Nothing
   about the image changes, and the production image trusts no test CA.
2. `fake-oidc` has the two names as network aliases on the compose network and serves `POST
   /token` and `GET /oauth2/v3/certs` on 443 with that certificate. It signs RS256 ID tokens
   with a key it generates at start, checks the client id and secret (the generated
   `secrets/oauth_google_client_secret`, which web also reads), single-use codes, the redirect URI and
   PKCE (S256).
3. The browser's visit to `accounts.google.com` never leaves the machine. The spec intercepts it
   (`answerGoogleAs` in `helpers/flows.ts`) and posts the authorize URL and a synthetic identity
   to fake-oidc's control endpoint (`POST /__test/authorize`, plain http on `127.0.0.1:9100`).
   fake-oidc checks the request (client id, redirect URI, PKCE, state and nonce), remembers the
   identity under a code and returns the redirect to web's callback.
4. fake-oidc refuses to start unless `OWNVITALS_ENVIRONMENT=test`; web stays `dev`. It logs
   method and path only.

## How Withings is faked

`fake-withings` (#36) answers as `wbsapi.withings.net` and `account.withings.com` through compose
network aliases, with a certificate from the same throwaway CA as fake-oidc (`stack-prepare.sh`
writes `e2e/.certs/withings.{key,pem}`). Web and the worker both get the CA through the mounted
trust bundle (the worker needs it for the Withings calls of M1).

- **One protocol.** `server.py` only turns HTTPS requests into `withingswire.Request` and the
  `Response` back, does the HEAD check of `notify` and the webhook POST over the network, and
  serves the control endpoints. All behaviour is `FakeWithings` from `backend/tests/`, mounted
  read-only at `/protocol`, so the in-process fake and this one cannot drift.
  `backend/tests/test_withings_e2e_service.py` fails when `server.py` defines or names protocol
  code (constants, hosts, scopes, endpoints, `hmac`/`hashlib`) and when its answers differ from the
  in-process fake's.
- **No published port.** The TLS side (443) serves the API only. The control side is plain HTTP on
  9101 in the same container, under `/__e2e/`, and `compose.e2e.yaml` has no `ports:` for the
  service: only containers on the compose network reach it (the spec checks `compose ps` and that
  the host cannot connect). Specs call it from inside the container (`helpers/fake-withings.ts`,
  `control(...)`, `resetWorld()`).
- **Control.** `POST /__e2e/reset` (a fresh fake: one app, one synthetic user, a token pair,
  returned), `POST /__e2e/inject` (`kind` one of `drop|delay|status|http|garbage`, `at` a request
  number or list, optional `endpoint`, `when`, `status`, `wait_seconds`, `http_status`,
  `seconds`), `GET /__e2e/state` (counts, the masked request log, subscriptions as scheme and host
  only, failures fired), `POST /__e2e/send-webhook` (`userid`, `appli`, optional `fields` or a
  forged `form`; one attempt per subscribed URL, the retry schedule spans hours and is not run
  here), `POST /__e2e/authorize` (`url`, optional `deny`: the fake answers its authorize page for
  that URL, its synthetic user consenting or refusing once, and returns `status` and `location`),
  `GET /__e2e/ready`.
- **The connect journey** (#296). The browser's visit to `account.withings.com` is intercepted by
  the spec (`answerWithings` in `helpers/fake-withings.ts`) and handed to `/__e2e/authorize`; the
  answer's redirect goes to the app's callback base, which `withings-apps create` requires to be
  https (`https://localhost:<WEB_PORT>`, the fake's `FAKE_WITHINGS_REDIRECT_URI`), so the spec sends
  the browser to the same path and query on the stack's http origin. `registerApp` stores the fake
  app as the newest Withings app with the operator command in a one-off `migrate` container (the
  overlay gives it the synthetic `connection_kek`; the secret goes in on stdin) after
  `resetWorld()`, which gives the fake a new client secret.
- **Callback URLs.** The strict default (HTTPS, 443, a domain name) would refuse the compose
  receiver, which is plain HTTP on `web:8000`. The service starts the fake with
  `strict_callbacks=False`, the one e2e-only opt-out (a backend test shows the default refuses),
  and sends the HEAD check and webhooks to `FAKE_WITHINGS_CALLBACK_BASE` (`http://web:8000`)
  instead of the subscribed URL's own host, which the container cannot reach.
- Starts only with `OWNVITALS_ENVIRONMENT=test`; real clock; the log is method and path only.

## The specs

- `auth-password`: invite (operator CLI in the web container) → sign-up → mail link and password
  → sign-in → forced enrolment (key read from the page, code computed here) → recovery codes →
  sign out → sign-in → code prompt → signed in. Also a wrong password.
- `auth-google`: first Google sign-in with an invite creates the account → enrolment → sign out
  → Google again → code prompt. Without an invite: `invite_required`, no account.
- `google-reauth`: a Google-only account (fake-oidc sends no `auth_time`, like Google) signs up,
  activates 2FA, re-authenticates with Google, still needs a code, then sets a first password; an
  account with a password gets `reauth_use_password` at the start ([auth-reauth.md](auth-reauth.md)).
- `step-up`: Security → "Sign out everywhere…" → "Confirm it's you" with a fresh code → signed out
  everywhere. A wrong code changes nothing.
- `passkeys` (#180, ADR 0011 §8): a CDP virtual authenticator (CTAP2, internal, resident key, user
  verification): add a passkey behind "Confirm it's you" (code), sign out, "Sign in with a
  passkey", the code prompt, then remove it behind the dialog (password, then code).
- `qr-login` (#180, ADR 0011 §5): two contexts, the test's page as the desktop and a signed-in
  "phone". The phone opens `/qr#<nonce>` with the nonce from the desktop's `qr/start` answer (as
  the camera reads it from the QR; no test hook in the app): approve with a code (the desktop shows
  "Signed in as …", then the app), deny, and expiry: the desktop's clock is faked
  (`page.clock`), the code shows as expired after 60 s, and a new code makes the old one refused.
- `password-reset`: mail from mailpit → new password → the old one is refused, the new one signs
  in (as far as enrolment).

- `fake-withings`: the worker's own Withings client (real egress allowlist) reaches the fake over
  TLS and its calls are counted without a token or secret in the log; an injected 601 arrives as
  `RateLimited`; the control endpoints are reachable from the worker by service name and not from
  the host or the browser, and the TLS side does not serve `/__e2e/`.

`helpers/totp.ts` implements RFC 6238 itself (checked against the RFC's test vector). The server
refuses a code of a step already used, so `Authenticator.next()` hands out a later step each time:
the step after the current one is accepted (one step of tolerance), and when that is used up too
it waits for the clock, at most 30 seconds.

## CI

The `e2e` job runs when the `stack` filter is true (`backend/`, `frontend/`, `e2e/`, `docker/`,
`Dockerfile`, compose files, `.env.example`, `.trivyignore`, `scripts/worktree.sh`; everything on
a `.github/` change, the weekly run and a manual run). It builds and starts the stack, runs
`stack-checks.sh`, scans the web image with a checksum-verified Trivy (HIGH and CRITICAL with a
fix fail; `.trivyignore` applies), then, only when the `playwright` filter is true (`frontend/`, `e2e/`, `docker/`, `Dockerfile`,
compose files, `.dockerignore`; everything on a `.github/` change, a push to `main`, the weekly run
and a manual run), installs the e2e dependencies and Chromium, type-checks `e2e/` and runs
Playwright. A backend-only change gets the stack, the checks and the scan without Playwright.
`e2e` is in `needs`
of `ci` and in `JOBS`/`FILTERS` of `scripts/ci-gate.sh` (filter `stack`). `npm audit` of `e2e/`
is part of `checks` and required.

## Assumptions not verified here

- `onboarding`'s connect journey (#296) was written without a Docker daemon; its first CI run is
  its first run (the `docker compose run` of `migrate` with the KEK, the intercepted authorize
  page, the redirect to http, the worker's first import within 60 s).

- `fake-withings`: the first CI run is its first run in Docker (the sysctl for port 443 as a
  non-root user, the alias names, the mounted bundle in the worker). Nothing about it is verified
  against the real Withings (INTEGRATION_VERIFIED is the owner's).

- The specs were written without a Docker daemon in the authoring session; the first CI run is
  their first run. Chromium must keep `Secure` cookies (`__Host-`) on `http://localhost`, which it
  does for loopback, and reach the port published on 127.0.0.1 when it resolves `localhost`.
- `passkeys` and `qr-login` (#180) were written without a Docker daemon too; their first CI run is
  their first run.
- The Trivy release and checksum in the `e2e` job are copied from the `checks` job (both
  install it through `scripts/ci-install-release.sh`): bump both together.
