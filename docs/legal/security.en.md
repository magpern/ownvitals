# Security

> **DRAFT — legal gate for the owner.** Not reviewed by a lawyer and not approved. Written by an agent from what the code and ADRs do; bracketed items `[...]` are placeholders the owner must fill or decide. Do not publish until the owner has passed the legal gate (ADR 0016 item 6). Claim sources: [claims.md](claims.md).

Last updated: [date]. This page describes what the service does today. It makes no certification claim. [Owner: confirm each statement against the running deployment before publishing.]

## Sign-in
- Passwords are stored as argon2id hashes. Sign-in with Google is optional.
- **Two-factor authentication (TOTP) is mandatory.** Recovery codes are stored hashed and each works once. Passkeys and approving a sign-in from another device are supported.
- Sensitive actions (connect or disconnect Withings, export, delete account, change credentials, give AI consent) require a fresh second-factor confirmation.
- Sessions end after 72 hours idle or 30 days at most; you can sign out everywhere.

## Your data
- Withings access and refresh tokens are encrypted at rest (AES-256-GCM, with key rotation) and are kept out of logs (enforced by automated tests).
- Every row of health data belongs to one user and is enforced in the database (row-level security), not only in the application.
- The service talks to Withings (the worker; the web process only does the one authorisation-code exchange), to Google for sign-in if you use it, and, only if AI is enabled, to the model provider, through one controlled network layer with a fixed list of hosts. Tokens and secrets are never sent to a model.
- Logs are designed not to contain measurement values, tokens or webhook secrets (enforced by automated log-redaction tests). [Owner: confirm.]
- Webhooks from Withings are verified and only act as a trigger to fetch data.

## Your control
Export, disconnect (with Withings access revoked first) and account deletion are in the app. See the [privacy policy](privacy.en.md).

## Reporting a vulnerability
Write to [security contact address]. Please do not include real health data. We will acknowledge within [days] and will not take action against good-faith research. [Owner: add disclosure policy; breach notification to the authority within 72 hours is a procedure for the owner to adopt.]
