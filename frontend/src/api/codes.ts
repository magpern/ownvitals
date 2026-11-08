import { isMessageKey, t } from '../i18n/store'
import type { Params } from '../i18n/translate'

// Words the stable codes of the API (ADR 0015 section 6, backend `ownvitals.response_codes`). A
// response carries a code plus numbers, ISO dates or allowlisted enums, never a sentence. The text
// comes from the catalogue (i18n/*/errors.ts for sign-in reasons, i18n/*/codes.ts for the rest).
// A code the catalogue does not know shows the fallback `errors.generic`; the raw code is never
// shown. The sign-in codes of the auth API go through `errorMessage` (messages.ts), which uses
// `codeText` for what errors.ts does not word.

/** The text of a code in the current language, or undefined when no catalogue has it. */
export function codeText(code: string | null | undefined, params?: Params): string | undefined {
  if (code === null || code === undefined) return undefined
  for (const key of [`errors.reason.${code}`, `codes.code.${code}`]) {
    if (isMessageKey(key)) return t(key, params)
  }
  return undefined
}

/** The text of a code, or the English-fallback generic message for an unknown one. */
export function codeMessage(code: string | null | undefined, params?: Params): string {
  return codeText(code, params) ?? t('errors.generic')
}

/** A failed job's `error_category`; a job that did not fail has none. */
export function jobFailureMessage(category: string | null): string | null {
  return category === null ? null : codeMessage(category)
}

/** `removal` of a disconnect or an account deletion: set only when the upstream revoke failed. */
export function removalMessage(removal: string | null): string | null {
  return removal === null ? null : codeMessage(removal)
}

/** `method.code` of a metric detail, worded from `method.parameters` (numbers, as the API sends them). */
export function methodMessage(code: string, parameters: { band_k: number; band_window_days: number }): string {
  return codeMessage(code, { k: parameters.band_k, days: parameters.band_window_days })
}
