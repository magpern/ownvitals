import { DEFAULT_LOCALE, LOCALES, type Locale } from '../i18n/locale'

// The public routes of this issue (ADR 0016 section 2) and their addresses. Pure. The English
// page is at the root, Swedish under /sv; no trailing slash except on "/" itself, so one address
// is one page. The other public routes (security, privacy, ...) join with their own issues.

export type PublicPage = 'home' | 'features'

export const PUBLIC_PAGES: readonly PublicPage[] = ['home', 'features']

const SEGMENT: Record<PublicPage, string> = { home: '', features: '/features' }

/** The address path of `page` in `locale`: `/`, `/features`, `/sv`, `/sv/features`. */
export function publicPath(page: PublicPage, locale: Locale): string {
  const prefix = locale === DEFAULT_LOCALE ? '' : `/${locale}`
  return `${prefix}${SEGMENT[page]}` || '/'
}

/** Every public address of these pages, for the build's file list. */
export function allPublicPaths(): string[] {
  return LOCALES.flatMap((locale) => PUBLIC_PAGES.map((page) => publicPath(page, locale)))
}

/** The `hreflang` alternates of a page: each language, then x-default (English). */
export function alternates(page: PublicPage): { hreflang: string; path: string }[] {
  return [
    ...LOCALES.map((locale) => ({ hreflang: locale, path: publicPath(page, locale) })),
    { hreflang: 'x-default', path: publicPath(page, DEFAULT_LOCALE) },
  ]
}

/** The app routes the pages link to as ordinary links. */
export const APP_LINKS = { signIn: '/signin', signUp: '/signup' } as const
