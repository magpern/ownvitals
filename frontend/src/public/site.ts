import { LOCALES } from '../i18n/locale'
import { imageFiles } from './images'
import { normaliseOrigin } from './meta'
import { renderPublicPage } from './renderPublic'
import { PUBLIC_PAGES, alternates, publicPath, type PublicPage } from './routes'

// The files of the prerendered public site (ADR 0016 sections 1 and 3), as data: path under
// `dist-public/` and text. Pure: the origin and the legal flag are passed in. The script
// scripts/build-public.mjs writes them, adds `public.css` and copies the images.

/** The legal pages (privacy, terms, cookies), built only once the owner has passed the legal gate. */
export const LEGAL_PAGES: readonly PublicPage[] = []

export const STYLESHEET_URL = '/public.css'

/** Fail closed: the legal pages are built only for exactly `true` (ADR 0016 section 6). */
export function legalApproved(value: string | undefined): boolean {
  return value === 'true'
}

export function builtPages(legal: boolean): PublicPage[] {
  return [...PUBLIC_PAGES, ...(legal ? LEGAL_PAGES : [])]
}

/** The file of an address path: `/` is `index.html`, `/sv/features` is `sv/features/index.html`. */
export function fileOf(path: string): string {
  return path === '/' ? 'index.html' : `${path.slice(1)}/index.html`
}

function sitemap(base: string, pages: readonly PublicPage[]): string {
  const entries = pages.flatMap((page) =>
    LOCALES.map((locale) => {
      const links = alternates(page)
        .map((alt) => `<xhtml:link rel="alternate" hreflang="${alt.hreflang}" href="${base}${alt.path}"/>`)
        .join('')
      return `<url><loc>${base}${publicPath(page, locale)}</loc>${links}</url>`
    }),
  )
  return (
    '<?xml version="1.0" encoding="UTF-8"?>\n' +
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:xhtml="http://www.w3.org/1999/xhtml">\n' +
    `${entries.join('\n')}\n</urlset>\n`
  )
}

// Only /api/ is disallowed: a crawler that cannot fetch a URL never sees its X-Robots-Tag, so the
// sign-in routes stay crawlable (and noindex) by design (ADR 0016 section 3).
function robots(base: string): string {
  return `User-agent: *\nDisallow: /api/\n\nSitemap: ${base}/sitemap.xml\n`
}

export interface SiteFile {
  path: string
  content: string
}

export function siteFiles(origin: string, legal: boolean): SiteFile[] {
  const base = normaliseOrigin(origin)
  const pages = builtPages(legal)
  const html = pages.flatMap((page) =>
    LOCALES.map((locale) => ({
      path: fileOf(publicPath(page, locale)),
      content: renderPublicPage({ page, locale, origin: base, stylesheet: STYLESHEET_URL }),
    })),
  )
  return [...html, { path: 'sitemap.xml', content: sitemap(base, pages) }, { path: 'robots.txt', content: robots(base) }]
}

/** Every file the build emits, by path from the public root: the contract with the server. */
export function builtPaths(origin: string, legal: boolean): string[] {
  return [
    ...siteFiles(origin, legal).map((file) => file.path),
    'public.css',
    ...imageFiles().map((file) => `img/${file}`),
  ]
}
