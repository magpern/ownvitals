# UI design: "Quiet instrument"

**Status: accepted by the owner, 2026-10-31** (#85). This is the design of the ownvitals UI. It
designs what PLAN §8 (MVP UI) and §6 (identity) require and changes no specification text.

The reference is the set of mock-ups in [mockups/](mockups/README.md): the canvas sources, a
standalone HTML page per screen (light, and dark with `?theme=dark`) and a light and a dark
screenshot of each. The owner's design canvas is
<https://claude.ai/artifact/HQrrU1y3u2sxh29c4Xqp7p> (private; agents work from the repository
copy, which is identical to it).

## Rules for agents

1. **Build what the mock-ups show.** Layout, components, states, tokens, type, icons and the
   wording patterns are the specification of the UI. Open the screenshot (`mockups/png/`) and
   the HTML page (`mockups/html/`) of every screen an issue touches before writing code.
2. **Don't invent.** No new screens, components, colours, fonts, icons, chart styles or copy
   patterns that the mock-ups don't have. Reuse the closest existing one.
3. **Gaps go to the Architect, changes to the owner.** A screen or state the mock-ups don't
   cover (for example body composition or nerve health in full) is designed by extending the
   existing components and recorded here before it's built. A change to the design itself needs
   the owner, and the mock-ups and this file change in the same PR.
4. **The specification wins.** Where a mock-up and the frozen specification or an ADR differ
   (what is shown to whom, step-up, tiers, health data in admin), the specification is right and
   the Architect fixes the mock-up.
5. **Binding vs illustrative.** Binding: structure, components, states, tokens, value styles,
   tier marks and the way things are worded. Illustrative: the example values, names, dates and
   counts (all synthetic), and the "Coming later" items, which belong to their milestones
   (passkeys and QR approve-login in M3, AI in M7).
6. **Port, don't paste.** The mock-up HTML uses inline `style` attributes and a design-canvas
   runtime; the app may not (CSP, `frontend/src/sources.test.ts`). Rebuild each screen as React
   components with classes and `--ov-*` tokens. The reviewer of a UI PR compares the result with
   the screenshots.

## The idea

ownvitals is an independent analysis tool for your own Withings data, not a wellness app. The
design is a calm, precise instrument: every number sits on **your own typical range**, says
**who produced it**, and **admits what is missing**. The logo is the idea in miniature: a reading
(dot) on a personal band.

| # | Principle | Spec it serves |
|---|---|---|
| 1 | **Your history is the reference.** Every series is drawn on the user's rolling typical range (median ± 1.5 MAD over 90 days); comparisons are with their past, never population charts | Trend tier, §7.1–7.2 |
| 2 | **Gaps stay gaps.** Missing days are hatched and labelled, lines break, an unreadable stream is never zero | §4.2 capabilities, §4.4 completeness |
| 3 | **Say who said it.** Three value styles: reported by Withings (solid square mark), calculated by ownvitals (band mark), estimated by ownvitals (italic, dotted, "est.", dashed box, owner flag only) | D9, §7.3 (estimates never look certified) |
| 4 | **Freshness on every screen.** A status line under the top bar: last reading, last sync, running or incomplete imports, broken connections | §8 |
| 5 | **Every number is traceable.** Overview → metric → day → reading → provenance (method, `algo_version`, `input_revision`, `input_fingerprint`) | §5 provenance |
| 6 | **Quiet by default.** No streaks, rings, badges or nudges; colour only when something deserves a look | PLAN-operations §9.4 design rule |

## Information architecture

```
Signed out    /signin  /signin/email  /signup  /reset  /signup/confirm  /signed-out
Second factor /two-factor  /two-factor/setup (QR → code → recovery codes)
App (ready)   /                    Overview: "Needs a look", then tiles by area
              /body, /heart, /nerve, /sleep, /activity   area pages
              /m/:metric           metric detail (range, typical range, trend, change points)
              /m/:metric/:day      day: everything recorded, reading drawer with provenance
              /heart/ecg[/:id]     ECG list and viewer (+ PDF)
              /findings            Live | History, tier and area filters
              /connections         Withings: streams, capabilities, devices, disconnect
              /settings            Profile and display | Sign-in and security | Notifications | Your data
                                   the tabs are /settings/profile, /settings/security,
                                   /settings/notifications and /settings/data; /settings shows the first
              /admin               Overview | Accounts | Jobs | Withings app | Flags | Audit log
```

Every level is a URL (deep links, back button, `?range=90d`). A finding opens its evidence chart
already zoomed to its window, and from there the readings that drove it.

## Screens

Each screen is `mockups/src/<Name>.dc.html`, `mockups/html/<Name>.html` and
`mockups/png/<Name>.png` / `<Name>.dark.png`.

