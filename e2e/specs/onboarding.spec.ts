import { expect, test } from '@playwright/test'
import { enrolAuthenticator, newAccount, signInWithPassword, signUpAndConfirm, typeCode } from '../helpers/flows.ts'
import { answerWithings, registerApp, resetWorld } from '../helpers/fake-withings.ts'
import { ageStepUp, createInvite } from '../helpers/invite.ts'

// Onboarding (issue 196): a fresh account signs up, enrols the authenticator and is offered Withings.
async function freshAccount(page: Parameters<typeof signUpAndConfirm>[0], label: string) {
  const account = newAccount(label)
  await signUpAndConfirm(page, account, createInvite())
  await signInWithPassword(page, account)
  const authenticator = await enrolAuthenticator(page)
  return { account, authenticator }
}

test('a fresh account lands on Connections with Connect offered, and Onboarding says what is read', async ({
  page,
}) => {
  await freshAccount(page, 'onboard')
  await expect(page.getByText('Not connected.')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Connect Withings' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Disconnect…' })).toHaveCount(0)

  await page.goto('/onboarding')
  await expect(page.getByRole('heading', { name: 'Connect your Withings account' })).toBeVisible()
  await expect(page.getByText('It never changes anything in your Withings account.')).toBeVisible()
  await expect(page.getByText(/13 Oct 2026/)).toBeVisible()
})

// The whole journey (#296): Connect, the step-up, the Withings authorize page (intercepted and
// answered by fake-withings' synthetic user, `answerWithings`), the callback's code exchange in
// web, the first import by the worker, then the overview. Synthetic data only.
test('connects the fake Withings, waits for the import and reaches the overview', async ({ page }) => {
  registerApp(resetWorld())
  await answerWithings(page)
  const { account, authenticator } = await freshAccount(page, 'import')
  ageStepUp(account.email)
  await page.getByRole('button', { name: 'Connect Withings' }).click()
  const dialog = page.getByRole('dialog', { name: "Confirm it's you" })
  await typeCode(dialog, await authenticator.next())
  await dialog.getByRole('button', { name: 'Confirm' }).click()

  await expect(page).toHaveURL(/\/onboarding\?withings=connected/)
  await expect(page.getByRole('heading', { name: 'Your history is imported' })).toBeVisible({ timeout: 60_000 })
  await page.getByRole('link', { name: 'Go to the overview' }).click()
  await expect(page).toHaveURL(/\/$/)
  await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible()
})
