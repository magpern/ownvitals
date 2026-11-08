import { describe, expect, it } from 'vitest'
import { EN_AREAS } from '../i18n/en'
import { SV_AREAS } from '../i18n/sv'
import { getLocale } from '../i18n/store'
import codes from './codes.json'
import rules from './rules.json'
import { codeMessage, codeText, jobFailureMessage, methodMessage, removalMessage } from './codes'

// ADR 0015 sections 5 and 6: every code the backend can send has an English text, an unknown code
// shows the fallback, and every rule id has its health wording. `codes.json` and `rules.json` are
// written from the backend lists (tests/test_response_codes.py keeps them equal).

const REASONS = EN_AREAS.errors as Record<string, string>
const CODES = EN_AREAS.codes as Record<string, string>
const HEALTH = EN_AREAS.health as Record<string, string>

describe('response codes', () => {
  it.each(codes)('has an English entry for %s', (code) => {
    const text = REASONS[`reason.${code}`] ?? CODES[`code.${code}`]
    expect(text, code).toBeTruthy()
    expect(codeText(code), code).toBe(text)
  })

  it('has no entry for a code the backend does not send', () => {
    for (const key of Object.keys(CODES)) expect(codes, key).toContain(key.replace('code.', ''))
  })

  it('shows the generic fallback for an unknown code, never the code', () => {
    expect(codeText('made_up_code')).toBeUndefined()
    expect(codeMessage('made_up_code')).toBe(EN_AREAS.errors.generic)
    expect(codeMessage(null)).toBe(EN_AREAS.errors.generic)
    expect(codeMessage('made_up_code')).not.toContain('made_up')
  })

  it('words the failure of a job, the removal note and the method from their codes', () => {
    expect(getLocale()).toBe('en')
    expect(jobFailureMessage(null)).toBeNull()
    expect(jobFailureMessage('upstream_unavailable')).toBe(CODES['code.upstream_unavailable'])
    expect(removalMessage(null)).toBeNull()
    expect(removalMessage('revoke_incomplete')).toBe(CODES['code.revoke_incomplete'])
    const method = methodMessage('daily_summary', { band_k: 1.5, band_window_days: 90, morning_start: '04:00', morning_end: '11:00' })
    expect(method).toContain('1.5 times')
    expect(method).toContain('last 90 days')
  })

  it('words the re-authentication code of H1', () => {
    expect(codeMessage('reauth_use_password')).toBe('Use your password or an authenticator code instead.')
  })
})

describe('health wording', () => {
  it('does not call the E2 and E3 device flags atrial fibrillation', () => {
    for (const key of ['rule.E2.name', 'rule.E2.text', 'rule.E3.name', 'rule.E3.text']) {
      expect(HEALTH[key], key).not.toMatch(/fibrillation/i)
    }
  })

  it.each(rules)('has a name and a text for the rule %s', (rule) => {
    expect(HEALTH[`rule.${rule}.name`], rule).toBeTruthy()
    expect(HEALTH[`rule.${rule}.text`], rule).toBeTruthy()
  })

  it('has no entry for a rule that does not exist', () => {
    for (const key of Object.keys(HEALTH)) {
      const match = /^rule\.(.+)\.(name|text)$/.exec(key)
      if (match !== null) expect(rules, key).toContain(match[1])
    }
  })

  it('labels every tier and severity, and has the same keys in Swedish', () => {
    for (const label of ['tier.device', 'tier.trend', 'tier.experimental', 'tier.system']) expect(HEALTH[label]).toBeTruthy()
    for (const label of ['severity.urgent', 'severity.attention', 'severity.info']) expect(HEALTH[label]).toBeTruthy()
    expect(Object.keys(SV_AREAS.health).sort()).toEqual(Object.keys(HEALTH).sort())
  })
})
