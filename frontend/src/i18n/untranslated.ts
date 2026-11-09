// Keys whose Swedish is still the English text. An entry is an area ("auth") or a key
// ("auth.signIn.forgot"). A screen PR that adds English keys lists them here; the translation
// PR empties the list (G6 did). catalogue.test.ts fails when a listed key is already translated,
// and when an unlisted key is not.
export const UNTRANSLATED: readonly string[] = []

// Keys whose Swedish is the same word as the English one. Each entry is a key with its reason;
// catalogue.test.ts fails when a listed key differs and when an unlisted key is equal.
export const SAME_IN_SV: Readonly<Record<string, string>> = {
  'ecg.scale': 'units and numbers only, the same in Swedish',
  'ecg.gain.option': 'units and numbers only, the same in Swedish',
  'ecg.zoom': 'a number and a percent sign, the same in Swedish',
  'ecg.interval.qrs': 'a clinical abbreviation, the same in Swedish: QRS',
  'ecg.interval.pr': 'a clinical abbreviation, the same in Swedish: PR',
  'ecg.interval.qt': 'a clinical abbreviation, the same in Swedish: QT',
  'ecg.interval.qtc': 'a clinical abbreviation, the same in Swedish: QTc',
  'health.tier.trend': 'the same word in Swedish: trend',
  'connections.withings.title': 'a brand name, the same in Swedish: Withings',
  'ui.tier.trend': 'the same word in Swedish: trend',
  'ui.tier.system': 'the same word in Swedish: system',
  'shell.nav.data': 'the same word in Swedish: data',
  'detail.trend': 'the same word in Swedish: trend',
  'detail.day.status': 'the same word in Swedish: status',
  'detail.reading.firmware': 'the same word in Swedish: firmware',
  'detail.reading.rawFormula': 'a formula, the same in both languages',
  'detail.range.30d': 'the same abbreviation in Swedish: 30 d',
  'detail.range.90d': 'the same abbreviation in Swedish: 90 d',
  'overview.range.30d': 'the same abbreviation in Swedish: 30 d',
  'overview.range.90d': 'the same abbreviation in Swedish: 90 d',
  'settings.theme.system': 'the same word in Swedish: system',
  'settings.tier.trend': 'the same word in Swedish: trend',
  'settings.tier.system': 'the same word in Swedish: system',
  'settings.weight.unit.kg': 'a unit',
  'settings.weight.unit.lb': 'a unit',
  'settings.weight.unit.st': 'a unit',
  'settings.temperature.unit.c': 'a unit',
  'settings.temperature.unit.f': 'a unit',
  'settings.format.iso': 'an ISO date, the same in every language',
  'settings.range.30d': 'the same abbreviation in Swedish: 30 d',
  'settings.range.90d': 'the same abbreviation in Swedish: 90 d',
}
