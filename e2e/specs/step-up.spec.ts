import { expect, test } from '@playwright/test'
import {
  enrolAuthenticator,
  newAccount,
  signInWithPassword,
  signUpAndConfirm,
  typeCode,
} from '../helpers/flows.ts'
import { createInvite } from '../helpers/invite.ts'

// A step-up action (ADR 0003 §3): "Sign out everywhere" asks for a fresh code first.
test('signing out everywhere needs a fresh code, then ends the session', async ({ page }) => {
  const account = newAccount('stepup')
  await signUpAndConfirm(page, account, createInvite())
  await signInWithPassword(page, account)
  const authenticator = await enrolAuthenticator(page)

  await page.goto('/settings/security')
  await expect(page.getByRole('heading', { name: "Where you're signed in" })).toBeVisible()
  await page.getByRole('button', { name: 'Sign out everywhere…' }).click()

  const dialog = page.getByRole('dialog', { name: "Confirm it's you" })
  await expect(dialog).toBeVisible()
  // A code from the next step: the enrolment's step is spent (replay protection).
  await typeCode(dialog, await authenticator.next())
  await dialog.getByRole('button', { name: 'Confirm' }).click()

  await expect(page.getByRole('heading', { name: 'Sign in', exact: true })).toBeVisible()
  // The session is gone for good: a protected page sends the browser to sign in.
  await page.goto('/settings/security')
  await expect(page.getByRole('heading', { name: 'Sign in', exact: true })).toBeVisible()
})

test('a wrong code in the step-up dialog changes nothing', async ({ page }) => {
  const account = newAccount('stepupbad')
  await signUpAndConfirm(page, account, createInvite())
  await signInWithPassword(page, account)
  const authenticator = await enrolAuthenticator(page)

  await page.goto('/settings/security')
  await page.getByRole('button', { name: 'Sign out everywhere…' }).click()
  const dialog = page.getByRole('dialog', { name: "Confirm it's you" })
  const right = await authenticator.next()
  // Any six digits that are not the code of the steps the server accepts.
  await typeCode(dialog, right === '000000' ? '111111' : '000000')
  await dialog.getByRole('button', { name: 'Confirm' }).click()
  await expect(dialog.getByRole('alert')).toBeVisible()

  await dialog.getByRole('button', { name: 'Cancel' }).click()
  await expect(dialog).toBeHidden()
  await expect(page.getByRole('heading', { name: "Where you're signed in" })).toBeVisible()
})
