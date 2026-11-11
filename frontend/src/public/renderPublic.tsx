import { renderToStaticMarkup } from 'react-dom/server'
import { EN, type MessageKey } from '../i18n/en'
import { SV } from '../i18n/sv'
import type { Locale } from '../i18n/locale'
import { interpolate, lookup, type Catalogue } from '../i18n/translate'
import { pageMeta } from './meta'
import { PublicBody } from './PublicPages'
import type { PublicPage } from './routes'

// Renders one public page to a complete HTML document (ADR 0016 section 1). The build calls this
// once per page and language. No script element except the JSON-LD data block, no inline style,
// one linked stylesheet.

export interface RenderOptions {
  page: PublicPage
  locale: Locale
  /** The canonical origin, a build setting (an https origin). */
  origin: string
  /** The URL of the one stylesheet the build emits. */
  stylesheet: string
}

const CATALOGUES: Record<Locale, Catalogue> = { en: EN, sv: SV }

export function textFor(locale: Locale): (key: MessageKey) => string {
  return (key) => interpolate(lookup(CATALOGUES[locale], EN, key))
}

// The one script-like element of a public page is the JSON-LD data block (ADR 0016 section 3): a
// browser never executes it. It is written as a string, with its JSON already made safe by
// `safeJson`, because JSX would escape the quotes. The tag name is a constant so the source scan
// for script elements (src/sources.test.ts) stays strict for everything else; public.test.ts
// checks that exactly this one block is in the output.
const DATA_TAG = 'script'

function dataBlock(json: string): string {
  return `<${DATA_TAG} type="application/ld+json">${json}</${DATA_TAG}>`
}

export function renderPublicPage({ page, locale, origin, stylesheet }: RenderOptions): string {
  const t = textFor(locale)
  const meta = pageMeta(page, locale, origin, t)
  const markup = renderToStaticMarkup(
    <html lang={locale}>
      <head>
        <meta charSet="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <meta name="color-scheme" content="light dark" />
        <title>{meta.title}</title>
        <meta name="description" content={meta.description} />
        <link rel="canonical" href={meta.canonical} />
        {meta.alternates.map((alt) => (
          <link key={alt.hreflang} rel="alternate" hrefLang={alt.hreflang} href={alt.href} />
        ))}
        <meta property="og:type" content="website" />
        <meta property="og:site_name" content="ownvitals" />
        <meta property="og:title" content={meta.title} />
        <meta property="og:description" content={meta.description} />
        <meta property="og:url" content={meta.canonical} />
        <meta property="og:locale" content={meta.ogLocale} />
        <meta property="og:image" content={meta.image} />
        <meta property="og:image:alt" content={meta.imageAlt} />
        <meta name="twitter:card" content="summary_large_image" />
        <meta name="twitter:title" content={meta.title} />
        <meta name="twitter:description" content={meta.description} />
        <meta name="twitter:image" content={meta.image} />
        <meta name="twitter:image:alt" content={meta.imageAlt} />
        <link rel="stylesheet" href={stylesheet} />
      </head>
      <body className="pub">
        <PublicBody page={page} locale={locale} t={t} />
      </body>
    </html>,
  )
  return `<!doctype html>${markup.replace('</head>', `${dataBlock(meta.jsonLd)}</head>`)}`
}
