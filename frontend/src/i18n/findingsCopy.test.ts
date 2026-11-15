import { describe, expect, it } from 'vitest'
import { EN } from './en'
import { SV } from './sv'

// Trend-tier text names a change in a number, never a condition (PLAN §3, D9): no diagnostic label
// appears in the wording of a trend rule or in "What it isn't". "Not a diagnosis" is a negation.

const TREND_RULES = ['B1', 'B2', 'B3', 'B4', 'B5', 'B6', 'C1', 'C1x', 'C2', 'N2:guided', 'N2:routine', 'N3', 'N5']
const LABELS =
  /atrial|fibrill|arrhythm|hypertens|diabet|neuropath|syndrome|disease|infarct|stroke|heart failure|förmaks|arytmi|högt blodtryck|diabetes|neuropati|sjukdom|syndrom|hjärtsvikt|hjärtinfarkt/i

describe('the trend-tier wording', () => {
  for (const [lang, messages] of [['en', EN], ['sv', SV]] as const) {
    const catalogue = messages as Record<string, string>
    it(`has no diagnostic label (${lang})`, () => {
      const texts = [
        ...TREND_RULES.flatMap((id) => [`health.rule.${id}.name`, `health.rule.${id}.text`]).map((k) => catalogue[k]),
        catalogue['findings.isnt.trend'],
      ]
      expect(texts.every((text) => typeof text === 'string')).toBe(true)
      for (const text of texts) expect(text).not.toMatch(LABELS)
    })
  }
})
