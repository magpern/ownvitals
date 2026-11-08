import { expect, test } from '@playwright/test'
import {
  enrolAuthenticator,
  newAccount,
  signInWithPassword,
  signUpAndConfirm,
} from '../helpers/flows.ts'
import { createInvite } from '../helpers/invite.ts'
import { waitForMailHeader } from '../helpers/mailpit.ts'

// Languages end to end (ADR 0015, G7): the picker on a signed-out page, the account language in
// Settings, a reload, the Swedish security and activity pages, and the language of a mail.
test('switch language signed out, sign in, change it in Settings, reload, get Swedish mail', async ({
  page,
}) => {
  const account = newAccount('i18n')
  await signUpAndConfirm(page, account, createInvite())

  // Signed out: the picker changes the page at once.
  await page.goto('/signin')
  await page.getByLabel('Language').selectOption('sv')
  await expect(page.getByRole('heading', { name: 'Logga in', exact: true })).toBeVisible()
  await page.getByLabel('Språk').selectOption('en')
  await expect(page.getByRole('heading', { name: 'Sign in', exact: true })).toBeVisible()

  await signInWithPassword(page, account)
  await enrolAuthenticator(page)

  // Settings: the account's language, applied at once.
  await page.goto('/settings/security')
  await expect(page.getByRole('heading', { name: "Where you're signed in" })).toBeVisible()
  await page.getByLabel('Language').selectOption('sv')
  await expect(page.getByRole('heading', { name: 'Där du är inloggad' })).toBeVisible()

  // A reload keeps it (the session carries the account's language).
  await page.reload()
  await expect(page.getByRole('heading', { name: 'Där du är inloggad' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Logga ut överallt…' })).toBeVisible()

  // The activity page, in Swedish.
  await page.goto('/activity')
  await expect(page.getByRole('heading', { name: 'Aktivitet' })).toBeVisible()
  await expect(page.getByText('Aktivitetssidan kommer i M6.')).toBeVisible()

  // Mail goes out in the account's language: the reset mail for this address.
  await page.getByRole('button', { name: account.email }).click()
  await page.getByRole('menuitem', { name: 'Logga ut' }).click()
  await page.goto('/reset')
  await page.getByLabel('E-post').fill(account.email)
  await page.getByRole('button', { name: 'Skicka återställningslänk' }).click()
  const language = await waitForMailHeader(
    account.email,
    'Återställ ditt ownvitals-lösenord',
    'Content-Language',
  )
  expect(language).toBe('sv')
})
