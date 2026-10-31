# Frontend

`frontend/` is a React 19 + Vite + TypeScript (strict) single-page app, ported from classbeat's
frontend at `d80605100992f85f8f02672b9b5f885ed6166214` (PLAN §2, §3). This is the skeleton:
the API client, the session, the route guards and the auth pages. The product UI is M6 (PLAN §8).

Node 24 and npm. Every dependency is pinned to an exact version and `package-lock.json` is
committed; direct dependencies are MIT or Apache-2.0 (`src/packageJson.test.ts` checks both).

## Commands

Run them from `frontend/`.

| Command | What it does |
|---|---|
| `npm ci` | Install exactly what the lockfile says |
| `npm run dev` | Vite dev server; `/api` is proxied to `API_PROXY_TARGET`, default `http://localhost:8000` |
| `npm run lint` | ESLint (type-aware, React hooks rules) |
| `npm run typecheck` | `tsc -b` |
| `npm test` | Vitest and Testing Library in jsdom; no network, `fetch` is a fake |
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
src/test/       fake API and helpers for tests
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

## Rules the tests enforce

- **No third-party host.** No file shipped from `frontend/` (`index.html`, `public/`, `src/`)
  contains a URL to another host (`src/sources.test.ts`).
- **No colour literal outside `tokens.css`**: no hex, no `rgb()`/`hsl()`/`oklch()`/`color-mix()`,
  no named colour in a stylesheet or a colour attribute.
- **Works under the server's CSP** (`default-src 'self'`, no inline script or style, no `data:`):
  no inline script or style, no `style` attribute, no `innerHTML`/`eval`, no `data:` URL, in the
  sources and in the production build (`src/build.test.ts` builds into a scratch folder and checks
  `index.html`, the scripts and the stylesheet). `assetsInlineLimit` is 0, so no asset is inlined.
- The server's QR code is drawn as inline SVG from a short allow-list of shapes and attributes
  (`src/components/QrCode.tsx`); nothing from it is inserted as markup.

## Not here yet

Account settings, step-up and re-authentication dialogs, "log out everywhere" and the dashboard
pages come in later issues (#11, M6). The email-change page and the admin pages are not ported.
