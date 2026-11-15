# ADR 0018: Cloudflare Turnstile on sign-up, sign-in and the reset request

Status: Proposed (#460, roadmap Amendment 7 item P1, egress change in roadmap Amendment 12).
Security-critical (PLAN-operations §10.1: auth, and a new outbound host for web). Built off by
default; the owner decides when, and whether, it is turned on (before M8 opens registration
beyond the household).

## Context

Amendment 7 lists P1: a bot check on the three anonymous forms that cost the server work or send
mail (sign-up, password sign-in, password-reset request) before registration opens beyond the
household. classbeat solved it with Cloudflare Turnstile (#721, #724, #726, #798, #805). The
existing defences are the rate limiter (ADR 0003 section 9), the invite gate and uniform answers;
they bound the damage but do not tell a script from a person.

Turnstile has two halves. In the **browser** a widget (a script and an iframe from
`challenges.cloudflare.com`) produces a single-use token, valid for 300 seconds. The **server**
posts the token with its secret to `https://challenges.cloudflare.com/turnstile/v0/siteverify`
and gets JSON back: `success`, `error-codes`, `hostname`, `action`, `challenge_ts`, `cdata`.

ownvitals' constraints: every outbound call goes through `ownvitals.egress` with its host in code
(AGENTS.md); web's outbound list is fixed by roadmap Amendment 1 (Google sign-in, SMTP, the one
Withings code exchange); the CSP is `'self'` only (`headers.py`); the public pages and the app
load no third-party content (ADR 0016, the legal drafts); secrets come only from secret files.

## Decision

1. **Off by default.** `OWNVITALS_TURNSTILE_ENABLED` (default `false`). The public site key is
   `OWNVITALS_TURNSTILE_SITE_KEY`; the secret is read only from the secret file
   `turnstile_secret_key` (`config_secrets.SECRET_FILE_FIELDS`), never from the environment, and
   is a `SecretStr`. **Install check:** with `turnstile_enabled=true`, the settings refuse to load
   (so no process starts) unless both keys exist and have a usable shape; the message names the
   field, never a value (`config_turnstile.py`).
2. **The call is web's, through egress, narrow.** `egress.verify_turnstile_token(secret, token)`
   (`egress/turnstile.py`) is the only way to the host. It builds the request itself, as the
   Withings code exchange does (ADR 0004 §7): `POST /turnstile/v0/siteverify` on
   `challenges.cloudflare.com`, a form of exactly `secret` and `response`, fixed headers, no
   redirect, a 5-second total deadline; the transport checks the bytes on the wire again. It is
   refused in the worker. `remoteip` is not sent: the visitor's address stays with us (Cloudflare
   sees it from the widget anyway). With Turnstile off, no verifier object exists and the runtime
   hook (ADR 0009) does not even resolve the host for web.
3. **Fail closed.** With Turnstile on, `POST /api/auth/signup`, `POST /api/auth/login` and
   `POST /api/auth/password/reset/request` carry the token in the `X-Turnstile-Token` header and
   check it in a route dependency, before the handler reads the database, hashes a password,
   counts against a rate limit or sends mail. Refused with 400 `turnstile_failed`: no header, more
   than one, a value egress will not send, a timeout or transport error, a status other than 200,
   a body that is not a JSON object, `success` other than JSON `true` (an expired or reused token
   is `timeout-or-duplicate`), a `hostname` other than `public_base_url`'s host, or an `action`
   other than the form's (`signup`, `signin`, `reset`). The judgement is a pure function
   (`auth/turnstile_policy.py`). A failure logs one line with the outcome only (`missing`,
   `refused`, `unavailable` or the exception class), never the token or the secret.
4. **The widget only when on.** `GET /api/auth/session` returns `turnstile: {site_key,
   script_url}` while it is on and `null` while it is off. The page renders the widget on the three
   forms only then (explicit rendering, the form's `action`), sends its token, and resets the
   widget after every submission (a token is single-use). The script URL comes from the server
   (`egress.TURNSTILE_SCRIPT_URL`), so the frontend source keeps no third-party URL (its source
   guard).
5. **The CSP gains the origin only when on.** `headers.py` builds the policy from the settings:
   off, it is byte-for-byte today's; on, `script-src` and `frame-src` gain
   `https://challenges.cloudflare.com` (Cloudflare's documented CSP for the widget) and nothing
   else changes.
6. **Legal drafts.** The cookie and privacy drafts (`docs/legal/`) name Cloudflare Turnstile as a
   conditional recipient and its cookie as set only when the operator turns it on. They stay
   drafts behind the owner's legal gate (ADR 0016 item 6).

## Alternatives

- **hCaptcha or reCAPTCHA:** the same shape, a third party either way; Turnstile needs no
  puzzle for most visitors and the home server already sits behind Cloudflare (PLAN-operations
  §9). Not adopted.
- **A proof-of-work or in-house challenge:** no third party, but weak against a motivated script
  and not what classbeat ported. Possible later behind the same dependency.
- **The token in the JSON body:** ties the check to each body model and runs after body
  validation; a header keeps the three handlers unchanged and lets the check run first.
- **Verifying in the worker through a job:** the answer is needed inside the request; the
  worker's list stays Withings and the model host.
- **Sending `remoteip`:** optional for Cloudflare; not sent, to keep the visitor's address
  ours.

## Consequences

- Web's outbound list gains one host, conditionally (roadmap Amendment 12); the egress tests
  enumerate it (`tests/test_egress_runtime.py`, `tests/test_egress_hook.py`).
- With Turnstile on, sign-in depends on Cloudflare: an outage refuses every password sign-in,
  sign-up and reset request (fail closed). Passkeys, QR approve-login and Google sign-in are not
  gated and stay available. The owner weighs this before turning it on.
- Each check costs web one outbound request of at most 5 seconds. The check runs **before** the
  rate limiter, so a refused check is not counted against any limit, and a client sending junk
  tokens costs web one siteverify call (at most 5 seconds) each; Cloudflare, not our limiter,
  bounds that traffic. A request without a token costs no call.
- Turning it on adds a third-party script, a frame and a cookie to the sign-in pages; the legal
  texts must say so before it is on.

## Unverified assumptions (fakes only; nothing here is checked against Cloudflare)

- The siteverify answer has `success`, `hostname` and `action` as documented, and `hostname` is
  the bare host of `public_base_url`.
- Tokens are printable ASCII without `;`, `&`, `=`, `#` or space and at most 2048 characters.
- The widget needs only `script-src` and `frame-src` for `challenges.cloudflare.com` under our
  CSP (no inline style or `connect-src`). An owner test with Cloudflare's test keys on staging
  verifies this before it is turned on.
