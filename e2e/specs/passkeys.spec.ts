import { expect, test, type CDPSession, type Page } from '@playwright/test'
import {
  enrolAuthenticator,
  expectSignedIn,
  newAccount,
  passSecondFactor,
  signInWithPassword,
  signOut,
  signUpAndConfirm,
  typeCode,
  type Account,
} from '../helpers/flows.ts'
import { ageRecentAuthAndStepUp, ageStepUp, createInvite } from '../helpers/invite.ts'
import { resetRateLimits } from '../helpers/ratelimit.ts'
import type { Authenticator } from '../helpers/totp.ts'

// Passkeys end to end (ADR 0011 section 8, M3 done-when): Chromium with a CDP virtual
// authenticator (user verification on, a discoverable credential) adds a passkey behind the
// "Confirm it's you" dialog, signs in with it (the authenticator code still follows), and removes
// it behind the dialog (password, then code). The credential is the virtual authenticator's own
// synthetic key; no real device or account.

async function addVirtualAuthenticator(page: Page): Promise<{ cdp: CDPSession; authenticatorId: string }> {
  const cdp = await page.context().newCDPSession(page)
  await cdp.send('WebAuthn.enable')
  const { authenticatorId } = await cdp.send('WebAuthn.addVirtualAuthenticator', {
    options: {
      protocol: 'ctap2',
      transport: 'internal',
      hasResidentKey: true,
      hasUserVerification: true,
      isUserVerified: true,
      automaticPresenceSimulation: true,
    },
  })
  return { cdp, authenticatorId }
}

/** The shared dialog: the password when it asks for it, then a fresh code. */
async function confirmItsYou(page: Page, account: Account, authenticator: Authenticator, expectPassword: boolean) {
  const dialog = page.getByRole('dialog', { name: "Confirm it's you" })
  await expect(dialog).toBeVisible()
  if (expectPassword) {
    await expect(dialog).toContainText('needs your password')
    await dialog.getByLabel('Password', { exact: true }).fill(account.password)
    await dialog.getByRole('button', { name: 'Confirm' }).click()
    await expect(dialog).toContainText('needs a fresh code')
  }
  await typeCode(dialog, await authenticator.next())
  await dialog.getByRole('button', { name: 'Confirm' }).click()
  await expect(dialog).toBeHidden()
}

test('add a passkey with step-up, sign in with it, remove it with step-up', async ({ page }) => {
  test.setTimeout(180_000)
  resetRateLimits()
  const account = newAccount('passkey')
  await signUpAndConfirm(page, account, createInvite())
  await signInWithPassword(page, account)
  const authenticator = await enrolAuthenticator(page)
  const { cdp, authenticatorId } = await addVirtualAuthenticator(page)

  // Add: the sign-in is recent, the enrolment's step-up is aged, so the dialog asks for a code.
  await page.goto('/settings/security')
  const add = page.getByRole('button', { name: 'Add a passkey…' })
  await expect(page.getByText('No passkey yet.')).toBeVisible()
  ageStepUp(account.email)
  await add.click()
  await confirmItsYou(page, account, authenticator, false)
  const passkeys = page.getByRole('list', { name: 'Your passkeys' })
  await expect(passkeys.getByRole('listitem')).toHaveCount(1)
  const { credentials } = await cdp.send('WebAuthn.getCredentials', { authenticatorId })
  expect(credentials).toHaveLength(1)
  expect(credentials[0]?.isResidentCredential).toBe(true)

  // Sign in with it: the passkey replaces the password, never the code.
  await signOut(page, account.email)
  await page.getByRole('button', { name: 'Sign in with a passkey' }).click()
  await passSecondFactor(page, authenticator)
  await expectSignedIn(page, account.email)

  // Remove: both proofs aged, so the dialog asks for the password, then the code, then retries.
  await page.goto('/settings/security')
  await expect(passkeys.getByRole('listitem')).toHaveCount(1)
  await expect(passkeys).not.toContainText('not used yet')
  ageRecentAuthAndStepUp(account.email)
  await passkeys.getByRole('button', { name: /^Remove / }).click()
  await confirmItsYou(page, account, authenticator, true)
  await expect(page.getByText('No passkey yet.')).toBeVisible()
  await expect(passkeys).toBeHidden()
})
