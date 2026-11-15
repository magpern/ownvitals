# Auth: Google re-authentication and refused-callback logging

Written by #247 (H1, roadmap Amendment 7). The decision is ADR 0003's amendment of 2026-11-08;
this file is how it works in the code. The rest of Google sign-in is in [auth.md](auth.md); where
the two differ on re-authentication, this file is current (auth.md's "`auth_time` within 5
minutes" predates it).

## The rule

Google ignores `max_age=0` and the `claims` request for `auth_time`, so its ID tokens carry no fresh
`auth_time`. `auth/oauth_reauth_policy.py` (pure) decides, after state, PKCE, nonce, the ID token,
the session and the identity have been checked:

| Situation | Outcome (`/account?oauth=…`) | `why` (log, `reauth_failed` event) |
|---|---|---|
| The account has a password (at the start) | 403 `reauth_use_password`, no flow | — |
| The account has a password (at the callback, under the row lock) | `reauth_use_password` | `has_password` |
| More than 300 s from the start to the callback (database clock) | `reauth_expired` | `flow_old` |
| `email_verified` not true | `invalid` | `email_unverified` |
| `auth_time` present and more than 60 s in the future | `reauth_stale` | `auth_time_future` |
| Otherwise (no `auth_time`, an old one or a fresh one) | `reauthed`: `reauth_at` set, session rotated | — |

A Google re-authentication never sets `stepup_at`: `require_sensitive` still needs a TOTP or
recovery code within 10 minutes. `prompt=login` is not sent and changes nothing.

Where it lives:

- `oauth_routes.start`: the 403 for an account with a password (`oauth_outcomes.has_password`);
  401 `not_authenticated` if the account is gone (unreachable behind a live full session).
- `oauth_store.take_flow`: returns the flow with its age (`now() - created_at`, `Flow.age`) from
  the same `DELETE ... RETURNING` that takes it, before the code exchange; the callback passes it
  on as `Request.flow_age` (#282).
- `oauth_outcomes._reauth`: locks the user row first (`has_password(..., lock=True)`, `FOR NO KEY
  UPDATE`), then reads the identity, applies the rule, records `reauth_failed` with
  `{"why", "provider"}` or `reauth`. A password set or an identity unlinked while the callback
  runs is seen (the writer holds the user row, so the read waits for it), and the lock still lets
  a foreign-key check (`FOR KEY SHARE`) through: `tests/test_auth_oauth_reauth_races.py`.

## Logging

Every callback that does not end `ok` logs one `WARNING` from `ownvitals.auth.oauth_routes`:

```
oauth callback refused: provider=google purpose=reauth outcome=reauth_use_password why=has_password
```

`purpose` is `unknown` when there was no flow. `why` is a fixed word: `no_flow`,
`provider_mismatch`, `state_mismatch`, `provider_error=<code>` (RFC 6749 / OIDC error codes by
name, anything else `unrecognized`), `no_code`, `no_session`, `other_user`, `provider: <check>`
(the `ProviderError` text, a fixed check name such as `nonce mismatch`), the rule's words above,
`not_the_flows_user`, `account_gone`, `not_linked_to_this_user`, `session_gone`,
`linked_to_another_user`, `identity_gone` (a Google sign-in whose identity was removed or moved
by the time it held the user row; `account_gone` there means the account went), `user_disabled`,
`email_taken`, `no_invite`, `signup_closed`, `invite_gone`. A link whose session was revoked
meanwhile logs `session_gone`. Never a code, state, nonce, token, email or claim value.

## Tests and fakes

```bash
cd backend
uv run pytest tests/test_oauth_reauth_policy.py              # the table, no database
uv run pytest tests/test_auth_oauth_reauth.py tests/test_auth_oauth_logging.py   # needs the test Postgres
```

- `tests/oidcfake.py` behaves like the live Google: no `auth_time`, asked or not. Pass
  `auth_time=` as a claim override for a fresh one, or set `fake.honours_claims_request = True`
  for a provider that answers the `claims` request.
- `e2e/fake-oidc/server.mjs` sends no `auth_time` either; an identity with `auth_time: 'now'` gets
  one. `e2e/specs/google-reauth.spec.ts`: a Google-only account signs up, activates 2FA,
  re-authenticates with Google, still needs a code, then sets a first password; an account with a
  password gets `reauth_use_password` at the start. `helpers/api.ts` posts from the page with the
  CSRF token (no screen offers these actions yet).

## Owner's live check (optional; agents make no live call)

With Google enabled, on a Google-only account more than 10 minutes after sign-in, start a
re-authentication through Google: expect `/account?oauth=reauthed` and no `oauth callback refused`
line. On an account with a password, "Use Google instead" should answer `reauth_use_password`.
Report only the `why` words, never a token or claim value.
