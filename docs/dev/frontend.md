# Frontend

`frontend/` is a React 19 + Vite + TypeScript (strict) single-page app, ported from classbeat's
frontend at `d80605100992f85f8f02672b9b5f885ed6166214` (PLAN §2, §3). This is the skeleton:
the API client, the session, the route guards and the auth pages, and the app shell of the chosen
design (the rail, the top bar, the theme, the Plex fonts and the design primitives). The screens are
the rest of M6 (PLAN §8); until each is built it shows "Coming later".

Node 24 and npm. Every dependency is pinned to an exact version and `package-lock.json` is
committed; every direct dependency, and everything that ships in the bundle, has an accepted licence
(see "Rules the tests enforce").

## Commands

Run them from `frontend/`.

| Command | What it does |
|---|---|
| `npm ci` | Install exactly what the lockfile says |
| `npm run dev` | Vite dev server; `/api` is proxied to `API_PROXY_TARGET`, default `http://localhost:8000` |
| `npm run lint` | ESLint (type-aware, React hooks rules) |
| `npm run typecheck` | `tsc -b` |
| `npm test` | Vitest and Testing Library in jsdom; a test that reaches the network fails, `fetch` is a fake |
| `npm run build` | `tsc -b` then `vite build` into `dist/` |

These are the steps of the `frontend` CI job. Set `API_PROXY_TARGET` in the shell or in
`frontend/.env` (never committed) to point the dev server at another backend; it must be an
http(s) URL.

## Layout

```
src/api/        client.ts (CSRF, retries, ApiError), decode.ts (runtime checks), auth.ts (endpoints),
                messages.ts (plain-language texts for the API's reason codes)
src/auth/       access.ts (what a session may see), guards.tsx (route guards), SessionContext.tsx
src/router/     a small history-API router (safe return_to, mail-link tokens), appRoutes.ts (the
                screens of the app by address)
src/pages/      sign in, sign up, mail-link and reset pages, the two second-factor pages, and
                appPages.tsx (the screens of the app, or "Coming later")
src/components/ forms and the plain page frame, the QR code, the Google button;
                ui/ (the design primitives), shell/ (the app frame: rail, phone tabs, account menu)
src/theme/      the System, Light or Dark choice: theme.ts, and the useThemePreference hook
src/styles/     tokens.css (the only place with colours), fonts.css, app.css, primitives.css, shell.css
src/test/       fake API (fakeApi.ts), render helper, the network guard (networkGuard.ts, installed by
                setup.ts; setup.test.ts runs that file on scratch tests), and the scanners behind
                sources.test.ts, build.test.ts and licences.test.ts
```

## Session and guards

`GET /api/auth/session` says who is signed in. `GET /api/auth/mfa` says whether the account has an
authenticator app (`totp_enabled`). Both are read by `getSession` in `src/api/auth.ts`; a refusal
of the second with `two_factor_required` counts as "no authenticator app". If a later change
puts `totp_enabled` into the session answer, that function is the only place to change.

`accessOf(session)` in `src/auth/access.ts` turns the answer into one of four states, and every
guard decides from it:

| State | Meaning | Page it belongs on |
|---|---|---|
| `anonymous` | not signed in | `/signin` |
| `enrol` | signed in, no authenticator app | `/two-factor/setup` |
| `challenge` | has an app, this session has not passed it (`session.mfa` is false) | `/two-factor` |
| `ready` | signed in and through the second factor | `/` |

| Guard | Lets through | Sends everyone else to |
|---|---|---|
| `RequireReady` | `ready` | their page above, with `?return_to=` for the page they asked for |
| `RequireSignedOut` | `anonymous` | their page above, or `return_to` (a plain relative path only) |
| `RequireTwoFactorStep` | the matching step (`enrol` or `challenge`) | their page above |

Guards redirect with `replace`, and show nothing guarded until the session is known. The server
enforces the same rules on every request; the guards keep the screens honest. Any API call
answered `403 two_factor_required` makes the session provider read the session again, so the
guards then move the user to the right page.

