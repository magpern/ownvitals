import type { common as en } from '../en/common'

// Swedish for the common area (glossary: docs/dev/i18n.md). `satisfies` makes a missing or extra key a type error.
export const common = {
  loading: 'Läser in',
  loadingDots: 'Läser in…',
  tryAgain: 'Försök igen',
  cancel: 'Avbryt',
  confirm: 'Bekräfta',
  email: 'E-post',
  password: 'Lösenord',
  signIn: 'Logga in',
  signUp: 'Skapa konto',
  signOut: 'Logga ut',
  account: 'Konto',
  language: 'Språk',
  now: 'Nu',
} satisfies Record<keyof typeof en, string>
