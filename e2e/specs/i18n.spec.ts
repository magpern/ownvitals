import { expect, test } from '@playwright/test'
import {
  answerGoogleAs,
  continueWithGoogle,
  enrolAuthenticator,
  newGoogleIdentity,
} from '../helpers/flows.ts'
import { createInvite } from '../helpers/invite.ts'
import { waitForMailHeader } from '../helpers/mailpit.ts'

// Languages end to end (ADR 0015, G7): the picker on a signed-out page, the account language in
// Settings, a reload, the Swedish security and activity pages, and the language of a mail.
test('switch language signed out, sign in, change it in Settings, reload, get Swedish mail', async ({
  page,
}) => {
  // A Google account: the password sign-ups of the other specs use up the sign-up budget of the
  // shared stack (5 an hour), and the reset mail below needs no password.
  const account = newGoogleIdentity('i18n')
  await answerGoogleAs(page, account)

  // Signed out: the picker changes the page at once.
  await page.goto('/signin')
  await page.getByLabel('Language').selectOption('sv')
  await expect(page.getByRole('heading', { name: 'Logga in', exact: true })).toBeVisible()
  await page.getByLabel('Språk').selectOption('en')
  await expect(page.getByRole('heading', { name: 'Sign in', exact: true })).toBeVisible()

  await continueWithGoogle(page, createInvite())
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
  await expect(page.getByRole('heading', { name: 'Aktivitet', level: 1 })).toBeVisible()
  await expect(page.getByText('Aktivitetssidan kommer i M6.')).toBeVisible()

  // Mail goes out in the account's language: the reset mail for this address.
  await page.getByRole('button', { name: account.email }).click()
  await page.getByRole('menuitem', { name: 'Logga ut' }).click()
  await expect(page.getByRole('heading', { name: 'Logga in', exact: true })).toBeVisible()
  // The reload shows this browser's own choice (English, set with the picker above), so the
  // request's language is English: the Swedish mail comes from the account's stored language.
  await page.goto('/reset')
  await page.getByLabel('Email').fill(account.email)
  await page.getByRole('button', { name: 'Send reset link' }).click()
  const language = await waitForMailHeader(
    account.email,
    'Återställ ditt ownvitals-lösenord',
    'Content-Language',
  )
  expect(language).toBe('sv')
})