To add a screen of the app, add its path to `src/router/appRoutes.ts` and its component to
`src/pages/appPages.tsx`; `src/App.tsx` wraps every screen in `RequireReady` and the app frame (see
"The app frame" below). A page outside the app (a sign-in or mail-link page) is a case of `otherPage`
in `src/App.tsx`.
Mail-link pages (`/reset`, `/signup/confirm`) are not guarded: a link is followed in whatever
state the browser is in. They read their token from the URL fragment and remove it from the
address bar.

Recovery codes are shown once, on the enrolment page. The page stays up (`hold`) until the user
says they saved them, even though the session already counts as signed in.

## Styling

The stylesheets are in `src/styles/`, imported by `main.tsx` in this order: `fonts.css`,
`tokens.css`, `app.css` (base styles, the plain frame, and the cards, forms and notices of the
sign-in pages), `primitives.css` (the design primitives) and `shell.css` (the app frame). Everything
but `tokens.css` uses `var(--ov-...)`: a colour literal anywhere else fails `sources.test.ts`. New
classes are `ov-*`; the older `.card`, `.button`, `.notice` and `.field` are the sign-in pages'.

The UI design is chosen ([docs/design/](../design/README.md), owner, 2026-10-31): every screen is
built from its mock-ups, and nothing is invented beside them. Where the mock-up's HTML has an
inline `style` or the design-canvas runtime, the app uses classes and tokens instead.

### Tokens

`src/styles/tokens.css` holds the colours of `docs/design/mockups/src/ov.css` as `--ov-*` (ground,
text, brand, series, typical range, the four tiers, attention, urgent, ok, ECG paper, the admin
band, QR, scrim and shadow), and the type, spacing and shape tokens. `ov.css`'s `--ink` is
`--ov-text` and its `--brand-ink` is `--ov-on-brand`. `src/styles/tokens.test.ts` holds the file
to `ov.css` (every colour, in both themes) and checks the 4.5:1 contrast the design promises.

### Theme

Light is the default. Dark comes from `prefers-color-scheme`, and `data-theme="light"` or
`data-theme="dark"` on `<html>` overrides the device in either direction. "System" (the default)
sets no attribute. The dark values are written twice in `tokens.css` (behind the media query for a
root without the Light override, and for `data-theme="dark"`); a test keeps the two equal.

`src/theme/theme.ts` keeps the choice per browser in `localStorage` (key `ownvitals.theme`; System
stores nothing; every access is in try/catch, so a private window still works for the page) and
applies it. `initTheme()` runs from `main.tsx` before React renders, as a module and not an inline
script (the CSP is `'self'`). It also follows a change made in another tab. For the Settings
issue: `useThemePreference()` returns `[preference, setPreference]`, and `setThemePreference`,
`getThemePreference` and `THEME_PREFERENCES` are plain functions. Whether the choice should also
follow the account is that issue's question.

A module that is deferred cannot run before the browser's first paint of an empty page, so on a
cold load a visitor whose choice differs from their device can see the page background in the
device's theme for a moment, before the app (which renders only after the theme is applied) appears.
Closing that gap needs a classic script in `<head>`, which `sources.test.ts` forbids today.

### Fonts

IBM Plex Sans (UI) and Plex Mono (ids, versions, raw values), OFL-1.1, self-hosted through
`@fontsource/ibm-plex-sans` and `@fontsource/ibm-plex-mono` (exact versions, production
dependencies). `src/styles/fonts.css` imports only the faces the mock-ups use, in the Latin subset:
Sans 400, 500, 600 and italic 400, 500 (the "est." value style), Mono 400. The build copies the
`.woff2` files (and their `.woff` fallbacks) to `assets/`, and the stylesheet loads them from this
origin; the system stack is the fallback. Never Google Fonts or another host. `build.test.ts`
checks the files and the `@font-face` URLs, and allows font files in `assets/` (and nothing else
beyond scripts and styles); `fonts.test.ts` checks the list against the mock-ups. Numbers use
tabular figures through `Value`, `Num` and the tile styles.

### Design primitives

From `docs/design/mockups/src/Main.dc.html` and `ov.css`, in `src/components/ui/`, each with a test:

