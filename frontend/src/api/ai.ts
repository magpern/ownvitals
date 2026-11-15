import { del, get, post } from './client'
import { DecodeError, count, flag, object, optionalCount, optionalText, text } from './decode'

// /api/ai/consent (issue 206, ADR 0014 section 3): whether AI insights are available, the user's
// consent state, and the consent text with its version and fingerprint. Giving consent names that
// exact text and needs a step-up; withdrawing needs neither and deletes the conversation history.

export type ConsentState = 'none' | 'given' | 'outdated'

export interface ConsentText {
  version: number
  locale: string
  text: string
  sha256: string
}

export interface AiConsent {
  enabled: boolean
  state: ConsentState
  givenAt: string | null
  consentText: ConsentText
}

export interface Withdrawn {
  withdrawn: boolean
  deletedMessages: number
}

const STATES: readonly ConsentState[] = ['none', 'given', 'outdated']

function decode(value: unknown): AiConsent {
  const root = object(value, 'consent')
  const consent = object(root.consent, 'consent.consent')
  const shown = object(root.consent_text, 'consent.consent_text')
  const state = STATES.find((s) => s === text(consent, 'state'))
  if (state === undefined) throw new DecodeError('state')
  optionalCount(consent, 'version')
  return {
    enabled: flag(root, 'enabled'),
    state,
    givenAt: optionalText(consent, 'given_at'),
    consentText: {
      version: count(shown, 'version'),
      locale: text(shown, 'locale'),
      text: text(shown, 'text'),
      sha256: text(shown, 'sha256'),
    },
  }
}

export async function getAiConsent(locale: string, signal?: AbortSignal): Promise<AiConsent> {
  return decode(await get(`/api/ai/consent?locale=${encodeURIComponent(locale)}`, signal))
}

/** Agree to exactly the text that was shown. */
export async function giveAiConsent(shown: ConsentText): Promise<AiConsent> {
  return decode(
    await post('/api/ai/consent', {
      version: shown.version,
      locale: shown.locale,
      sha256: shown.sha256,
      agree: true,
    }),
  )
}

export async function withdrawAiConsent(): Promise<Withdrawn> {
  const body = object(await del('/api/ai/consent'), 'withdrawn')
  return { withdrawn: flag(body, 'withdrawn'), deletedMessages: count(body, 'deleted_messages') }
}
