import type { account as en } from '../en/account'

// Swedish for the account area. Until the translation PR (G6) the values are English copies and the
// keys are listed in ../untranslated.ts. `satisfies` makes a missing or extra key a type error.
export const account = {
  'language.label': 'Language',
  'language.help': 'The language of these pages on every device you sign in on.',
  'language.followBrowser': "This browser's language",
  'language.saved': 'Language saved.',
} satisfies Record<keyof typeof en, string>
