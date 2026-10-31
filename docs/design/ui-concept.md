# UI concept: "Quiet instrument" (proposal)

Status: **proposal for the owner**, 2026-10-31. Nothing here is decided until the owner picks a
direction; the chosen one is then recorded in an ADR and split into M6 issues. It changes no
specification text: it designs what PLAN §8 (MVP UI) and §6 (identity) already require.

Mock-ups of every screen below are on a design canvas (private to the owner):
<https://claude.ai/artifact/HQrrU1y3u2sxh29c4Xqp7p>. All example data in them is synthetic.

## The idea

ownvitals is an independent analysis tool for your own Withings data, not a wellness app. The
design is a calm, precise instrument: every number sits on **your own typical range**, says
**who produced it**, and **admits what is missing**. The logo is the idea in miniature: a reading
(dot) on a personal band.

### Six principles

| # | Principle | Spec it serves |
|---|---|---|
| 1 | **Your history is the reference.** Every series is drawn on the user's rolling typical range (median ± 1.5 MAD over 90 days); comparisons are with their past, never population charts | Trend tier, §7.1–7.2 |
| 2 | **Gaps stay gaps.** Missing days are hatched and labelled, lines break, an unreadable stream is never zero | §4.2 capabilities, §4.4 completeness |
| 3 | **Say who said it.** Three value styles: reported by Withings (solid square mark), calculated by ownvitals (band mark), estimated by ownvitals (italic, dotted, "est.", dashed box, owner flag only) | D9, §7.3 (estimates never look certified) |
| 4 | **Freshness on every screen.** A thin status line under the top bar: last reading, last sync, running or incomplete imports, broken connections | §8 |
| 5 | **Every number is traceable.** Overview → metric → day → reading → provenance (method, `algo_version`, `input_revision`, `input_fingerprint`) | §5 provenance |
| 6 | **Quiet by default.** No streaks, rings, badges or nudges; colour only when something deserves a look | §9.4 design rule (not "substantially similar" to the Withings app) |

## Information architecture

```
Signed out    /signin  /signup  /reset  /signup/confirm  /signed-out
Second factor /two-factor  /two-factor/setup (QR → code → recovery codes)
App (ready)   /                    Overview: "Needs a look", then tiles by area
              /body, /heart, /nerve, /sleep, /activity   area pages
              /m/:metric           metric detail (range, typical range, trend, change points)
              /m/:metric/:day      day: everything recorded, reading drawer with provenance
              /heart/ecg[/:id]     ECG list and viewer (+ PDF)
              /findings            Live | History, tier and area filters
              /connections         Withings: streams, capabilities, devices, disconnect
              /settings            Profile and display | Sign-in and security | Notifications | Your data
              /admin               Overview | Accounts | Jobs | Withings app | Flags | Audit log
```

Every level is a URL (deep links, back button, `?range=90d`). A finding opens its evidence chart
already zoomed to its window, and from there the readings that drove it.

## Screens on the canvas

| Board | Shows | Spec |
|---|---|---|
| Design language | Principles, chart grammar, value styles, tiers, the data-state vocabulary, type, colour, controls | D9, §4.2, §5, §8 |
| Overview | Freshness line; "Needs a look" (live findings); tiles with sparkline and typical range; not-supported, insufficient-data and incomplete-import tiles | §7.1 (7), §8 |
| Metric detail | Weight over 6 months: band, smoothed line, 9-day gap, change point, unconfirmed readings; latest, typical range, Theil–Sen trend, data quality; the day's readings; method | §7.1 |
| Day and reading | Everything recorded that day; reading drawer with raw value × 10^unit, attrib, device, source time zone; quality gates passed; provenance | §5, §7.1 (2–3) |
| ECG | Recording list; Withings' result attributed; reported intervals; 25 mm/s × 10 mm/mV strip with calibration pulse; 30 s overview; experimental estimates boxed and dashed; doctor PDF | §7.2 E1–E3, §7.3, ADR 0006 |
| Findings | Live / History tabs; tier badges; confidence and data quality; evidence chart; "why you're seeing this" and "what it isn't" | §7.1 (6–7), §7.2 |
| Onboarding (phone) | Connect (what we read, what we never do, Withings+ note), import progress per stream, phone overview with range bars | M6 done-when, §9.4 |
| Connections | Status, how updates arrive, per-stream availability and completeness, devices, other states (not connected, needs reconnect, Withings+ needed, not shared), disconnect dialog with keep/delete and step-up | §4.2–4.4, §6, ADR 0004 |
| Sign in and out | Sign in (password or Google), second factor, authenticator setup, recovery codes, account menu with sign out, signed-out page and its reasons | §6, ADR 0003 |
| Settings | Analysis profile (birth year, sex for score bands, day assignment), display, overview layout, notifications, experimental and AI, export and delete | §6, §7.4, D11 |
| Security | Password, Google, email, passkeys and QR approve (M3), second factor and recovery codes, sessions with place and IP, sign out everywhere, recent security activity, the shared step-up dialog | §6, ADR 0003 §3–4 |
| Admin | Admin band ("no health data here"), counts, accounts, job counts, Withings app (read-only), flags that only tighten, audit log | ADR 0002 §4, ADR 0005, threat model T26, T52–T54 |

## Visual system

- **Type:** IBM Plex Sans for UI, Plex Mono for ids, versions and raw values; tabular figures for
  every number. OFL-1.1 (on the accepted list), self-hosted so the CSP stays `'self'`.
- **Colour:** neutral ground, teal brand (kept from `tokens.css`), deep-blue series, teal-tint
  typical range. Meaning is carried by blue and orange, not red and green. Urgent red is kept
  for a Withings AFib-positive result and a broken connection. Light and dark are both designed;
  every text colour is meant to pass 4.5:1.
- **Tiers:** Device (solid square), Trend (band), Experimental (dashed diamond, dashed borders
  everywhere), System (ring).
- **Data states:** up to date, stale, importing n %, incomplete import, insufficient data (with
  progress toward the minimum), not supported, not shared, not available, needs reconnect,
  Withings+ needed. One component, one wording, everywhere.
- **Step-up:** actions on the PLAN §6 list carry a lock icon and open one shared "Confirm it's
  you" dialog; a code covers 10 minutes.
- **Admin:** an inverted band on every admin page states that health data isn't visible there.
- **Phone:** the rail becomes bottom tabs; charts become range bars (where today sits in the
  typical range); touch targets are at least 44 px.

## Fit with what's built

- `frontend/src/styles/tokens.css` grows the new tokens (series, band, tier, attention, urgent,
  ECG paper). The token-only colour rule in `sources.test.ts` stays as it is.
- Charts: uPlot (MIT, small, handles gaps as `null`) for time series, as PLAN §8 allows; ECG on
  canvas at a calibrated px/mm; the PDF prints to scale.
- Plex would be the first font package in the bundle (`@fontsource/*`, OFL-1.1); the licence and
  source-scan tests already allow fonts.
- No third-party host, no inline style or script: everything above works under the current CSP.

## Decisions for the owner

1. Direction: "Quiet instrument" as proposed, or changes to it.
2. Fonts: IBM Plex (self-hosted) or keep the system font stack.
3. Health score on the overview: a plain number with its components (proposed), or hidden
   until M4 is verified on real data.
4. Phone: range bars instead of charts on the phone overview (proposed).

Once decided: an ADR records the direction, `tokens.css` and the shared components come first,
then one M6 issue per screen group.
