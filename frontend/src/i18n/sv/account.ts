import type { account as en } from '../en/account'

// Swedish for the account area (glossary: docs/dev/i18n.md). `satisfies` makes a missing or extra key a type error.
export const account = {
  'language.label': 'Språk',
  'language.help': 'Språket på de här sidorna på alla enheter där du loggar in.',
  'language.followBrowser': 'Webbläsarens språk',
  'language.saved': 'Språket är sparat.',
} satisfies Record<keyof typeof en, string>
