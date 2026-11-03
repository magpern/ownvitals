import { randomBytes } from 'node:crypto'
import { expect, test } from '@playwright/test'
import { newAccount, signInWithPassword, signUpAndConfirm } from '../helpers/flows.ts'
import { createInvite } from '../helpers/invite.ts'
import { tokenLink, waitForMail } from '../helpers/mailpit.ts'

// Password reset through mailpit: the mail's link, a new password, and the old one stops working.
test('reset the password from the mailed link', async ({ page }) => {
  const account = newAccount('reset')
  await signUpAndConfirm(page, account, createInvite())

  await page.goto('/reset')
  await page.getByLabel('Email').fill(account.email)
  await page.getByRole('button', { name: 'Send reset link' }).click()
  await expect(page.getByRole('heading', { name: 'Check your inbox' })).toBeVisible()

  const mail = await waitForMail(account.email, 'Reset your ownvitals password')
  await page.goto(tokenLink(mail))
  await expect(page.getByRole('heading', { name: 'Choose a new password' })).toBeVisible()
  const newPassword = `synthetic-${randomBytes(9).toString('hex')}`
  await page.getByLabel('New password').fill(newPassword)
  await page.getByRole('button', { name: 'Change password' }).click()
  await expect(page.getByRole('heading', { name: 'Password changed' })).toBeVisible()

  // The old password is refused.
  await signInWithPassword(page, account)
  await expect(page.getByRole('alert')).toBeVisible()

  // The new one signs in, as far as the mandatory authenticator step.
  await signInWithPassword(page, { ...account, password: newPassword })
  await expect(page.getByRole('heading', { name: 'Set up your authenticator app' })).toBeVisible()
})
