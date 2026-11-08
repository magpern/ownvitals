import type { common as en } from '../en/common'

// Swedish for the common area. Until the translation PR (G6) the values are English copies and the
// keys are listed in ../untranslated.ts. `satisfies` makes a missing or extra key a type error.
export const common = {
  loading: 'Loading',
  loadingDots: 'Loading…',
  tryAgain: 'Try again',
  cancel: 'Cancel',
  confirm: 'Confirm',
  email: 'Email',
  password: 'Password',
  signIn: 'Sign in',
  signUp: 'Sign up',
  signOut: 'Sign out',
  account: 'Account',
  language: 'Language',
  now: 'Now',
} satisfies Record<keyof typeof en, string>