| Name | Shows | Spec |
|---|---|---|
| `Main` | Design language: principles, chart grammar, value styles, tiers, data states, type, colour, controls | D9, §4.2, §5, §8 |
| `Rail` | The navigation rail component used by every app page (no screenshot; it's in every page's) | §8 |
| `Dashboard` | Overview: freshness line; "Needs a look"; tiles with sparkline and typical range; not-supported, insufficient-data and incomplete-import tiles | §7.1 (7), §8 |
| `Metric` | Weight over 6 months: band, smoothed line, 9-day gap, change point, unconfirmed readings; latest, typical range, Theil–Sen trend, data quality; the day's readings; method | §7.1 |
| `Reading` | Day: everything recorded; reading drawer with raw value × 10^unit, attrib, device, source time zone; quality gates passed; provenance | §5, §7.1 (2–3) |
| `ECG` | Recording list; Withings' result attributed; reported intervals; 25 mm/s × 10 mm/mV strip with calibration pulse; 30 s overview; experimental estimates boxed and dashed; doctor PDF. Shows the "signal quality passed" case only: the other overlay states ("not evaluated by ownvitals", hidden findings) and what the doctor PDF carries follow ADR 0006 (#46), and the Architect adds them here when it merges | §7.2 E1–E3, §7.3, ADR 0006 |
| `Findings` | Live / History (the History tab is `Findings.history.html` and `.png`); tier badges; confidence and data quality; evidence chart; "why you're seeing this" and "what it isn't" | §7.1 (6–7), §7.2 |
| `Onboarding` | Phone: connect (what we read, what we never do, Withings+ note), import progress per stream, phone overview with range bars | M6 done-when, §9.4 |
| `Connections` | Status, how updates arrive, per-stream availability and completeness, devices, other states (not connected, needs reconnect, Withings+ needed, not shared), disconnect dialog with keep/delete and step-up | §4.2–4.4, §6, ADR 0004 |
| `Auth` | Sign in (password or Google), second factor, authenticator setup, recovery codes, account menu with sign out, signed-out page and its reasons. The Google button follows Google's branding guidelines: the standard "G" mark from the app's own origin, an approved label, light and dark variants (owner, 2026-11-01) | §6, ADR 0003 |
| `Settings` | Analysis profile (birth year, sex for score bands, day assignment), display, overview layout, notifications, experimental and AI, export and delete | §6, §7.4, D11 |
| `Security` | Password, Google, email, passkeys and QR approve (M3), second factor and recovery codes, sessions with place and IP, sign out everywhere, recent security activity, the shared step-up dialog | §6, ADR 0003 §3–4 |
| `Admin` | Admin band ("no health data here"), counts, accounts, job counts, Withings app (read-only), flags that only tighten, audit log | ADR 0002 §4, ADR 0005, threat model T26, T52–T54 |

## Visual system

- **Type:** IBM Plex Sans for UI, Plex Mono for ids, versions and raw values; tabular figures for
  every number. OFL-1.1 (on the accepted licence list), self-hosted from the app's origin so the
  CSP stays `'self'`. The mock-ups load Plex from Google Fonts; the app never does.
- **Colour:** the tokens in `mockups/src/ov.css` (`.ov` light, `.ov[data-theme="dark"]` dark):
  neutral ground, teal brand (kept from `tokens.css`), deep-blue series, teal-tint typical range.
  Meaning is carried by blue and orange, not red and green. Urgent red is kept for a Withings
  AFib-positive result and a broken connection. A QR code stays dark on light in both themes.
- **Tiers:** Device (solid square), Trend (band), Experimental (dashed diamond, dashed borders
  everywhere), System (ring).
- **Data states:** up to date, stale, importing n %, incomplete import, insufficient data (with
  progress toward the minimum), not supported, not shared, not available, needs reconnect,
  Withings+ needed. One component, one wording, everywhere.
- **Step-up:** every button for an action on the PLAN §6 list (ADR 0003 §4) and every admin
  write carries a lock icon and opens one shared "Confirm it's you" dialog; a code covers 10
  minutes (5 for admin writes). Inline admin controls (the sign-up switch) say so in their card.
- **Admin:** an inverted band on every admin page states that health data isn't visible there.
  The admin area shows only what `ownvitals_maint` may read (ADR 0002 §4): accounts, connection
  status, job counts per state (`queue_stats()`, ADR 0005), flags, the audit log and the
  non-secret columns of `withings_apps`. Webhook delivery, sync freshness and the API budget
  are Prometheus metrics, not admin pages.
- **Email:** every email says only that something is waiting, with a link (ADR 0007, T59); the
  Settings copy says so.
- **Phone:** the rail becomes bottom tabs; charts become range bars (where today sits in the
  typical range); touch targets are at least 44 px.

## Decisions (owner, 2026-10-31)

| Question | Decision |
|---|---|
| Direction | "Quiet instrument", exactly as in the mock-ups |
| Fonts | IBM Plex Sans and Plex Mono, self-hosted |
| Health score on the overview | Shown as a plain number with its components and a "How it's computed" link |
| Phone overview | Range bars instead of charts |
| Theme | Light, dark and "System" (the default), chosen in Settings |

## Building it

- `frontend/src/styles/tokens.css` takes the tokens of `ov.css` as `--ov-*` (series, band, tier,
  attention, urgent, ECG paper, QR) in the first M6 issue. The token-only colour rule in
  `sources.test.ts` stays. The theme setting needs a `data-theme` override next to
  `prefers-color-scheme`.
- Charts: uPlot (MIT, small, handles gaps as `null`) for time series, as PLAN §8 allows; the
  ECG on canvas at a calibrated px/mm; the PDF prints to scale.
- Fonts: `@fontsource/ibm-plex-sans` and `@fontsource/ibm-plex-mono` (OFL-1.1); the licence and
  source-scan tests already allow font files.
- No third-party host, no inline style or script: the design works under the current CSP.
- M6 is split into issues by screen group (shell and tokens first, then overview, metric and
  day, ECG, findings, connections and onboarding, settings and security). The UI parts of #10,
  #11 and #12 (sign-in, second factor, step-up, admin) follow `Auth`, `Security` and `Admin`.
