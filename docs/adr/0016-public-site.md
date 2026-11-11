# ADR 0016: Public site and search-engine visibility (milestone M9)

Status: Proposed (roadmap Amendment 11, 2026-11-11, owner-requested scope addition). The owner
questions are listed at the end, each with the default that applies until it is answered. Issue #416.

## Context

ownvitals has no public page. The web process serves the Vite single-page app: `web.py`
(`_add_single_page_app`) answers any GET outside `/api` with a file under the static directory or
`index.html`, and `headers.py` puts one strict CSP on every response (`default-src 'self'`, no inline
script or style, `img-src 'self'`, `frame-ancestors 'none'`, `base-uri 'none'`), plus
`Referrer-Policy: no-referrer` and `nosniff`. A search engine therefore sees an empty shell, and a
visitor cannot learn what the product is, who runs it or how it treats health data before signing up.
The owner asked (2026-11-11) for public pages so the app can be found. They must add no trust
boundary, no outbound call, and no cookie or analytics.

## Decision

**1. Where the pages live: prerendered static HTML, built from the existing frontend, same origin.**
A build step in `frontend/` (a Vite plugin or a small script run after `vite build`) renders each
public route into a **separate output directory**, `dist-public/` (`dist-public/index.html`,
`dist-public/features/index.html`, `dist-public/sv/features/index.html`, `sitemap.xml`,
`robots.txt`, images, one stylesheet), from the same typed i18n catalogue (ADR 0015) and the same
design tokens. `dist/` stays the SPA build, and `dist/index.html` stays the app shell and the
fallback; a prerendered file never overwrites it. The image ships both directories.

`_add_single_page_app` changes as follows, in this order, for a GET outside `/api`: (a) the
public lookup: if the **exact** request path is in the public path set (item 3), resolve
`dist-public/<path>/index.html` (`dist-public/index.html` for `/`; `sitemap.xml` and `robots.txt`
and the public assets are exact files) and serve it if it exists; (b) the existing file lookup
under `dist/`; (c) the `dist/index.html` fallback. **Containment rule:** every candidate in (a) is
resolved and must pass `is_relative_to(<dist-public root>)` and `is_file()` after resolution
(symlinks included), inside the same `try` that catches `OSError` and `ValueError` (an embedded NUL)
as `web.py:136-137` does; any failure falls through to (b), never to an error that reveals the
path. Only paths in the fixed set reach (a) at all, so an unlisted or traversal path never builds a
`dist-public` path. The public files are plain HTML and CSS, with **no JavaScript at all** and no link to
the app bundle, so they need no CSP exception and a crawler gets full content without running
scripts. No server-side rendering at request time, no second server, no CDN, no new process.

**2. Route map** (each in `en` at the root and `sv` under `/sv`): `/` (what it is), `/features`,
`/security`, `/privacy`, `/terms`, `/cookies` (states that none are set), `/contact`. Sign-in,
sign-up and everything else keep their current routes; an implementation issue audits the SPA route
map for a clash with `/` and settles it (default: the public page owns the exact path `/`; a signed-in
dashboard moves to a path under the app, with a redirect). A public page links to sign-in as an
ordinary link.

**3. SEO.** Per page: a distinct `<title>` and meta description from the catalogue, a canonical URL,
`hreflang` for `en`, `sv` and `x-default` (en), Open Graph and Twitter-card tags, and JSON-LD
`SoftwareApplication`. The JSON-LD block is `type="application/ld+json"` data, which a browser never
executes, so CSP `script-src 'self'` is unchanged and no inline script is allowed. `sitemap.xml` and
`robots.txt` are generated at build from the route list. The canonical origin is a build-time
setting (the public hostname, roadmap Amendment 9), never taken from the request `Host`.
`robots.txt` allows the public routes and disallows `/api/`. Because `robots.txt` is advisory, the
web process adds `X-Robots-Tag: noindex, nofollow` to every response that is not served from a
prerendered public file (the SPA shell, the API, errors). The public path set is a fixed tuple in
code, matched **exactly** against the request path (the trailing-slash form decided once, no prefix
or glob, so listing `/` exempts nothing else). A path is exempt only when (a) above actually served
a prerendered file: a listed path that falls through to the SPA shell, for example a legal page
omitted while the legal flag is off, still gets `noindex`. A new route is `noindex` until added. The
sign-in, sign-up and reset routes are not `Disallow`ed, because a crawler that cannot fetch a URL
never sees its `X-Robots-Tag`; they are crawlable and `noindex`.

**4. Images.** Only the owner-approved synthetic mock-up screenshots in `docs/design/mockups/png/`,
copied into the build as same-origin files, optimised, with `alt` text and explicit dimensions. A
fixture-guard test checks that every public image is on a fixed list; no real health data, no
third-party images, fonts or embeds (`img-src 'self'` stays).

