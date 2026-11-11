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
public route to its own file (`dist/features/index.html`, `dist/sv/features/index.html`, ...) from
the same typed i18n catalogue (ADR 0015) and the same design tokens. The existing static handler
serves them unchanged, with one addition: a path with no file tries `<path>/index.html` before the
SPA fallback. The public files are plain HTML and CSS, with **no JavaScript at all** and no link to
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
`robots.txt` allows the public routes and disallows `/api/`, the sign-in, sign-up, reset and app
routes. Because `robots.txt` is advisory, the web process also adds `X-Robots-Tag: noindex, nofollow`
to every response that is not on an explicit public-path allowlist (including the SPA shell and the
API). The allowlist is code, with a test that a new route is `noindex` until added.

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
  Cache headers on public files: `Cache-Control: public, max-age=300`; the app routes keep theirs.
- No analytics, tag manager, fonts CDN, map or video embed, and no tracking pixel. The egress guards
  need no change; a test that the built HTML has no absolute external URL other than `<a href>`
  links and the canonical/`hreflang` origin enforces it.

**6. Legal text is a gate, not a deliverable of agents.** Agents draft `/privacy`, `/terms` and
`/cookies` as DRAFT text (controller identity, purposes, health data as special-category data under
GDPR art. 9 and its explicit consent, retention and deletion, processors including Anthropic only
when AI is enabled (ADR 0014), data-subject rights, supervisory authority, the statement that no
cookies are used on public pages). Until the owner passes the legal gate, the three pages are built
only when the flag `OWNVITALS_PUBLIC_LEGAL_APPROVED` is true at build time; otherwise they are
omitted from the build, the sitemap and the footer, and `/` and `/features` say no medical claims.
Agents never set the flag, never mark the gate passed, and the pages never claim medical
advice, diagnosis or Withings endorsement (health-wording rule of ADR 0015).

**7. Split into issues** (the Architect writes them): M9a build step, static serving and headers;
M9b home, features, security and contact in en and sv; M9c SEO files and JSON-LD; M9d legal drafts
and the flag; M9e tests (no cookie, no external URL, noindex allowlist, image list, sitemap/robots).

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
