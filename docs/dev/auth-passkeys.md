# Auth: passkeys (WebAuthn)

How passkeys work in ownvitals: [ADR 0011](../adr/0011-passkeys-and-qr-login.md) sections 1 to
4, 8 and 9. #178 adds the backend; the Security page rows, the sign-in button and
`passkey.spec.ts` come with the frontend issue, and QR approve-login (section 5) with its own
issue. The rest of the account system is in [auth.md](auth.md).

## The pieces

| Path | What |
|---|---|
| `backend/migrations/versions/0014_passkeys.py` | `users.webauthn_handle`, and the system tables `pre_sessions`, `webauthn_credentials`, `webauthn_challenges` (guard 8; `db/tables.py` lists them) |
| `backend/src/ownvitals/auth/passkeys.py` | the shared core: failures and their reasons, the relying party, challenges, the account-row lock, list, rename, remove; no WebAuthn library, so the CLI and the reset flow load none |
| `backend/src/ownvitals/auth/passkey_model.py` | the values, the failure reasons, the relying party and passkey types, name and client-data parsing (pure); `passkeys` re-exports them |
| `backend/src/ownvitals/auth/passkey_ceremonies.py` | options, registration and authentication with the sign count; the only module that imports `webauthn` |
| `backend/src/ownvitals/auth/passkey_credentials.py` | the account's user handle, the stored-credential lookup and the ceremony's input types; no WebAuthn library; `passkey_ceremonies` re-exports them |
| `backend/src/ownvitals/auth/passkey_routes.py` | `/api/auth/passkeys`: list, rename, remove, register |
| `backend/src/ownvitals/auth/passkey_signin_routes.py` | `/api/auth/passkeys`: sign-in |
| `backend/src/ownvitals/auth/passkey_reauth_routes.py` | `/api/auth/passkeys`: re-auth |
| `backend/src/ownvitals/auth/presession.py` | the pre-session cookie `__Host-ownvitals_pre` (section 4), shared with the QR issue |
| `backend/tests/passkeytools.py` | `SoftAuthenticator`: a software authenticator with synthetic keys (ES256, Ed25519, and ES384 for the refusal test) that can break each part of a response |

## Routes

| Route | Needs | Does |
|---|---|---|
| `GET /api/auth/passkeys` | a full session | the user's passkeys (name, dates, synced, backed up, suspended) and the limit |
| `POST /api/auth/passkeys/register/options` | `require_sensitive` | registration options and a `register` challenge bound to the session; 409 `passkey_limit` at the limit |
| `POST /api/auth/passkeys/register` | `require_sensitive` | `{credential, name?}`: verifies and stores the passkey; mails the user |
| `PATCH /api/auth/passkeys/{id}` | a full session | `{name}`: 1 to 64 characters after NFKC; 404 for another user's id |
| `DELETE /api/auth/passkeys/{id}` | `require_sensitive` | removes it; 404 for another user's id |
| `POST /api/auth/passkeys/signin/options` | nothing (20 per 10 minutes per IP) | a new pre-session cookie and discoverable sign-in options |
| `POST /api/auth/passkeys/signin` | the pre-session cookie | `{credential}`: a session that still owes its TOTP, like a password sign-in |
| `POST /api/auth/passkeys/reauth/options` | a full session | options listing the user's passkeys that are not suspended; 404 `no_passkeys` |
| `POST /api/auth/passkeys/reauth` | a full session | `{credential}`: sets `reauth_at` and rotates the session; never `stepup_at` |

`{credential}` is the JSON of `PublicKeyCredential` with base64url fields (`id`, `rawId`,
`type`, `response.clientDataJSON`, `response.attestationObject` or
`response.authenticatorData`, `response.signature`, `response.userHandle`), each field bounded.
Every refused ceremony answers the same 401 `passkey_failed` (400 for a registration); the
reason goes only into `auth_events` (`detail.reason`, a closed set in `passkeys.REASONS`). When
`OWNVITALS_PUBLIC_BASE_URL`'s host is an IP address there is no relying party and every route
answers 404 `passkeys_unavailable`; locally, use `http://localhost:<port>`.

## Decisions the code makes

