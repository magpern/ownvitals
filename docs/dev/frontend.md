# Frontend

`frontend/` is a React 19 + Vite + TypeScript (strict) single-page app, ported from classbeat's
frontend at `d80605100992f85f8f02672b9b5f885ed6166214` (PLAN §2, §3). This is the skeleton:
the API client, the session, the route guards and the auth pages. The product UI is M6 (PLAN §8).

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
src/router/     a small history-API router (flat routes, safe return_to, mail-link tokens)
src/pages/      sign in, sign up, mail-link and reset pages, the two second-factor pages, home
src/components/ forms, the page frame, the QR code, the Google button
src/styles/     tokens.css (the only place with colours), app.css
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

To add a guarded page, wrap it in `RequireReady` and add its path to `Routes` in `src/App.tsx`.
Mail-link pages (`/reset`, `/signup/confirm`) are not guarded: a link is followed in whatever
state the browser is in. They read their token from the URL fragment and remove it from the
address bar.

Recovery codes are shown once, on the enrolment page. The page stays up (`hold`) until the user
says they saved them, even though the session already counts as signed in.

## Styling

`src/styles/tokens.css` holds every colour, spacing, type and shape token (`--ov-*`), light by
default and dark with `prefers-color-scheme`. Everything else uses `var(--ov-...)`. System fonts
only.

The UI design is chosen ([docs/design/](../design/README.md), owner, 2026-10-31): every screen
is built from its mock-ups, and nothing is invented beside them. The first M6 issue brings its
tokens into `tokens.css`, the theme setting and self-hosted IBM Plex.

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
  `index.html`, the scripts and the stylesheet). An SVG may not hold a `<script>` or an event
  handler. `assetsInlineLimit` is 0, so no asset is inlined.

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

Account settings, step-up and re-authentication dialogs, "log out everywhere" and the dashboard
pages come in later issues (#11, M6). The email-change page and the admin pages are not ported.
