# End-to-end tests

Playwright (Chromium) against the real compose stack, with fakes only: no real Google, Withings
or mail provider, no real account and no real health data. Every address, subject and password a
spec uses is made up per run. M0's "done when" (PLAN §11) is the login-and-TOTP spec here.

| Path | What |
|---|---|
| `compose.e2e.yaml` | the overlay on `compose.yaml`: `fake-oidc`, Google sign-in switched on for web, web's trust bundle |
| `e2e/fake-oidc/` | the stand-in for Google's token and JWKS hosts (Node, no dependencies) |
| `e2e/scripts/` | `stack-up.sh`, `stack-prepare.sh`, `stack-checks.sh`, `stack-down.sh` |
| `e2e/helpers/` | `totp.ts` (the authenticator), `mailpit.ts`, `invite.ts` (operator CLI), `flows.ts` (page steps) |
| `e2e/specs/` | `auth-password`, `auth-google`, `step-up`, `password-reset` |

## Run it

From the repository root (Docker, Node 24, openssl, curl and jq):

```bash
e2e/scripts/stack-up.sh          # worktree.sh init, build, certificates, up, wait for /api/ready
e2e/scripts/stack-checks.sh      # ready, SPA, non-root, no secret value in docker inspect
cd e2e && npm ci && npx playwright install chromium && npm test
e2e/scripts/stack-down.sh        # down -v, removes e2e/.certs
```

Ports come from `.env` (`WEB_PORT`, `MAILPIT_PORT`; `FAKE_OIDC_PORT` defaults to 9100), which
`playwright.config.ts` reads. The browser must use `http://127.0.0.1:<WEB_PORT>`, the stack's
`OWNVITALS_PUBLIC_BASE_URL`, or the Origin check refuses it. One worker, no retries: the specs
share one stack and one client IP, and with them the sign-up (5 an hour) and sign-in rate limits
(ADR 0003 §9). A rerun against the same stack needs a fresh one (`stack-down.sh`, `stack-up.sh`)
once those budgets are spent.

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

## The specs

- `auth-password`: invite (operator CLI in the web container) → sign-up → mail link and password
  → sign-in → forced enrolment (key read from the page, code computed here) → recovery codes →
  sign out → sign-in → code prompt → signed in. Also a wrong password.
- `auth-google`: first Google sign-in with an invite creates the account → enrolment → sign out
  → Google again → code prompt. Without an invite: `invite_required`, no account.
- `step-up`: Security → "Sign out everywhere…" → "Confirm it's you" with a fresh code → signed out
  everywhere. A wrong code changes nothing.
- `password-reset`: mail from mailpit → new password → the old one is refused, the new one signs
  in (as far as enrolment).

`helpers/totp.ts` implements RFC 6238 itself (checked against the RFC's test vector). The server
refuses a code of a step already used, so `Authenticator.next()` hands out a later step each time:
the step after the current one is accepted (one step of tolerance), and when that is used up too
it waits for the clock, at most 30 seconds.

## CI

The `e2e` job runs when the `stack` filter is true (`backend/`, `frontend/`, `e2e/`, `docker/`,
`Dockerfile`, compose files, `.env.example`, `.trivyignore`, `scripts/worktree.sh`; everything on
a `.github/` change, the weekly run and a manual run). It builds and starts the stack, runs
`stack-checks.sh`, scans the web image with a checksum-verified Trivy (HIGH and CRITICAL with a
fix fail; `.trivyignore` applies), type-checks `e2e/`, then runs Playwright. `e2e` is in `needs`
of `ci` and in `JOBS`/`FILTERS` of `scripts/ci-gate.sh` (filter `stack`). `npm audit` of `e2e/`
is part of `checks` and required.

## Assumptions not verified here

- The specs were written without a Docker daemon in the authoring session; the first CI run is
  their first run. Chromium must keep `Secure` cookies (`__Host-`) on `http://127.0.0.1`, which it
  does for loopback.
- The Trivy release and checksum in the `e2e` job are copied from the `checks` job and
  `scripts/test-ci-downloads.sh` tests only the latter: bump both together.