| Component | Is | Notes |
|---|---|---|
| `DataState` | The ten data states: up to date, stale, importing n %, incomplete import, insufficient data, not supported, not shared, not available, needs reconnect, Withings+ needed | One wording and tone per state (`DATA_STATE_KINDS`, `dataStateLabel`); `progress` for importing (required) and insufficient data draws a native `<progress>` (no inline style); `children` is the line of detail. `StatePill` is the bare pill |
| `TierMark`, `SourceMark`, `TierGlyph` | The tier badges (Device, Trend, Experimental, System), and the "Reported by Withings / Calculated by ownvitals / Estimated by ownvitals" source line | The glyphs take the text colour |
| `Value`, `Num`, `Mono` | A number with its unit (`size="large"`, `estimated` for the italic, dotted "est." style), tabular figures, ids and raw values in mono | |
| `Card`, `Stack`, `ComingLater` | The design's card (with `title`, `level`, `aside`, `flat`, `experimental`), the column, and the "Coming later" row | The sign-in pages' narrow card is `Card` in `components/forms.tsx`; #10 and #11 move those pages over |
| `Button` | `variant` default, primary, danger, danger-solid, ghost; `wide`; `lock` | `lock` shows the lock icon and sets `aria-haspopup="dialog"`: the action asks for a fresh authenticator code. The shared "Confirm it's you" dialog is #11's; until it exists the page passes its own `onClick` |
| `Icon`, `Logo` | The icons of the rail and the controls, the logo | Decorative (`aria-hidden`) |

### The app frame

A signed-in user who has passed the second factor sees an app screen (`src/router/appRoutes.ts`) in
`components/shell/AppFrame.tsx`: the rail on the left (`Rail.tsx`, the `Rail` mock-up), and on the
right a top bar (the page header as the h1, and the account menu in `AccountMenu.tsx`) above the
page. Under 760 px the rail gives way to four tabs at the bottom (`PhoneTabs`: Overview, Findings,
Data, Settings, as on the Onboarding board), the top bar carries the logo, the account button shows
the avatar only (the address is the button's name and the menu's header), and the targets are at
least 44 px. Everything else sits in the plain frame (`components/PlainFrame.tsx`): the sign-in,
sign-up and mail-link pages, the two second-factor pages and "Page not found". The frame is chosen
from the address, not the session, so a page does not move when the session arrives.

