# Cookie statement

> **DRAFT — legal gate for the owner.** Not reviewed by a lawyer and not approved. Written by an agent from what the code and ADRs do; bracketed items `[...]` are placeholders the owner must fill or decide. Do not publish until the owner has passed the legal gate (ADR 0016 item 6). Claim sources: [claims.md](claims.md).

Last updated: [date].

**The public pages of this site set no cookies and use no analytics, advertising or tracking, and load no third-party content.**

When you open the app (including the sign-in page), sign in or start signing in, the app sets only strictly necessary cookies (no consent banner is needed for these). All are first-party, `Secure`, and have no `Domain`.

| Cookie | Purpose | Lifetime |
|---|---|---|
| `__Host-ownvitals_session` | Keeps you signed in (HttpOnly) | Up to 30 days; ends after 72 hours idle |
| `__Host-ownvitals_device` | Recognises a known device for sign-in security (HttpOnly) | 365 days |
| `__Host-ownvitals_csrf` | Protects forms against cross-site requests; set whenever the app loads while you are signed out | 30 days |
| `__Host-ownvitals_oauth` | Carries the Google sign-in step (HttpOnly) | 10 minutes |
| `__Host-ownvitals_pending` | Carries a pending sign-up step (HttpOnly) | 24 hours |
| `__Host-ownvitals_pre` | Carries the step before a session exists (HttpOnly) | 5 minutes |

The app also stores your light/dark theme choice in your browser's local storage (key `ownvitals.theme`) your language choice (key `ov-locale`) and that you have seen the cookie notice (key `ownvitals.cookie-notice`, value is a version number); these are settings kept on your device, never sent to us. You can delete cookies and site data in your browser; you will then be signed out. [Owner: confirm the app stores nothing else in the browser before publishing.]
