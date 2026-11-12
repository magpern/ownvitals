import { expect, test } from '@playwright/test'
import {
  enrolAuthenticator,
  newAccount,
  signInWithPassword,
  signUpAndConfirm,
  skipAuthenticator,
  typeCode,
} from '../helpers/flows.ts'
import { ageRecentAuthAndStepUp, ageStepUp, createInvite } from '../helpers/invite.ts'

// A step-up action (ADR 0003 §3): "Sign out everywhere" asks for a fresh code first.
test('signing out everywhere needs a fresh code, then ends the session', async ({ page }) => {
  const account = newAccount('stepup')
  await signUpAndConfirm(page, account, createInvite())
  await signInWithPassword(page, account)
  const authenticator = await enrolAuthenticator(page)

  await page.goto('/settings/security')
  await expect(page.getByRole('heading', { name: "Where you're signed in" })).toBeVisible()
  // The enrolment's code counts as a step-up for ten minutes, so the action would go straight
  // through (the app is right); age it the way ten minutes would.
  ageStepUp(account.email)
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

// Without an authenticator app (optional for users, issue 475) the password is the step-up.
test('without an authenticator app, signing out everywhere needs the password', async ({ page }) => {
  const account = newAccount('stepupnototp')
  await signUpAndConfirm(page, account, createInvite())
  await signInWithPassword(page, account)
  await skipAuthenticator(page)

  await page.goto('/settings/security')
  await expect(page.getByRole('heading', { name: "Where you're signed in" })).toBeVisible()
  // The sign-in counts as a recent authentication; age it the way ten minutes would.
  ageRecentAuthAndStepUp(account.email)
  await page.getByRole('button', { name: 'Sign out everywhere…' }).click()

  const dialog = page.getByRole('dialog', { name: "Confirm it's you" })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByLabel('Digit 1')).toHaveCount(0)
  await dialog.getByLabel('Password').fill(`${account.password}-wrong`)
  await dialog.getByRole('button', { name: 'Confirm' }).click()
  await expect(dialog.getByRole('alert')).toBeVisible()

  await dialog.getByLabel('Password').fill(account.password)
  await dialog.getByRole('button', { name: 'Confirm' }).click()
  await expect(page.getByRole('heading', { name: 'Sign in', exact: true })).toBeVisible()
  await page.goto('/settings/security')
  await expect(page.getByRole('heading', { name: 'Sign in', exact: true })).toBeVisible()
})

test('a wrong code in the step-up dialog changes nothing', async ({ page }) => {
  const account = newAccount('stepupbad')
  await signUpAndConfirm(page, account, createInvite())
  await signInWithPassword(page, account)
  const authenticator = await enrolAuthenticator(page)

  await page.goto('/settings/security')
  await expect(page.getByRole('heading', { name: "Where you're signed in" })).toBeVisible()
  ageStepUp(account.email)
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
