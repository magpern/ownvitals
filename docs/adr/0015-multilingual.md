# ADR 0015: Multilingual support (milestone G)

Status: Proposed (roadmap Amendment 7, 2026-11-08). The owner questions are listed at the end, each
with the default that applies until it is answered. Ported from classbeat ADR 0023 (milestone ML,
classbeat #657) at `magpern/classbeat@77f0ccf`. The differences are listed under "Differences
from classbeat".

## Context

ownvitals is English only. The frontend is small: about 3,400 lines of `.tsx`, the sign-in, sign-up,
two-factor, reset, security and admin pages. M6 will add about ten screens (PLAN §8). The household
is Swedish. classbeat spent nine issues (ML0 to ML8) retrofitting about 1,500 strings across 170
files. Doing it before M6 means each screen is written through the catalogue from its first commit.

The backend mostly sends codes already. A few places send English sentences (G4 audits them).
Findings store structured `evidence` and a `rule_id`, never prose (ADR 0010). Mail is fixed text per
outbox kind (ADR 0007, threat model T59): `notify/templates.render` takes the kind, the route and the
base URL and nothing else. The auth mails in `auth/mails.py` are separate. No language preference is
stored, and nothing reads `Accept-Language`.

## Decision

**1. Languages.** `en` is the source and the fallback for every string; `sv` is added. Codes are
bare language codes. One checked-in manifest, `frontend/src/i18n/locales.json`, lists the supported
locales. The frontend pins its catalogue map to it, and the backend loads it as `SUPPORTED_LOCALES`
(shipped in the image; a test reads the repository copy). The setting `OWNVITALS_LOCALES` (default
`en,sv`, `en` always on) can switch a supported language off. It cannot add one.

**2. Choice and storage.** The browser resolves the language as the first of:

1. `users.locale`, set in Settings with `PUT /api/account/locale` and returned with the session;
2. the `localStorage` key `ov-locale`, set by the picker on signed-out pages (read in try/catch);
3. the first supported entry of `navigator.languages`;
4. `en`.

There is no new cookie and no language in the URL. The server's request locale is an optional
`locale` body field if supported, otherwise a bounded `Accept-Language` parse (256 bytes, 8
entries), otherwise `en`.

A new account stores the language in effect at sign-up:

- email sign-up: through the pending sign-up row;
- Google sign-up: through the OAuth flow row, because the callback is a provider redirect and
  carries no body.

`NULL` means "follow this browser". It holds for existing accounts and for accounts made by the CLI
or an admin. Which language such an account's mail uses is set out in §7. The column is an allowlisted enum
(`CHECK (locale IN (...))`, widened in the migration that adds a language), never free text.

**3. Frontend runtime.** An in-house typed catalogue, with no new dependency:

- the catalogue is split by area (`en/<area>.ts`, for example `auth`, `account`, `codes`, `health`,
  one per M6 screen), so parallel issues own separate files. `en/` is the source, and each
  `sv/<area>.ts` `satisfies` its English file's shape, so a missing key fails `typecheck`;
- `t(key, params)` takes named placeholders;
- `plural()` uses `Intl.PluralRules`;
- a `Trans` component has named slots for links and emphasis: no markup in strings and no
  `dangerouslySetInnerHTML`;
- `sv` loads as a separate chunk through a static import map. The CSP is unchanged.

A pre-paint script sets `<html lang>`, only to a manifest code. It is a same-origin static file
(`public/locale-init.js`), never an inline script, because the CSP is `script-src 'self'`. The admin area stays English and is
exempt from the guard.

**4. Formatting.** One `format.ts` on `Intl` with the resolved locale. Numbers use the locale's
decimal separator, and units stay SI and are never converted (kg, %, bpm, ms, mmHg).

Days and times follow the account's **day setting**, as day assignment does
(`derived/daily.py`):

- a day is the API's date as it is, never re-zoned in the browser;
- a reading's time is shown in the zone the day setting uses for it (`day_tz` under `fixed_tz`, the
  reading's own zone under `reading_tz`);
- the browser's zone is used only for times that belong to no reading, such as sessions and mail.

This keeps a day on the chart the same day the analysis used. Week start and month names come from `Intl`. Charts take their tick formatters from
`format.ts`.

**5. Health wording.** Rule names, finding texts, tier labels and disclaimers are catalogue entries
keyed by `rule_id` (variants included, Amendment 6) and tier. They are worded in the frontend from
`evidence`, never stored translated. Every such key is listed in `docs/dev/i18n.md` as
**health wording**: the owner's medical-claims review at M8 (PLAN-operations §9.3) covers both
languages. No translation may change a tier, a threshold or a confidence, or add advice the English
text does not give.

**6. Server sentences (G4).** A backend sentence shown to the user becomes a code plus parameters
(numbers, dates as ISO strings, allowlisted enums). The frontend words the code and shows the English
fallback for an unknown code. Log lines, admin-only output and the CLI stay English.

**7. Mail (G5).** The outbox row does not change: it holds exactly kind, recipient and link
(ADR 0007 §1, threat model T59). Web's sender looks up the locale when it sends, the same way it
looks up the address (ADR 0007 §2): through `ownvitals.auth` (guard 8), from the recipient's
`users.locale`. The value is an allowlisted code. Its only use is to pick one of the fixed template
tables, so nothing else from a row or an account reaches the text, and T59 holds unamended. Which
locale applies:

- **outbox mail to a user:** the stored `users.locale`; `NULL` means `en`;
- **owner notices** (to the admins, from a system row): `en`, like the admin area;
- **auth mail sent inside a request** (sign-up confirmation, "account exists", reset): the
  recipient's stored locale if an account exists and has one, otherwise the request locale (§2).
  Pre-account mail always uses the request locale;
- **security notices** (TOTP enrolled, failed second factors, email changed, identity linked,
  passkey added or suspended, QR login approved): the same rule as auth mail inside a request,
  the recipient's stored locale at send time, otherwise the request locale, otherwise `en`
  (amendment of 2026-11-10 below);
- **anything else:** `en`.

**Amendment (2026-11-10, owner delegated the call, provisional; #282).** The security notices
were "anything else: `en`". They now follow the recipient's stored `users.locale`, looked up when
the notice is sent through `ownvitals.auth.recipients` (guard 8), falling back to the request
locale (§2), then `en`. The lookup is the one auth mail inside a request already uses, so it
returns only a manifest code that is also offered, and its one use is still to pick a fixed
template table: the texts stay in the per-locale tables, no new placeholder is added, nothing is
written to the outbox and T59 holds unamended.

The rest:

- `notify/templates` becomes one fixed template table per locale, with identical keys and the
  `{link}` placeholder exactly once.
- `Content-Language` comes from the allowlisted code.
- Month and date words come from the catalogue, never `strftime("%B")` or `setlocale`.
- Golden files exist per kind and locale.

**8. Translation process (G6 and later).** Swedish text is written only in translation PRs: a
Sonnet translator as Developer and an independent Opus-class Reviewer who checks meaning, tone,
placeholders, plurals and the health-wording rule in §5. A screen PR adds English keys only. It
lists them as untranslated, and CI allows untranslated keys only from that list, which the next
translation PR empties.

**9. Guards (CI).**

- A hard-coded string guard over `.tsx` (JSX text and user-facing attributes such as `aria-label`,
  `title`, `placeholder` and `alt`), with an allowlist that only shrinks (admin pages, brand names,
  units).
- Every catalogue has the same keys, except the untranslated list.
- Placeholder names are equal to English, and placeholders are bare names with no format spec or
  conversion.
- No markup in strings.
- Plural categories are present for each locale.
- A new manifest entry fails CI until its frontend catalogue, mail catalogue, `CHECK` constraint and
  picker label all exist.

## Alternatives

- **A library (i18next, FormatJS).** More features than needed, a new dependency under the licence
  and audit tests, and runtime message parsing. The typed catalogue makes missing keys a type error.
  classbeat ran the in-house version through nine PRs without trouble.
- **Translating stored text.** Rejected: findings and scores are data. Wording them at display time
  keeps the analysis reproducible (ADR 0010) and language-free.
- **Language in the URL (classbeat ADR 0027).** No search-engine need for a private signed-in app.
  It can be added later without changing §2's storage.
- **Waiting until after M6.** Rejected: it costs the classbeat-sized retrofit.

## Consequences

- From G2's merge on, every UI PR goes through the catalogue. The Reviewer checks the guard is green
  and the allowlist did not grow.
- Mail templates double. Each new outbox kind lands with an English template and an untranslated
  entry, which the next translation PR fills.
- The medical-claims review at M8 covers two languages.
- M7 (AI, PDF) inherits the locale. This ADR does not decide the language of model output or of the
  doctor PDF (owner questions 4 and 5).

## Differences from classbeat

| classbeat ADR 0023 | ownvitals |
|---|---|
| en, sv, da | en, sv (Danish is a catalogue-only addition later, through the checklist) |
| Dates in the browser's time zone | Days and reading times follow the account's day setting, so a chart day is the analysis day |
| Legal and consent text translated with no owner sign-off | Health wording and legal text go to the owner's M8 reviews (§5, P3) |
| Language-prefixed URLs (ADR 0027) | None |
| Mail locale chosen inside the mail catalogue | The sender looks up the locale at send time, like the address; the outbox row is unchanged (T59) |

## Owner questions (defaults apply until answered)

1. **Languages:** default English and Swedish. Add Danish or another language?
2. **Translation review:** default agent translator plus an independent Opus-class Reviewer, with
   health wording listed for the M8 medical-claims review. Do you want to proofread the Swedish
   health wording yourself before M8?
3. **Admin area:** default English only.
4. **AI output language (M7, ADR 0014):** default the user's language. Or English only?
5. **Doctor PDF language (M7, ADR 0013):** default the user's language, with a "language of this
   report" choice in the export dialog.
6. **Picker placement:** the design mock-ups have no language picker. Default: a compact picker in
   the signed-out page footer and a "Language" row in Settings → Profile, styled with the existing
   select control.
