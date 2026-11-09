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

## Body composition (`/body`), designed for #201

No mock-up covers this screen; it is built only from components the mock-ups have
(recorded 2026-11-09 under rule 3; the owner may review it). Reference screens: `Dashboard`
(tiles, range bars, data states) and `Findings` (finding cards).

- **Layout.** The range links of the overview (30 d, 90 d, 1 y, All; `?range=`), then three
  sections, each a heading with cards in the overview's tile grid (one column on a phone):
  1. *Body composition*: one overview tile each for weight, fat ratio, fat-free mass, fat mass,
     muscle mass, hydration, bone mass, extracellular water, intracellular water, visceral fat
     and metabolic age. A tile is the overview tile unchanged (value, sparkline or phone range
     bar, typical range, data state) and its title links to the metric detail.
  2. *Arms and legs*: one card with four rows (left arm, right arm, left leg, right leg), each
     the segmental muscle mass in kg, with the left and right difference shown as a plain
     percentage (the B6 symmetric index) in the card's footer. Descriptive only: no good or
     bad colour; the tier mark of B6 appears only on its finding.
  3. *Body findings*: the finding cards of the findings inbox for the rules B2, B4, B5 and B6,
     and B1 and B3 only when the API returns them (the owner-only flag), each linking to
     `/findings`. Empty: "No body findings right now."
- **States.** Every tile and row uses the data-state pill and sentence of the overview: up to
  date, stale, importing, incomplete import, insufficient data (with progress), not supported,
  not shared, not available. A metric a device cannot measure reads "Not supported" with the
  sentence "Your device doesn't measure this." and is never empty or zero. A metric the API does
  not list at all reads "Not available". Segmental values have no read API yet, so the card
  shows "Not available" per row until they arrive.
- **Wording.** Titles use the metric names of the catalogue; the section headings are "Body
  composition", "Arms and legs" and "Body findings"; the symmetry line is "Left and right differ
  by {percent}". No new colour, icon, chart style or component.

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

## Gaps found building Connections and Onboarding (issue 196)

For the Architect to accept or send to the owner. Each was built from existing components and tokens.

- **No devices list.** The API has no devices route, so "Devices" is the "Coming later" marker with
  the Withings-app note. Battery and last-reading columns come with that route.
- **No "Sync now" or "Import 90 more days".** No web route exists for either; the buttons are not
  drawn rather than drawn dead.
- **No "Imported" range column** in "What we import": the status API has no range field. The table
  has Stream, Available, Last sync and State.
- **Dates inside the notices are dropped** (for example the "since" date of Needs reconnect): the
  status API does not carry them.
- **The disconnect code is asked by the shared step-up dialog**, after "Disconnect", not by a field
  inside the disconnect dialog.
- **The connect routes** (#296, [connections.md](../dev/connections.md)): the screens use `POST
  /api/connections/withings/start` answering `{authorize_url}`, and the callback returns to
  `/onboarding?withings=<outcome>`.
- **New classes** `ov-callout` (neutral, urgent, attn, info), `ov-kv`, `ov-choice` and `ov-steps` in
  `styles/connections.css`, built only from existing tokens.
- **Onboarding's phone "overview" step** is the real overview page: the last step links to it.

## ECG screens built beyond the mock-up (#199)

Built from existing components and tokens; the Architect accepts them here.

- The `ECG` mock-up shows the list and the viewer side by side. The UI has two routes: the list
  at `/heart/ecg` and the viewer at `/heart/ecg/:id` (a back link, no list sidebar).
- The mock-up's 9.5 s scroll is a 10 s row with Earlier/Later, plus the 30 s overview with the
  shown row outlined (a click picks a row).
- States the mock-up lacks: "Not evaluated by ownvitals" (no gate result), "Signal quality: not
  passed" (values hidden; only mapped reasons shown, never a raw code), no signal stored (no
  strip, no PDF button), "Withings code n" for an undocumented classification, and "not reported"
  for a missing value.
- Gain options are 10 (nominal) and a labelled 5 mm/mV; zoom is 100/150/200 %, both axes
  together. The mock-up's 20 mm/mV option is not built.
- The two PDF buttons of the mock-up are one "Download PDF" link.
- The All / AFib / Inconclusive filter chips are not built; the list pages by "Older recordings".
- The filtered 0.5-40 Hz view is an opt-in checkbox, off by default, labelled display-only.

## Findings inbox built beyond the mock-up (#200)

Built from existing components and tokens; the Architect accepts them here.

- The route is `/findings`. The tab (`tab=history`), the tier (`tier=`), the area (`area=`) and the
  chosen finding (`id=`) are in the address, so a deep link and the back button work; defaults are
  left out.
- The API sends no titles, counts or status line, so the mock-up's per-tab counts, its "N findings
  need a look" line and its filter totals are not built. Names and "why" texts come from the
  `health.rule.*` catalogue, worded from the finding's evidence; a text whose numbers the evidence
  lacks is not shown.
- "Seen" and "New" follow the API's `seen_at`; the screen does not mark a finding seen (no write
  endpoint). A backfill finding shows "From history" instead.
- "What it isn't" is one fixed text per tier (`findings.isnt.*`, health wording).
- The evidence chart is the metric's own daily series from the metric endpoint, cut to the 28 days
  before the window through the day after it, with the typical range. The mock-up's shaded window
  is not drawn. "Open <metric>" and "Readings on <day>" link to the metric and day screens.
- The "Provenance" block of the mock-up is the Data quality, Confidence and Sources rows.
- The area filter is a select, not chips; the list pages by "Older findings".
- Hidden findings are absent and an experimental finding appears only when the API sends it, marked
  "Only you see this".
