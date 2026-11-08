// Keys whose Swedish is still the English text. An entry is an area ("auth") or a key
// ("auth.signIn.forgot"). A screen PR that adds English keys lists them here; the translation
// PR (G6) empties the list. catalogue.test.ts fails when a listed key is already translated, and
// when an unlisted key is not.
export const UNTRANSLATED: readonly string[] = ['common', 'errors', 'auth', 'account', 'security', 'shell', 'ui']
