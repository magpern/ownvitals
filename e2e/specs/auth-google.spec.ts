import { expect, test } from '@playwright/test'
import {
  answerGoogleAs,
  continueWithGoogle,
  enrolAuthenticator,
  expectSignedIn,
  newGoogleIdentity,
  passSecondFactor,
  signOut,
} from '../helpers/flows.ts'
import { createInvite } from '../helpers/invite.ts'

// Google sign-in through fake-oidc (PLAN §3): the first sign-in creates the account (an invite is
// needed, the address is verified), the authenticator is still mandatory, and the next Google
// sign-in lands on the code prompt.
test('sign in with Google, enrol the authenticator, sign in with Google and a code', async ({ page }) => {
  const identity = newGoogleIdentity('google')
  await answerGoogleAs(page, identity)

  await continueWithGoogle(page, createInvite())
  const authenticator = await enrolAuthenticator(page)
  await expectSignedIn(page, identity.email)

  await signOut(page, identity.email)

  await continueWithGoogle(page)
  await passSecondFactor(page, authenticator)
  await expectSignedIn(page, identity.email)
})

test('a first Google sign-in without an invite creates no account', async ({ page }) => {
  await answerGoogleAs(page, newGoogleIdentity('noinvite'))

  await continueWithGoogle(page)
  await expect(page).toHaveURL(/\/signin\?oauth=invite_required/)
  await expect(page.getByRole('alert')).toBeVisible()
})
