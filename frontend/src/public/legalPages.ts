import type { Locale } from '../i18n/locale'
import { parseMarkdown, type Block } from './markdown'
import { publicPath, type LegalPage } from './routes'

// The legal drafts of docs/legal/ as page content (ADR 0016 section 6). The files are read as
// text at build time and only for a build that passes the legal gate: with the gate closed no page
// asks for them. Pure apart from that read.

const SOURCES = import.meta.glob<string>('../../../docs/legal/*.{en,sv}.md', { query: '?raw', import: 'default', eager: true })

/** One line per page and language: the search-result description. The title is the draft's own heading. */
const LEGAL_DESCRIPTION: Record<LegalPage, Record<Locale, string>> = {
  privacy: {
    en: 'Draft privacy policy for ownvitals: what personal data is processed, why, for how long, and your rights.',
    sv: 'Utkast till integritetspolicy för ownvitals: vilka personuppgifter som behandlas, varför, hur länge och dina rättigheter.',
  },
  terms: {
    en: 'Draft terms of use for ownvitals.',
    sv: 'Utkast till användarvillkor för ownvitals.',
  },
  security: {
    en: 'Draft description of how ownvitals protects sign-in, tokens and your data.',
    sv: 'Utkast som beskriver hur ownvitals skyddar inloggning, token och dina data.',
  },
  cookies: {
    en: 'Draft cookie statement for ownvitals: the public pages set no cookies.',
    sv: 'Utkast till cookie-information för ownvitals: de publika sidorna sätter inga cookies.',
  },
}

/** The parsed draft of `page` in `locale`; a missing file fails the build instead of leaving a page empty. */
export function legalBlocks(page: LegalPage, locale: Locale): Block[] {
  const text = SOURCES[`../../../docs/legal/${page}.${locale}.md`]
  if (!text) throw new Error(`docs/legal/${page}.${locale}.md is missing from the build`)
  return parseMarkdown(text)
}

/** The heading of the draft, used as the page title and as the footer link text. */
export function legalTitle(page: LegalPage, locale: Locale): string {
  const first = legalBlocks(page, locale)[0]
  const text = first?.kind === 'heading' ? first.children.map((node) => ('text' in node ? node.text : '')).join('') : ''
  return text || page
}

/** The public address a draft's relative `*.en.md` / `*.sv.md` link stands for, or null for any other target. */
export function legalLinkTarget(target: string): string | null {
  const found = /^(privacy|terms|security|cookies)\.(en|sv)\.md$/.exec(target)
  return found ? publicPath(found[1] as LegalPage, found[2] as Locale) : null
}

export const legalDescription = (page: LegalPage, locale: Locale): string => LEGAL_DESCRIPTION[page][locale]
