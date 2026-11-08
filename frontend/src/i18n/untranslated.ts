// Keys whose Swedish is still the English text. An entry is an area ("auth") or a key
// ("auth.signIn.forgot"). A screen PR that adds English keys lists them here; the translation
// PR (G6) empties the list. catalogue.test.ts fails when a listed key is already translated, and
// when an unlisted key is not.
// Only words that are the same in Swedish ('Trend', 'Data', 'System'); catalogue.test.ts needs them
// listed while sv equals en. Everything else is translated (G6).
export const UNTRANSLATED: readonly string[] = [
  'health.tier.trend',
  'shell.nav.data',
  'ui.tier.system',
  'ui.tier.trend',
]