- **Verification order** is section 1's: the challenge is taken (`DELETE … RETURNING`, committed
  at once, so it is spent even when the rest fails) and its purpose and binding checked, then
  origin, type and `crossOrigin`, then the credential (known, the `userHandle`'s, not suspended,
  account active and, for sign-in, a confirmed TOTP), then py_webauthn (rp id hash, UP, UV,
  algorithm, signature) and last the sign count. py_webauthn is handed a stored count of 0, so
  it never refuses on the count before the signature; ours is judged after it, and a regression
  suspends the credential, writes `passkey_suspended` and mails the user.
- **The final step** runs with the account row locked `FOR SHARE` and writes the new count with
  `UPDATE … WHERE sign_count = <read value>`; the session is created (or rotated) in that same
  transaction. `reset_mfa` locks the row `FOR UPDATE` before it removes anything, so a sign-in or
  registration that races a reset leaves neither a session nor a passkey
  (`test_auth_passkeys_recovery.py`). Lock order for later work (the QR complete step of
  section 5): the account row first.
- **Rate limits.** Sign-in options: `PASSKEY_OPTIONS_IP` (20 per 10 minutes, every request
  counts). A sign-in or re-auth reserves the password sign-in buckets: per IP while the
  credential is unknown, and per (account, IP), per account (unless a known device) and per IP
  once it names an account; a success releases them, as for a password.
- **Registration** stores `aaguid` as reported (all zeros under attestation `none`), the
  transports the browser lists (only WebAuthn's names), and the backup flags (shown as "synced"
  and "backed up", never enforced). A credential id already registered, to anyone, is refused.
- **Account recovery** (`admin mfa-reset`) locks the account row, then removes the TOTP,
  recovery codes, every passkey and every Google identity and clears the password hash (ADR 0011
  §3 amendment of 2026-11-03, amending ADR 0003 §2); the reset mail names what was removed and
  carries the link that sets a new password. This closes the #165 finding that a Google identity
  linked by someone holding the account survived the reset. `keys wipe` removes passkeys and
  Google identities too but keeps the password; its mail points a Google-only account to the
  reset link.
- **Attestation** is `none` only: any other format in the attestation object is refused before
  py_webauthn sees it, so no attestation statement or certificate chain is processed.
- **Relying-party id** is not checked against the public suffix list (no dependency for it). A
  public suffix can only be configured as `webauthn_rp_id` if `public_base_url`'s host is under
  it, and browsers refuse a public-suffix rp id, so such a setting fails closed: no passkey can
  be registered or used.
- **A password reset** keeps passkeys; its link mail lists them with a link to the Security page.
  A password change sends no mail today, so it lists nothing.

## Settings

| Setting | Default | |
|---|---|---|
| `webauthn_rp_id` | unset | the relying-party id; unset: the host of `public_base_url`. Set, it must be that host or a parent domain of it, else startup refuses |
| `passkeys_per_account` | `10` | 1 to 10 |
| `passkey_satisfies_second_factor` | `false` | refused `true` until the owner answers ADR 0011 open question 1; nothing reads it as true |

## Audit kinds

`passkey_registered`, `passkey_register_failed` (not in the ADR's list; the refusal row of
section 8), `passkey_renamed`, `passkey_removed`, `passkey_signin`, `passkey_signin_failed`
(`detail.reason`; `detail.purpose` `reauth` for a failed re-auth), `passkey_reauth`,
`passkey_suspended`. They hold row ids and the IP, never a challenge, credential id, public key,
user handle or cookie (`test_auth_passkeys_logging.py`).

## Dependencies

`webauthn==3.0.1` (py_webauthn, Duo Labs, BSD-3-Clause), exact pin. Its transitive
dependencies and their licences: `cbor2` (MIT), `pyasn1` (BSD-2-Clause), `pyasn1-modules`
(BSD-2-Clause), `pyOpenSSL` (Apache-2.0), `cryptography` (already a dependency; Apache-2.0 or
BSD-3-Clause). No network code of it runs: attestation is `none`, and it builds no client.

## Tests

`tests/test_auth_passkeys_*.py` (options and settings, registration, sign-in, sign count,
management and isolation, recovery and races, pre-session, schema, logging), and the two rows
of `test_auth_stepup.py`. The hybrid transport ("Use a phone") is not exercised in CI: the
options test proves nothing restricts it, and the owner checks it on real devices (ADR 0011
Consequences).
