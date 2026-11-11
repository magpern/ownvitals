# Privacy policy

> **DRAFT — legal gate for the owner.** Not reviewed by a lawyer and not approved. Written by an agent from what the code and ADRs do; bracketed items `[...]` are placeholders the owner must fill or decide. Do not publish until the owner has passed the legal gate (ADR 0016 item 6). Claim sources: [claims.md](claims.md).

Last updated: [date]. Applies to [service name, e.g. ownvitals] at [public hostname].

## 1. Who is responsible (controller)

[Controller name or organisation, postal address, organisation number if any]. Contact for privacy questions and to exercise your rights: [privacy contact address]. [Data protection officer: none appointed / name and contact.]

## 2. What the service does

The service connects to your Withings account at your request, fetches your measurements, stores them, analyses them and shows them to you on a dashboard. It does not give medical advice, diagnosis or treatment, and it is not endorsed by Withings.

## 3. What we store

- **Account:** email address, a password hash (never the password), your sign-in method (password and/or Google; for Google we store Google's account identifier, not your Google profile), two-factor authentication secrets and recovery-code hashes, passkey public keys if you add passkeys, language and display settings, birth year and sex (used only for reference ranges in scores), a day assignment setting.
- **Withings data you connect:** your Withings user id, connection status and scopes; access and refresh tokens (stored encrypted); devices; measurements (for example weight, body composition, blood pressure, heart rate, temperature), sleep, activity and workouts, ECG recordings including the signal, and the raw payloads received from Withings.
- **Derived data we compute:** daily values, findings, scores and their provenance.
- **Security records:** sign-in events (kind, time, IP address, approximate place) and known devices, to protect your account and show you your own activity.
- **If you turn on AI insights:** your consent record, your conversations with the assistant and weekly digests, and a redacted usage log (see section 8).

Health data is special-category data under GDPR Article 9.

## 4. Why we use it and on what legal basis

| Purpose | Data | Legal basis |
|---|---|---|
| Fetch, store, analyse and show your Withings health data | Section 3, Withings data and derived data | Your **explicit consent**, GDPR Art. 6(1)(a) and 9(2)(a), given when you connect Withings. You can withdraw it by disconnecting (section 7). |
| Provide your account, sign you in, keep it secure | Account and security records | Contract (Art. 6(1)(b)) and our legitimate interest in security (Art. 6(1)(f)) |
| Send service mail (verification, security notices, export ready, finding notices) | Email address | Contract and legitimate interest [owner: confirm; finding notices may need a setting you control] |
| AI insights | Summaries of your data (section 8) | Your **separate explicit consent**, Art. 6(1)(a) and 9(2)(a), only if you turn it on |

[Owner: confirm that consent is the right basis for every health-data purpose and that the household/non-household position has been assessed.]

## 5. Who receives data (processors and recipients)

- **Withings** is the source of your data. We receive data from it; we only send it the requests needed to fetch your data and to revoke our access.
- **Google** only if you choose "Sign in with Google": sign-in verification calls.
- **Hosting:** [hosting provider and location; the server is operated by the controller / by a provider under a processor agreement].
- **Email delivery:** [email provider, location, processor agreement]. Mail contains no health measurements.
- **Backups:** [backup location and provider; encrypted].
- **Anthropic** (model provider) **only if you enable AI insights** and consent (section 8).
- No analytics, advertising or tracking services. We do not sell data.

[Owner: complete and attach processor agreements; check international transfers for each processor.]

## 6. How long we keep data

- Your Withings data, derived data and account data are kept until you delete them or your account.
- Sign-in events and invitations: 30 days. Known devices: 90 days after last use. Session IP addresses: 30 days. Sessions end after 72 hours without use and at most 30 days after sign-in.
- Export files expire 24 hours after they are ready; the empty record of an expired export is kept 90 days.
- AI conversations: deleted after 90 days, or at once when you withdraw AI consent. The redacted AI usage log: 12 months.
- Backups: [retention period; data removed from live systems stays in encrypted backups until they expire].

## 7. Your rights and how to use them

You have the right to access, rectification, erasure, restriction, data portability, objection and to withdraw consent at any time, without affecting earlier processing.

- **Access and portability:** Settings, export. You can download your data as JSON and CSV (a fresh two-factor confirmation is required).
- **Withdraw consent / stop fetching:** disconnect Withings in the app. We revoke our access at Withings first. You can choose to delete the imported data at the same time.
- **Erasure:** delete your account in the app (fresh two-factor confirmation required). We revoke Withings access, then remove your data. If revoking fails, the app tells you to remove access in your Withings account.
- **Anything else:** write to [privacy contact address]. We answer within one month.
- **Complaints:** to the Swedish Authority for Privacy Protection (IMY), imy.se, or the supervisory authority where you live. [Owner: confirm authority.]

## 8. AI insights (off by default)

AI is **off** unless the operator has enabled it and you have given a separate, explicit consent (with a fresh two-factor confirmation). When on, the service sends computed summaries (for example medians, trends, daily values at display precision) to Anthropic's API (api.anthropic.com) so a model can write a digest or answer your questions. Not sent: tokens, email, name, user ids, raw payloads, ECG signals, times of day. Answers are labelled as AI-generated, can be wrong and are not medical advice. [Owner: add Anthropic's retention terms as recorded in the provider review, and any transfer mechanism.] Withdrawing consent stops use and deletes your conversations and digests.

## 9. Security

See the [security page](security.en.md). Health data is encrypted in transit; Withings tokens are encrypted at rest. [Owner: confirm disk/database encryption statement before publishing.]

## 10. Cookies

Only strictly necessary cookies are used, after you sign in or start signing in; none on public pages. See the [cookie statement](cookies.en.md).

## 11. Changes

We will post changes here with a new date and, for material changes, tell signed-in users.
