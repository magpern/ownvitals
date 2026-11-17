import { expect, test } from '@playwright/test'
import { grantAdmin } from '../helpers/admin.ts'
import {
  enrolAuthenticator,
  newAccount,
  passSecondFactor,
  signInWithPassword,
  signUpAndConfirm,
  skipAuthenticator,
  typeCode,
} from '../helpers/flows.ts'
import { ageStepUp, createInvite } from '../helpers/invite.ts'
import { resetRateLimits } from '../helpers/ratelimit.ts'

// The admin page's writes (issue 550): "Invite someone…" asks for a fresh code, shows the new
// invite code once, and that code signs a second account up; "Sign out all…" then ends every
// session of that account. Synthetic accounts only; the invite is the one this test creates.

test('an admin invites someone after a fresh code, then signs that account out everywhere', async ({ page, browser }) => {
  test.setTimeout(180_000)
  resetRateLimits()
  const admin = newAccount('admin')
  await signUpAndConfirm(page, admin, createInvite())
  await signInWithPassword(page, admin)
  const authenticator = await enrolAuthenticator(page)
  grantAdmin(admin.email) // signs the account out everywhere
  await signInWithPassword(page, admin)
  await passSecondFactor(page, authenticator)

  await page.goto('/admin?tab=accounts')
  await expect(page.getByRole('heading', { level: 2, name: 'Accounts' })).toBeVisible()
  // The sign-in's code counts as a step-up for 5 minutes; age it so the write asks for one.
  ageStepUp(admin.email)
  await page.getByRole('button', { name: 'Invite someone…' }).click()
  const stepUp = page.getByRole('dialog', { name: "Confirm it's you" })
  await expect(stepUp).toContainText('Creating an invite')
  await typeCode(stepUp, await authenticator.next())
  await stepUp.getByRole('button', { name: 'Confirm' }).click()

  const shown = page.getByRole('dialog', { name: 'Invite code' })
  await expect(shown).toBeVisible()
  const code = (await shown.getByLabel('Invite code').innerText()).trim()
  expect(code).toMatch(/^\S{16,}$/)
  await shown.getByRole('button', { name: 'Done' }).click()
  await expect(page.getByText(code)).toHaveCount(0)

  // The code signs a second account up, in its own browser.
  const context = await browser.newContext()
  try {
    const other = await context.newPage()
    const invitee = newAccount('invited')
    await signUpAndConfirm(other, invitee, code)
    await signInWithPassword(other, invitee)
    await skipAuthenticator(other)
    await other.goto('/settings/security')
    await expect(other.getByRole('heading', { name: "Where you're signed in" })).toBeVisible()

    await page.reload()
    const row = page.getByRole('row').filter({ hasText: invitee.email })
    await row.getByRole('button', { name: 'Sign out all…' }).click()
    const confirm = page.getByRole('dialog', { name: `Sign out ${invitee.email} everywhere?` })
    await confirm.getByRole('button', { name: 'Sign out all' }).click()
    await expect(confirm).toBeHidden()
    // Its sessions are gone: the row offers nothing more, and the other browser is signed out.
    await expect(row.getByRole('button', { name: 'Sign out all…' })).toHaveCount(0)
    await other.goto('/settings/security')
    await expect(other.getByRole('heading', { name: 'Sign in', exact: true })).toBeVisible()
  } finally {
    await context.close()
  }
})
