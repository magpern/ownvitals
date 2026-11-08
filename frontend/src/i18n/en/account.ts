// The account's own settings: for now its language (ADR 0015 section 2). sv/account.ts satisfies
// this file's shape.
export const account = {
  'language.label': 'Language',
  'language.help': 'The language of these pages on every device you sign in on.',
  'language.followBrowser': "This browser's language",
  'language.saved': 'Language saved.',
} satisfies Record<string, string>
