import { INTL_TAG, type Locale } from '../i18n/locale'
import type { MessageKey } from '../i18n/en'
import { PUBLIC_IMAGES, imageUrl } from './images'
import { alternates, publicPath, type PublicPage } from './routes'

// What goes in the <head> of a public page (ADR 0016 section 3). Pure: text and the canonical
// origin are passed in. The origin is a build setting, never taken from a request.

export interface PageMeta {
  title: string
  description: string
  canonical: string
  alternates: { hreflang: string; href: string }[]
  ogLocale: string
  /** Absolute URL of the share image. */
  image: string
  imageAlt: string
  jsonLd: string
}

type Text = (key: MessageKey) => string

/** An https origin with no path or trailing slash, or an error: a bad origin must fail the build. */
export function normaliseOrigin(origin: string): string {
  const url = new URL(origin)
  if (url.protocol !== 'https:') {
    throw new Error(`The public origin must be https, got "${origin}"`)
  }
  if (url.pathname !== '/' || url.search !== '' || url.hash !== '' || url.username !== '') {
    throw new Error(`The public origin must be a bare origin, got "${origin}"`)
  }
  return url.origin
}

/** JSON for a `application/ld+json` block: `<` is escaped so the data can never close the tag. */
export function safeJson(value: unknown): string {
  return JSON.stringify(value).replace(/</g, '\\u003c').replace(/\u2028/g, '\\u2028').replace(/\u2029/g, '\\u2029')
}

export function pageMeta(page: PublicPage, locale: Locale, origin: string, t: Text): PageMeta {
  const base = normaliseOrigin(origin)
  const canonical = `${base}${publicPath(page, locale)}`
  const share = PUBLIC_IMAGES[0]
  const image = `${base}${imageUrl(share?.light ?? '')}`
  const description = t(page === 'home' ? 'site.meta.home.description' : 'site.meta.features.description')
  const title = t(page === 'home' ? 'site.meta.home.title' : 'site.meta.features.title')
  const jsonLd = safeJson({
    '@context': 'https://schema.org',
    '@type': 'SoftwareApplication',
    name: 'ownvitals',
    url: `${base}${publicPath('home', locale)}`,
    description: t('site.meta.app.description'),
    inLanguage: locale,
    applicationCategory: 'HealthApplication',
    operatingSystem: 'Web',
    image,
    offers: { '@type': 'Offer', price: '0', priceCurrency: 'EUR' },
  })
  return {
    title,
    description,
    canonical,
    alternates: alternates(page).map((alt) => ({ hreflang: alt.hreflang, href: `${base}${alt.path}` })),
    ogLocale: INTL_TAG[locale].replace('-', '_'),
    image,
    imageAlt: t('site.meta.image.alt'),
    jsonLd,
  }
}
