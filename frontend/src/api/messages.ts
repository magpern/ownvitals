import { ApiError } from './client'

// Plain-language texts for the `reason` codes of the auth API. Unknown codes get a generic
// message; the raw code is never shown.
const REASONS: Record<string, string> = {
  invalid_invite: 'That invite code is not valid, or it has already been used.',
  invite_invalid: 'The invite for this sign-up is no longer valid. Ask for a new one.',
  signups_closed: 'Sign-ups are closed right now.',
  password_rule: 'Choose a password of 12 to 128 characters.',
  invalid_credentials: 'The email or password is not right.',
  wrong_password: 'That is not the password you chose when you signed up.',
  invalid_token: 'This link is not valid any more. It may have expired or been used already.',
  wrong_browser:
    'Finish on the device and browser where you started the sign-in. This link will not work here.',
  account_exists: 'An account with this address already exists. Sign in to it instead.',
  invalid_code: 'That code is not right. Check the app and try again.',
  totp_changed: 'Your two-factor setup changed meanwhile. Start again.',
  totp_not_enabled: 'Two-factor authentication is not set up.',
  not_authenticated: 'You are signed out. Sign in and try again.',
  no_pending_signup: 'No sign-in is waiting for an address. Start the Google sign-in again.',
  provider_disabled: 'That sign-in method is not available.',
  forbidden: 'Your account is not allowed to do that.',
  two_factor_required: 'Enter a code from your authenticator app to continue.',
  step_up_required: 'This needs a fresh code from your authenticator app.',
  reauth_required: 'This needs your password again.',
  reauth_stale: 'The sign-in at Google was not recent enough. Try again.',
  busy: 'The server is busy. Try again in a few seconds.',
}

// Outcome codes the OAuth callback puts in `?oauth=` of /signin.
const OAUTH_OUTCOMES: Record<string, string> = {
  invalid: 'The sign-in could not be completed. Try again.',
  denied: 'The sign-in was cancelled at Google.',
  session_mismatch: 'You were signed out or switched account while signing in. Sign in again.',
  account_exists:
    'An account with that email already exists. Sign in to it with your password instead.',
  invite_required:
    'You need an invite to create an account. Enter your invite code below and try again.',
  invite_invalid: 'That invite is no longer valid. Ask for a new one.',
}

export function oauthOutcomeMessage(code: string): string {
  const known = Object.hasOwn(OAUTH_OUTCOMES, code) ? OAUTH_OUTCOMES[code] : undefined
  return known ?? 'The sign-in could not be completed. Try again.'
}

function waitText(seconds: number): string {
  const minutes = Math.ceil(seconds / 60)
  return minutes > 1 ? `${String(minutes)} minutes` : `${String(Math.ceil(seconds))} seconds`
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 429) {
      return error.retryAfter === null
        ? 'Too many attempts. Wait a little and try again.'
        : `Too many attempts. Try again in ${waitText(error.retryAfter)}.`
    }
    const known =
      error.reason !== null && Object.hasOwn(REASONS, error.reason) ? REASONS[error.reason] : undefined
    if (known !== undefined) return known
    if (error.status === 422) return 'Check the fields and try again.'
    if (error.status >= 500) return 'Something went wrong on the server. Try again in a moment.'
  }
  if (error instanceof TypeError) return 'The server could not be reached. Check your connection.'
  return 'That did not work. Try again.'
}

export function providerName(provider: string): string {
  return provider === 'google' ? 'Google' : provider
}