- **Items.** `components/shell/nav.ts` has the groups and items of the mock-up. Admin is shown only
  to an admin (`isAdmin`: the session's role), in the rail and in the account menu, and `/admin`
  is "Page not found" for anyone else. The phone has no Admin tab; it is in the account menu.
- **Status at the end of an item** (`3 of 4`, `n/a`, a dot, a count) is data. `Rail` takes it as
  `status`, and shows nothing until a screen that owns the data passes it.
- **Routes** follow the information architecture: `/`, `/body`, `/heart`, `/nerve`, `/sleep`,
  `/activity`, `/m/:metric`, `/m/:metric/:day`, `/heart/ecg`, `/heart/ecg/:id`, `/findings`,
  `/connections`, `/settings`, `/settings/security` (the Security rail item and the "Sign-in and
  security" tab of Settings), `/admin`. A screen that is not built shows its page header and the
  "Coming later" row. To build one, put its component in `BUILT` in `src/pages/appPages.tsx`.
- The document title follows the page (`Weight · ownvitals`).

## Rules the tests enforce

### What the repository ships

`src/sources.test.ts` scans **every text file** the browser can get: `index.html`, everything under
`public/`, and everything under `src/` except tests (`*.test.ts[x]`) and `src/test/`. The
extension does not matter: `.svg`, `.json`, `.html`, `.txt`, a web manifest and a stylesheet are
read alike. A binary file is not scanned and is listed; the test fails for any binary file that is
not an image or a font, so a file cannot hide from the scan by being unreadable. A file in
`src/assets/` or `public/` gets the same three checks as a stylesheet:

- **No third-party host.** No shipped file contains a URL to another host. The pattern reads the
  URL's authority, so it catches `https://localhost.evil.example`, `http://localhost@evil.example`,
  `https://127.0.0.1.nip.io`, IPv4 and IPv6 literals, hosts with no dot, `ws:`/`wss:`/`ftp:`,
  protocol-relative `//host`, the backslash forms a browser reads as slashes (`/\host`, `\/host`,
  `\\host`, `https:/\host`) and JSON-escaped `https:\/\/host`. Only `localhost`, `127.0.0.1` and
  `[::1]`, spelled exactly like that, and the XML namespaces of SVG, XLink and XHTML are let
  through. A host without a scheme needs a dot (or is an IPv6 literal), so `// comment`, `//TODO`
  and `'\\n'` are not hits. Known limit: percent- or entity-encoded slashes are not decoded
  (a browser does not read `%2F` in an authority as a separator).
  - **Same-origin template URLs.** An authority that is exactly one `${location.host}`,
    `${location.hostname}`, `${window.location.host}` or `${window.location.hostname}` expression
    is our own origin, whatever it is, and is not a hit: ``` `wss://${location.host}/api/ws` ``` and
    ``` `${location.protocol}//${location.host}/x` ``` pass. Nothing around the expression is
    exempt: `${location.host}.evil.example`, `${location.host}@evil.example`,
    `${location.host}:8000` (another port is another origin), `${location.origin}`,
    `${other.location.host}` and a literal host anywhere else in the file are still hits. The
    scan reads no spaces inside the braces, so write `${location.host}`, not `${ location.host }`;
    for anything more elaborate build the URL with `new URL('/api/ws', location.href)`, which has
    no host in its text at all.
  - **`e.g.` and `i.e.`** written right after the slashes of a comment (`//e.g. foo`) are not
    hosts either (no top-level domain is one letter). They are let through only with no scheme
    and only as a whole word: `https://e.g`, `//e.g/x` and `//e.g.example.com` are hits.
- **No colour literal outside `tokens.css`**: no hex, no `rgb()`/`hsl()`/`oklch()`/`color-mix()`,
  no named colour in a stylesheet, in an SVG (attribute, `<style>` or `style=`) or in a colour
  attribute. Known limit: a named colour in JSON or a manifest is not caught (hex and colour
  functions are).
- **Works under the server's CSP** (`default-src 'self'`, no inline script or style, no `data:`):
  no inline script or style, no `style` attribute, no `innerHTML`/`eval`, no `data:` URL, in the
  sources and in the production build (`src/build.test.ts` builds into a scratch folder and checks
  `index.html`, the scripts and the stylesheet; the Plex font files are the one other kind of file
  it allows in `assets/`). An SVG may not hold a `<script>` or an event handler.
  `assetsInlineLimit` is 0, so no asset is inlined.

The server's QR code is drawn as inline SVG from a short allow-list of shapes and attributes
(`src/components/QrCode.tsx`); nothing from it is inserted as markup.

### Tests never touch the network

`src/test/setup.ts` installs a guard (`src/test/networkGuard.ts`) in every test file. Until a test
stubs it, **`fetch`, `XMLHttpRequest`, `WebSocket`, `EventSource` and `navigator.sendBeacon`
refuse**: the first four throw or reject, `sendBeacon` returns `false`. jsdom has no `EventSource`
and no `sendBeacon`; the guard adds them, so code that uses them cannot slip by.

- **How it fails.** Every attempt is recorded, and `setup.ts` fails the test in its `afterEach`
  with the list (`METHOD url`, without credentials, query or fragment: they may hold a secret). It
  does so even when the code under test caught the refusal and carried on, which is how an app's
  "network error" handler would hide a missing stub.
- **How a test stubs.** For the API, `fakeApi({ 'GET /api/auth/session': ... })` from
  `src/test/fakeApi.ts`. Otherwise
  `vi.stubGlobal('fetch', ...)`, `vi.stubGlobal('EventSource', ...)` or
  `vi.spyOn(navigator, 'sendBeacon').mockReturnValue(true)`. The stub replaces the guard for that
  test only; `unstubGlobals` and `restoreMocks` in `vite.config.ts` put the guard back before the next.
- **What it does not guard.** What a page loads by itself (`img`, `script`, `link`): jsdom loads
  none of it, and the CSP stops it in the browser (the scan above and `build.test.ts` check the
  sources and the build).
- **The tests of the guard.** `src/test/networkGuard.test.ts` tests the guard itself: what it
  refuses, what it records, what `drain()` returns. It cannot tell if the `afterEach` in
  `setup.ts` stops failing the test, because each test there drains the list itself.
  `src/test/setup.test.ts` covers that hook. It starts Vitest as a child process, in a scratch
  folder with a small configuration of its own and the real `setup.ts`, on test files that swallow
  a refused `fetch` and `EventSource` and ignore the answer of `sendBeacon`, and on a test that
  stubs `fetch`. The run must fail exactly the tests that made an attempt, with the attempt in
  the message and no query string, and pass the others. Removing the hook, not calling `drain()`,
  not throwing or not installing the guard all fail it. The scratch files use relative URLs only
  and import nothing (`globals: true`), so nothing leaves the machine even with the guard gone.
  The nested run adds a few seconds.

### Licences

`src/licences.test.ts` reads `package-lock.json`. **Every package in the production dependency
tree, direct or transitive, has an accepted licence**: `MIT`, `ISC`, `Apache-2.0` or `OFL-1.1`, or
an SPDX expression that offers one (`MIT OR GPL-3.0` passes; `MIT AND GPL-3.0` does not).
`OFL-1.1-RFN` and `OFL-1.1-no-RFN` are not on the list: adding them would be a deliberate later
change. `WITH` exceptions, `+` suffixes, `UNLICENSED`, `SEE LICENSE IN ...` and a missing licence
are refused.

- **The production tree** is worked out from `package.json` through each package's dependencies,
  optional dependencies and peer dependencies, and a test requires it to equal the packages npm
  does not flag `dev`. A package counts as shipped if either says so, so a hand-set `dev: true`
  cannot hide one.
  - **Optional peers are followed too.** A peer a shipped package marks optional in
    `peerDependenciesMeta` is checked like any other, as long as it is installed, whatever its
    flags. The bundler decides what ships, not the lockfile: Vite follows the imports of a shipped
    package into `node_modules`, so an optional peer that a dev tool made npm install ends up in
    the bundle as soon as a shipped package imports it. npm marks such a package `devOptional`
    (never `dev`), which counts as not dev-only, so the cross-check above agrees. A peer that is
    not installed is ignored. A package flagged `dev` by hand that a shipped package names as a
    peer is reported, and the cross-check fails on it.
- **One list of accepted ids.** `ACCEPTED_LICENCE_IDS` in `src/test/licenceScan.ts` is the only
  list (`MIT`, `ISC`, `Apache-2.0`, `OFL-1.1`). `licenceAccepted` (the lockfile check) and
  `packageJson.test.ts` (the direct dependencies) both read it. A test pins its value, so a change
  is one deliberate edit, and another reads the source of `packageJson.test.ts` to check that it
  imports the list whole and names no licence of its own.
- **Dev-only tooling is exempt** (Vite, ESLint, Vitest, the CSS tooling ...): it is not
  distributed. Some of it is under other permissive or weak-copyleft licences (BSD, MPL-2.0
  unmodified, MIT-0, CC0, BlueOak-1.0.0, CC-BY-4.0 data); the decisions log in `docs/status.md`
  records that this is accepted. `src/packageJson.test.ts` still checks that every direct
  dependency, dev ones included, has an accepted licence.
- A new production dependency under any other licence fails the test; the fix is to choose
  another package, or a recorded decision (an ADR) that changes the list.

## Not here yet

Account settings, step-up and re-authentication dialogs (the shared "Confirm it's you" dialog is
#11's), "log out everywhere" and every screen of the app come in later issues (#10, #11, #12, M6).
The email-change page and the admin pages are not ported.

The shell also leaves out what needs data or a call that does not exist yet: the status line under
the top bar (freshness, imports, broken connections), the range selector of the Overview and Metric
top bars, the status at the end of a rail item (`Rail`'s `status`), and the account menu's "This
device since ..." line. The theme choice has no control until the Settings issue.
