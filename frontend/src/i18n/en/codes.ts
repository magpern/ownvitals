// Texts for the response codes of the API that errors.ts does not word (ADR 0015 section 6): one
// key per code, `code.<code>` (api/codes.ts). A code means one thing in every response that
// carries it, so its text is the same in a refusal, a job's failure and a connection's note.
// Order follows api/codes.json. A code the page does not know shows the fallback (errors.generic).
export const codes = {
  'code.account_disabled': 'Your account is disabled.',
  'code.bad_last_event_id': 'The page asked to resume from a position that does not exist. Reload it.',
  'code.blocked_by_provider_plan':
    'Your Withings plan does not include this data. Nothing is wrong with your connection.',
  'code.daily_summary':
    "Each day's value is the first reading between {start} and {end} local time, otherwise the median of the day's readings. Readings marked unconfirmed, implausible or duplicate are shown but not used. The typical range is the median plus or minus {k} times the scaled median absolute deviation of the last {days} days, never narrower than a minimum half-width. The trend is a Theil-Sen slope with a Mann-Kendall test corrected for autocorrelation. Change points are searched with PELT, only on a series with enough data. A day without a reading is a gap, not zero.",
  'code.decrypt_failed': 'Your connection could not be read. Reconnect your Withings account.',
  'code.email_taken': 'That email address is already in use.',
  'code.idempotency_key_invalid': 'The request could not be sent safely twice. Try again.',
  'code.idempotency_key_missing': 'The request could not be sent safely twice. Try again.',
  'code.idempotency_mismatch': 'That request was already sent with different contents. Try again.',
  'code.internal': 'Something went wrong on our side.',
  'code.invalid_body': 'The request was not valid. Check the fields and try again.',
  'code.invalid_input': 'The request was not valid.',
  'code.key_unavailable': 'Your connection cannot be used right now. Try again later.',
  'code.last_sign_in_method': 'This is your only way to sign in. Add another one before removing it.',
  'code.lease_lost': 'The job stopped unexpectedly. Try again.',
  'code.mfa_required': 'Set up two-factor authentication to continue.',
  'code.name_invalid': 'Choose a name of 1 to 40 characters.',
  'code.needs_reconnect': 'Your Withings connection needs to be renewed. Reconnect it.',
  'code.no_passkeys': 'No passkey is saved on your account.',
  'code.not_cancellable': 'That job has already finished and cannot be cancelled.',
  'code.not_connected': 'Connect your Withings account first.',
  'code.not_found': 'That does not exist, or it is not yours.',
  'code.outbound_blocked': 'The job tried to reach a service it may not use.',
  'code.passkey_failed': 'The passkey could not be used. Try again.',
  'code.passkey_limit': 'You have reached the number of passkeys an account can hold.',
  'code.passkey_signin_failed': 'The passkey was not accepted. Try again or use another way to sign in.',
  'code.passkeys_unavailable': 'Passkeys are not available right now.',
  'code.qr_approval_not_allowed': 'This account cannot approve a sign-in on another device.',
  'code.rate_limited': 'Too many requests were made for now. Try again later.',
  'code.reauth_use_password': 'Use your password or an authenticator code instead.',
  'code.revoke_failed': 'Withings could not be told to stop the access. Remove it in your Withings account.',
  'code.revoke_incomplete':
    "The upstream revoke did not finish. Remove this application's access in the Withings account.",
  'code.too_large': 'The request is too large.',
  'code.too_many_streams': 'Too many pages are open. Close one and try again.',
  'code.unknown_kind': 'That kind of job does not exist.',
  'code.unknown_provider': 'That provider is not available.',
  'code.upstream_unavailable': 'The service did not answer in time. Try again later.',
} satisfies Record<string, string>
