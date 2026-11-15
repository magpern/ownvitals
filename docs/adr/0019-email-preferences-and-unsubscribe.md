# ADR 0019: Email preferences per kind and the signed one-click unsubscribe

Status: Proposed (#459; roadmap Amendment 7, "M6 additions", classbeat #655).

## Context

Roadmap Amendment 7 gives #195 and #203 "email preferences and a signed unsubscribe link". #289
shipped the in-app preferences (`notify_email`, `notify_weekly`, `notify_tiers` on
`user_profiles`), but no outgoing mail carried an unsubscribe link and there was no unsubscribe
endpoint. Mail clients (RFC 8058) post a one-click unsubscribe with no cookie, no `Origin` and no
CSRF token, so the endpoint must be unauthenticated and still change only what it is meant to.
ADR 0007 fixes what an outbox row holds (§1) and that web's sender reaches rows only through the
outbox's definer family (§3); ADR 0002 §1 rules out a third `UserContext` issuer.

## Decision

### 1. What can be switched off

`ownvitals.notify.email_policy` lists the **suppressible** outbox kinds: `findings_waiting`,
`connection_attention`, `ai_digest_ready`. Everything else is sent whatever the preference, and
the code says so: `export_ready` (the user asked for it minutes before; ADR 0013 §4), owner
notices (mail to the admins), and every account and security mail (sign-up and email-change
confirmations, password reset, step-up and sign-in notices, passkey, TOTP, QR and MFA-failure
notices, the operator's reconnect mail), which `ownvitals.auth.mails` and `ownvitals.keys_cli`
send directly and which never pass a preference or a link. A kind added later is sent until it is
listed on purpose.

### 2. Where the preference lives

A new owned table `email_prefs` (migration 0029; forced RLS, `owner_isolation`): at most one row
per owner and suppressible kind (`enabled`, `generation`, `updated_at`; a check admits only the
suppressible kinds). No row means "on, generation 0". `generation` counts every change of the
choice. The settings (`/api/notify/email`, `ownvitals.notify.email_prefs_store`) read and write
the caller's own rows in their `user_session`. The existing `notify_email` stays what it was (the
opt-in for findings mail at queue time); this preference is checked at send time on top of it.
The outbox row is unchanged (ADR 0007 §1, T59).

### 3. The token

Built by web's sender at send time, never stored, never logged. URL-safe base64 of
`version | owner id | kind index | generation | expiry` plus a 128-bit truncated HMAC-SHA256 over a
domain string and those bytes, under a key derived for this purpose only: HKDF-SHA256 of
`app_secret_key` with the label `unsubscribe-v1` (`ownvitals.auth.keys.derive_key`, the
derivation of ADR 0003 §1). Lifetime 60 days. `verify` answers the same `None` for a forged,
tampered, expired, other-key or malformed token. Rotating `app_secret_key` voids every link sent.

The link is `public_base_url` + `/api/unsubscribe?t=<token>` (T12), in the mail's footer (each
locale's text, one placeholder) and in `List-Unsubscribe`, with
`List-Unsubscribe-Post: List-Unsubscribe=One-Click`, on suppressible kinds only. Headers fold at
998 characters for those mails, so the URL is never RFC 2047-encoded.

### 4. The endpoint

`/api/unsubscribe` is served by a middleware outside the auth middleware (as the webhook
receiver is), inside `SecurityHeaders` (CSP, `no-referrer`) and `RobotsHeaders`:

- `GET`/`HEAD`: a confirm page with a button that posts the token back; no token check, no
  database, no rate-limit slot. It never changes state.
- `POST`: one slot of a per-address limit (`UNSUBSCRIBE_IP`, 20 per 15 minutes, every POST
  counts), then `verify`, then for an authentic unexpired token the definer call below. The body
  is never read. The answer is the same page and status for every token (valid, forged, expired,
  stale, repeated), so it is no oracle.
- Anything else: 405.

### 5. The cross-user write and read: two functions in the outbox family

The endpoint and the sender hold no `UserContext` (ADR 0002 §1, ADR 0007 §3). They reach
`email_prefs` only through two `SECURITY DEFINER` functions added to the outbox family
(`docker/postgres/notify/50-outbox.sql`, owned by `ownvitals_mail_outbox`, pinned by digest,
`EXECUTE` for the app role only), amending ADR 0007 §3's list of three:

- `mail_outbox_email_pref(p_id)`: for a **claimed** outbox row of a suppressible kind, whether its
  kind's email is on and the generation; nothing for any other row. It takes the row's id, never
  an owner id, and returns no id.
- `email_unsubscribe(p_owner, p_kind, p_generation)`: switches that one kind off, only while it is
  on at exactly that generation, and counts the change (so a replay, or a link older than a change
  in the settings, does nothing). It can only switch off, only a suppressible kind, and returns
  false for an account that no longer exists. It is the family's one function that takes an owner
  id: web passes the one a verified token names.

The role gains column privileges on `email_prefs` (`SELECT` and `INSERT` of owner, kind, enabled
and generation, `INSERT`/`UPDATE` of enabled, generation and `updated_at`), no `DELETE`, nothing
on an account or health table. Guard 2 (`role_guard`) and the queue check pin both.

## Alternatives

- **A `UserContext` issued from the token:** a third issuer, which ADR 0002 §1 rules out.
- **Columns on `user_profiles`:** the definer role would need privileges on a table that holds
  birth year and sex; a separate table keeps it off health data.
- **A new definer family and role:** a second copy of the role-repair script for two functions
  that belong with the sender's claim and mark.
- **The token in the URL fragment, like the account mails:** a one-click client posts the URL it
  was given; a fragment never reaches the server.
- **A stored, single-use token:** more state to purge; the generation gives the same single use.

## Consequences

- Any app-role code can call `email_unsubscribe` for any owner and switch off one of their
  suppressible mails. The app role can already set `app.user_id` (threat model T24), so this
  shows or allows nothing new; the worst case is a missed "something is waiting" mail, never a
  disclosure, never an account or security mail.
- A link works for 60 days and only until the kind's choice changes.
- A switched-off row is marked `failed` through the existing `mail_outbox_mark` (the sender
  counts it as `suppressed` in its report and log). `failed` is therefore overloaded: it means
  "not delivered", either a delivery that gave up or a mail the user switched off. A later issue
  may add its own outcome code, which changes the mark function and its digest.
- Provisional, accepted by the Architect on 2026-11-12 (#459): the 60-day link lifetime,
  `connection_attention` being suppressible (it has no producer yet), and `failed` for a
  switched-off row.
- `email_prefs` is exported (kind, enabled, updated_at; not the generation), kept by "delete
  imported data" (a setting) and removed with the account by cascade.
- RFC 8058 also expects the mail to be DKIM-signed over these headers; that is the SMTP relay's
  configuration, not the app's (an owner step at deployment).
