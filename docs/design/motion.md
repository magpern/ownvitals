# Motion and liveliness (M10): PROVISIONAL

**Status: PROVISIONAL, delegated design judgement.** The owner asked on 2026-11-11 for a livelier
app ("use your best judgement") and delegated the design to the Architect. This is a proposal, not
an accepted design. **The owner reviews it at the M8 review (or earlier on request); the M10
implementation issues (#424-#426) must not start until the owner has accepted it or amended it.**
It extends [README.md](README.md) ("Quiet instrument") and changes no specification text. Where it
conflicts with the README it says so under "Owner decisions needed".

## Principle: lively, not loud

Motion explains change (a value arrived, a state changed, something loaded) and rewards arrival
once. It never loops for decoration, never nags and never judges. Everything below is built from
CSS transitions, CSS keyframes and the Web Animations API; no animation library.

## Owner decisions needed (conflicts with the accepted design)

| # | Conflict | Proposal | If the owner declines |
|---|---|---|---|
| 1 | README principle 6 and the 2026-10-31 decision say "no rings", "plain number" for the health score | A thin **arc gauge** behind the score number (component `ScoreArc`, 270 degrees, the same teal as `--ov-trend`), decorative and secondary to the number, which stays the primary readout with its components and "How it's computed" link | Drop `ScoreArc`; the count-up and glow on the plain number remain |
| 2 | "No streaks, badges or nudges" | The import-finished moment (below) is a one-off, dismissible acknowledgement of a system event, not a streak or reward for behaviour. No badges, no streak counters, ever | Replace with a static "Import complete" status line |
| 3 | "Don't invent components" | The component list below is the new, owner-approved vocabulary once accepted | n/a |

## Tokens

Added to `frontend/src/styles/tokens.css` next to the `--ov-*` set (the token-only rule in
`sources.test.ts` applies: no literal durations or easings in component CSS).

| Token | Value | Use |
|---|---|---|
| `--ov-dur-instant` | `90ms` | press feedback |
| `--ov-dur-fast` | `160ms` | hover, focus, toggles |
| `--ov-dur-base` | `240ms` | card reveal, tab and route fades |
| `--ov-dur-slow` | `480ms` | chart draw-in, ring fill |
| `--ov-dur-count` | `700ms` | number count-up (the longest motion in the app) |
| `--ov-ease-out` | `cubic-bezier(0.2, 0, 0, 1)` | entrances, count-up, draw-in |
| `--ov-ease-in-out` | `cubic-bezier(0.4, 0, 0.2, 1)` | state changes |
| `--ov-ease-in` | `cubic-bezier(0.4, 0, 1, 1)` | exits (always shorter, `--ov-dur-fast`) |
| `--ov-stagger` | `40ms` | step between siblings, capped at 8 steps (320 ms) |
| `--ov-lift-y` | `-2px` | card hover translate |
| `--ov-glow` | light: `0 0 24px color-mix(in srgb, var(--ov-brand) 22%, transparent)`; dark: `... 32%` | score glow |
| `--ov-sheen` | `linear-gradient(135deg, color-mix(in srgb, var(--ov-brand) 7%, var(--ov-surface)), var(--ov-surface) 60%)` | card surface lift |
| `--ov-motion` | `1` normally, `0` under reduced motion | JS reads it; CSS durations are zeroed too |

No spring physics, no overshoot, no durations above 700 ms, except the import moment (1.2 s total).

## Where motion applies

| # | Where | Behaviour | Tokens |
|---|---|---|---|
| 1 | **Score and big numbers** (overview score, stat strip, metric headline) | Count up from the previous value (or from 0 on first load) to the final value, once per mount or data change. Tabular figures so width never changes; the final text is in the DOM from the first paint (`aria-label` and visible text are final, the animation only changes the displayed digits) | `count`, `ease-out` |
| 2 | **Score arc** (decision 1) | Stroke fills to the value; soft glow behind it; never pulses | `slow`, `ease-out`, `glow` |
| 3 | **Stat strip with deltas** | Cards stagger in; delta chip (arrow, signed number, "vs your 90-day median") fades in after its number. Delta colour is the neutral series blue or attention orange per the README, never green/red | `base`, `stagger` |
| 4 | **Chart draw-in** (uPlot series, range bars on phone) | Line draws left to right once on first mount via a clip-path reveal; typical-range band fades in first; gaps stay gaps and hatching is static. No redraw animation on zoom, hover or data refresh | `slow`, `ease-out` |
| 5 | **Staggered card reveal on load** | Cards fade and rise 8 px, in DOM order, at most 8 steps; later cards appear without delay | `base`, `stagger` |
| 6 | **Page transitions** | Route content cross-fades (opacity only, no slide), shell and rail stay put; View Transitions API where supported, CSS fallback otherwise | `base` |
| 7 | **Hover and press** | Cards with a link lift `--ov-lift-y` and gain `--shadow`; buttons darken at `fast` and scale to 0.98 at `instant` on press; focus rings appear without animation delay | `fast`, `instant` |
| 8 | **Skeleton to content** | Skeletons use a flat `--ov-raised` block with a single slow shimmer (1.4 s, linear, only while loading); they take the exact size of the content (no layout shift); content cross-fades in | `base` |
| 9 | **Sparkline pulse** | After a sync that delivers a new reading, the last point of that sparkline gets one 600 ms ring pulse. Once per new reading, not a loop, and only while the tab is visible | `slow` |
| 10 | **Empty-state illustrations** | Static line art in `--ov-line` and `--ov-brand` (inline SVG, under 2 KB each) with one 480 ms fade-in; one for no data yet, one for insufficient data, one for importing. Illustrations show the idea (a dot on a band), never a body or an organ | `slow` |
| 11 | **Import finished** | When a history import reaches complete while the user is on the app: the status line turns to "History imported", the overview cards re-reveal (item 5) with the new data and the score counts up. One time per import, dismissible, no confetti, no sound, no badge | `slow`, `stagger` |
| 12 | **Numbered steps** (onboarding, connect flow) | Step number circle fills as a step completes (fast); the connecting line draws between steps (base) | `fast`, `base` |

## Visual lift (static, no motion needed)

- Cards: `--ov-sheen` surface and a 1 px `--ov-line-soft` border; `--shadow` only on hover and
  on dialogs.
- Score readout: very large figure (readiness-style, 56 px on desktop, 48 px on phone) with the
  component list beneath; the arc and glow from item 2.
- Live-looking dashboard mock-up on the landing page: a static, clearly synthetic overview
  rendered from fake data and labelled "Example data", animated with items 1, 4, 5 on scroll
  into view, once.
- The mock-ups for these (overview with arc, stat strip, import-finished, three empty states)
  are added under `mockups/` by #424; this file is their specification until then.

## Rules

1. **Reduced motion.** Under `prefers-reduced-motion: reduce` every animation and transition in
   this file is removed: durations compute to `0.01ms`, count-ups show the final value at once,
   draw-ins show the finished chart, shimmer becomes a static block, the pulse and import moment
   become static states (the status line text still changes). The rule is one global block in
   `tokens.css` plus a `useMotion()` hook that every JS animation must call; a test mounts every
   animated component with the media query on and asserts the final DOM and no running
   animations. Also honoured: a Settings "Reduce motion" override that sets `data-motion="off"`
   (follows the existing Light/Dark/System pattern).
2. **No layout shift.** Animate only `opacity` and `transform` (plus `stroke-dashoffset` and
   `clip-path`). Never animate width, height, margin or font size. Reserve space for every
   skeleton, illustration and chart. CLS stays 0 for the animated routes (Playwright check).
3. **60 fps budget.** No animation runs on the main thread longer than 700 ms; at most one JS
   animation loop at a time (count-ups share one `requestAnimationFrame`); everything else is
   compositor-only CSS. `will-change` only during an animation. Nothing animates off-screen
   (IntersectionObserver) or in a hidden tab.
4. **No medical implication.** Motion never encodes health meaning. Numbers count up neutrally
   whatever their value; no animation direction, speed, colour flash, bounce, pulse or confetti
   depends on whether a value is "good" or "bad", high or low, or in or out of range. Alerts and
   findings (including AFib results) are not animated at all, they appear instantly and
   statically. The score arc fill is the same animation for every value. The sparkline pulse
   marks "new data arrived", not "value changed". No heartbeat or lung metaphors. Copy stays
   within the PLAN §6 wording (no "improving", "great", "keep it up").
5. **Estimates stay still.** Experimental-tier (estimated) values do not count up or glow; they
   appear as static italic text, so they never look certified.
6. **Light, dark, phone.** All colours come from `--ov-*` tokens, so glow and sheen are defined
   per theme (the `--ov-glow` row). On phone width: no hover effects (guarded with
   `@media (hover: hover)`), stagger capped at 5 steps, the dashboard mock-up shows the range
   bars, touch targets stay 44 px, and count-up uses `--ov-dur-base`.
7. **Bundle budget.** M10 adds at most **12 KB gzip** of JavaScript in total and no new
   runtime dependency (CSS, WAAPI and hand-written hooks only; no framer-motion, GSAP or
   Lottie). Illustrations are inline SVG, at most 2 KB each, 8 KB in all. Enforced by a size
   check in CI that fails above the budget (added by #424, ceiling set from the pre-M10
   baseline plus 12 KB).
8. **CSP.** No inline styles or scripts. Animated values are set through CSS custom
   properties on classes or the WAAPI/`element.style` property API, as the existing source scan
   allows; confirm against `sources.test.ts` in #424.
9. **Accessibility.** Animated numbers are not re-announced on every frame (`aria-hidden`
   digits plus a static accessible label, `aria-live` only on the status line). Nothing flashes
   more than once per second.

## Implementation split (for the Architect)

- **#424** tokens, reduced-motion block, `useMotion`, `CountUp`, `Reveal` (stagger), card
  hover/press, skeletons, size check, mock-up additions.
- **#425** `ScoreArc`, stat strip with deltas, chart draw-in, sparkline pulse, page transitions.
- **#426** empty-state illustrations, import-finished moment, numbered steps, landing dashboard
  mock-up.

## Out of scope

Sound, haptics, confetti, parallax, scroll-jacking, looping decorative animation, animated
backgrounds, gamification of any kind, and any change to the data or the analysis.
