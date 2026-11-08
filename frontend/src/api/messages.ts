import { isMessageKey, plural, t } from '../i18n/store'
import { ApiError } from './client'
import { codeText } from './codes'

// Plain-language texts for the `reason` codes of the auth API, worded by the catalogue
// (i18n/en/errors.ts). Unknown codes get a generic message; the raw code is never shown.

function known(prefix: 'reason' | 'oauth', code: string | null) {
  const key = `errors.${prefix}.${code ?? ''}`
  return code !== null && isMessageKey(key) ? t(key) : undefined
}

/** Outcome codes the OAuth callback puts in `?oauth=` of /signin. */
export function oauthOutcomeMessage(code: string): string {
  return known('oauth', code) ?? t('errors.oauth.invalid')
}

function waitText(seconds: number): string {
  const minutes = Math.ceil(seconds / 60)
  return minutes > 1 ? plural('errors.minutes', minutes) : plural('errors.seconds', Math.ceil(seconds))
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 429) {
      return error.retryAfter === null
        ? t('errors.tooMany')
        : t('errors.tooManyWait', { wait: waitText(error.retryAfter) })
    }
    const text = known('reason', error.reason) ?? codeText(error.reason)
    if (text !== undefined) return text
    if (error.status === 422) return t('errors.fields')
    if (error.status >= 500) return t('errors.server')
  }
  if (error instanceof TypeError) return t('errors.network')
  return t('errors.generic')
}

export function providerName(provider: string): string {
  return provider === 'google' ? 'Google' : provider
}