**5. Security of the public routes.**
- The same CSP and headers apply (`headers.py` unchanged for them). No `style` attribute and no
  inline `<style>`: the stylesheet is a linked file.
- Public routes are GET and HEAD only, served from files, and touch no database, session, key ring
  or queue. Therefore no state-changing endpoint, no `Set-Cookie`, no CSRF token and no per-user
  response; a test asserts no `Set-Cookie` on every public path.
- `/contact` shows an address and a `mailto:` link. There is **no contact form**, so there is no new
  write endpoint, no mail relay to abuse and nothing that could reveal whether an email address is
  registered. Sign-in, sign-up and reset keep their existing anti-enumeration behaviour (ADR 0003).
- Rate limiting: in-app per-IP limiting is not reliable without trusted proxies (roadmap Amendment
  9) and static files cost nothing beyond bandwidth, so volume control is the edge (Cloudflare).
  Cache headers on public files: `Cache-Control: public, max-age=300` only on a response served from a `dist-public` file; the SPA
shell, including on a listed path that falls through to it, and the API keep theirs.
- No analytics, tag manager, fonts CDN, map or video embed, and no tracking pixel. The egress guards
  need no change; a test that the built HTML has no absolute external URL other than `<a href>`
  links enforces it; absolute URLs on the site's own canonical origin are allowed in canonical,
  `hreflang`, `og:image`/`og:url`, Twitter-card and JSON-LD `url`/`image` fields.

**6. Legal text is a gate, not a deliverable of agents.** Agents draft `/privacy`, `/terms` and
`/cookies` as DRAFT text (controller identity, purposes, health data as special-category data under
GDPR art. 9 and its explicit consent, retention and deletion, processors including Anthropic only
when AI is enabled (ADR 0014), data-subject rights, supervisory authority, the statement that no
cookies are used on public pages). Until the owner passes the legal gate, the three pages are built
only when the flag `OWNVITALS_PUBLIC_LEGAL_APPROVED` is exactly `true` at build time; any other
value or none fails closed: they are omitted from the build, the sitemap and the footer, and `/`
and `/features` make no medical claims. The flag is a build argument that only the owner supplies
(repository variable set in Settings, never a committed file); the committed default is unset. A CI
test fails if any committed file (Dockerfile, workflow, `.env.example`, compose) sets it. Agents
never set the flag, never mark the gate passed, and the pages never claim medical
advice, diagnosis or Withings endorsement (health-wording rule of ADR 0015).

**7. Split into issues** (the Architect writes them): M9a build step, static serving and headers;
M9b home, features, security and contact in en and sv; M9c SEO files and JSON-LD; M9d legal drafts
and the flag; M9e tests (no cookie, no external URL, noindex rule, image list, sitemap/robots), including: traversal inputs
(`..`, `sv/../..`, `%2e%2e`, a NUL byte, a symlink out of `dist-public`) against the new lookup;
exact-match noindex (listing `/` leaves `/api/...` and `/x` noindex); a listed legal path with the
flag off still `noindex` and without `public` cache headers; the flag fail-closed and not-committed
tests.

## Alternatives

- **Separate static site or host** (Pages, another container): a second origin, deploy path and
  review surface for little gain; cross-origin links to the app weaken the single-origin CSP story.
  Rejected.
- **Client-rendered SPA routes only:** crawlers get an empty shell; title and meta tags would need
  JavaScript and often inline script. Rejected.
- **Request-time SSR (Node in the web image):** a new runtime and process in the trust boundary.
  Rejected.
- **A contact form:** needs a write endpoint, spam and enumeration handling and outbound mail from a
  public route. Rejected for v1 in favour of a plain address.
- **A cookie banner plus analytics:** contradicts "no cookies or analytics". Rejected.

## Consequences

- A crawler and a visitor get full content with no script; the build gains a prerender step and the
  catalogue gains public strings (en and sv, both required by the existing catalogue check).
- One more rule in the web process: the `noindex` allowlist, which is code and tested.
- Public pages become part of the review surface of the legal gate; they can ship without the legal
  pages, but the site is not opened for registration beyond the household before the owner passes it
  (AGENTS.md safety envelope).
- Nothing here is verified against a search engine; indexing depends on the live domain and DNS,
  which are the owner's.

## Owner questions (default until answered)

1. Public hostname and canonical origin: the production hostname from Amendment 9.
2. Controller identity and contact address for the legal pages: omitted (pages not built).
3. Whether `/` stays public when signed in: yes, with a "Go to the app" link.
4. Languages: `en` and `sv` only.
