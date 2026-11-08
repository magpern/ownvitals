import { expect, test } from '@playwright/test'
import { postFromPage, reasonOf } from '../helpers/api.ts'
import {
  answerGoogleAs,
  continueWithGoogle,
  enrolAuthenticator,
  expectSignedIn,
  newGoogleIdentity,
} from '../helpers/flows.ts'
import { ageRecentAuthAndStepUp, createInvite } from '../helpers/invite.ts'

// Google re-authentication of an account without a password (ADR 0003 §1, amendment of
// 2026-11-08, #247). fake-oidc answers like the live Google: no auth_time in the ID token, even
// though the request asks for one. The sign-in leads to the mandatory 2FA activation; later a
// Google re-authentication plus a fresh code unlock an action that needs both (setting a first
// password), and the re-authentication alone does not. Assertions on URLs, roles and reason codes.
test('a Google-only account re-authenticates with Google, then steps up with a code', async ({ page }) => {
  const identity = newGoogleIdentity('reauth')
  await answerGoogleAs(page, identity)

  await continueWithGoogle(page, createInvite())
  const authenticator = await enrolAuthenticator(page) // 2FA activation after a Google sign-in
  await expectSignedIn(page, identity.email)

  ageRecentAuthAndStepUp(identity.email)
  const setPassword = { new_password: `synthetic-${identity.sub}` }
  const refused = await postFromPage(page, '/api/auth/password/change', setPassword)
  expect([refused.status, reasonOf(refused)]).toEqual([403, 'reauth_required'])

  const started = await postFromPage(page, '/api/auth/oauth/google/start', {
    purpose: 'reauth',
    invite_code: null,
    return_to: '/settings/security',
  })
  expect(started.status).toBe(200)
  await page.goto((started.body as { authorization_url: string }).authorization_url)
  await expect(page).toHaveURL(/\/settings\/security$/)

  const needsCode = await postFromPage(page, '/api/auth/password/change', setPassword)
  expect([needsCode.status, reasonOf(needsCode)]).toEqual([403, 'step_up_required'])

  const stepUp = await postFromPage(page, '/api/auth/mfa/stepup', { code: await authenticator.next() })
  expect(stepUp.status).toBe(200)
  const done = await postFromPage(page, '/api/auth/password/change', setPassword)
  expect(done.status).toBe(200)
})

test('an account with a password is told to confirm with it, and no Google flow starts', async ({ page }) => {
  const identity = newGoogleIdentity('reauthpw')
  await answerGoogleAs(page, identity)
  await continueWithGoogle(page, createInvite())
  await enrolAuthenticator(page)

  // Give the account a password: the sign-in and the enrolment are fresh proofs for ten minutes.
  const set = await postFromPage(page, '/api/auth/password/change', { new_password: `synthetic-${identity.sub}` })
  expect(set.status).toBe(200)

  const started = await postFromPage(page, '/api/auth/oauth/google/start', { purpose: 'reauth', invite_code: null, return_to: null })
  expect([started.status, reasonOf(started)]).toEqual([403, 'reauth_use_password'])
})
