import { expect, test, type Page } from '@playwright/test'
import {
  answerGoogleAs,
  continueWithGoogle,
  enrolAuthenticator,
  expectSignedIn,
  newAccount,
  newGoogleIdentity,
  passSecondFactor,
  signInWithPassword,
  signOut,
  signUpAndConfirm,
  typeCode,
} from '../helpers/flows.ts'
import { ageStepUp, createInvite } from '../helpers/invite.ts'
import type { Authenticator } from '../helpers/totp.ts'

// Linking Google from Settings → Sign-in and security (issue 516, ADR 0003 sections 1 and 6). A
// Google sign-in never attaches itself to an existing account by its email, so a password account
// links Google here: "Link…" behind the step-up, fake-oidc's consent, the callback's
// /account?oauth=linked back on the Security tab. Then Google signs in to that account, and
// "Unlink…" (behind the step-up) removes it. Everything is synthetic.

async function confirmWithCode(page: Page, authenticator: Authenticator): Promise<void> {
  const dialog = page.getByRole('dialog', { name: "Confirm it's you" })
  await expect(dialog).toBeVisible()
  await typeCode(dialog, await authenticator.next())
  await dialog.getByRole('button', { name: 'Confirm' }).click()
}

test('a password account links Google, signs in with it, and unlinks it', async ({ page }) => {
  const account = newAccount('link')
  await signUpAndConfirm(page, account, createInvite())
  await signInWithPassword(page, account)
  const authenticator = await enrolAuthenticator(page)

  const identity = newGoogleIdentity('link')
  await answerGoogleAs(page, identity)

  await page.goto('/settings/security')
  const link = page.getByRole('button', { name: 'Link…' })
  await expect(link).toBeVisible()
  ageStepUp(account.email) // the enrolment's code counts for ten minutes: age it so the dialog asks
  await link.click()
  await confirmWithCode(page, authenticator)

  await expect(page).toHaveURL(/\/settings\/security$/)
  await expect(page.getByText('Google is linked. You can now sign in with Google.')).toBeVisible()
  await expect(page.getByText(`Linked to ${identity.email}`)).toBeVisible()

  await signOut(page, account.email)
  await continueWithGoogle(page) // no invite: the identity is linked, no account is created
  await passSecondFactor(page, authenticator)
  await expectSignedIn(page, account.email)

  await page.goto('/settings/security')
  const unlink = page.getByRole('button', { name: 'Unlink…' })
  await expect(unlink).toBeVisible()
  ageStepUp(account.email)
  await unlink.click()
  await confirmWithCode(page, authenticator)
  await expect(page.getByRole('button', { name: 'Link…' })).toBeVisible()
  await expect(page.getByText(`Linked to ${identity.email}`)).toHaveCount(0)
})
