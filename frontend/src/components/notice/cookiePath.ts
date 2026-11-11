/** The cookie statement is a prerendered public page: `/cookies`, `/sv/cookies` (public/routes.ts). */
export function cookiePagePath(locale: string): string {
  return locale === 'en' ? '/cookies' : `/${locale}/cookies`
}
