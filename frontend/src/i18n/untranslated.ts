// Keys whose Swedish is still the English text. An entry is an area ("auth") or a key
// ("auth.signIn.forgot"). A screen PR that adds English keys lists them here; the translation
// PR empties the list (G6 did). catalogue.test.ts fails when a listed key is already translated,
// and when an unlisted key is not.
export const UNTRANSLATED: readonly string[] = []

// Keys whose Swedish is the same word as the English one. Each entry is a key with its reason;
// catalogue.test.ts fails when a listed key differs and when an unlisted key is equal.
export const SAME_IN_SV: Readonly<Record<string, string>> = {
  'health.tier.trend': 'the same word in Swedish: trend',
  'ui.tier.trend': 'the same word in Swedish: trend',
  'ui.tier.system': 'the same word in Swedish: system',
  'shell.nav.data': 'the same word in Swedish: data',
}
