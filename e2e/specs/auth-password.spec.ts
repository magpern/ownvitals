import { expect, test } from '@playwright/test'
import {
  enrolAuthenticator,
  expectSignedIn,
  newAccount,
  passSecondFactor,
  signInWithPassword,
  signOut,
  signUpAndConfirm,
  skipAuthenticator,
} from '../helpers/flows.ts'
import { createInvite } from '../helpers/invite.ts'

// M0 done-when "e2e login + TOTP" (PLAN §11): sign up by invite, confirm by mail, the offered
// authenticator enrolment, sign out, then a password sign-in that needs the second factor. The
// authenticator app is optional for users (owner decision of 2026-11-12, issue 475): the second
// test skips it and signs in with the password alone.
test('sign up, enrol the authenticator, sign out, sign in with a code', async ({ page }) => {
  const account = newAccount('password')
  await signUpAndConfirm(page, account, createInvite())

  await signInWithPassword(page, account)
  const authenticator = await enrolAuthenticator(page)
  await expectSignedIn(page, account.email)

  await signOut(page, account.email)

  await signInWithPassword(page, account)
  // The password alone is not a session: the code prompt comes first.
  await passSecondFactor(page, authenticator)
  await expectSignedIn(page, account.email)
})

test('sign up, skip the authenticator, sign out, sign in with the password alone', async ({ page }) => {
  const account = newAccount('nototp')
  await signUpAndConfirm(page, account, createInvite())

  await signInWithPassword(page, account)
  await skipAuthenticator(page)
  await expectSignedIn(page, account.email)

  await signOut(page, account.email)

  // Offered once per browser session: this sign-in goes straight in, with no code prompt.
  await signInWithPassword(page, account)
  await expectSignedIn(page, account.email)
  await expect(page.getByRole('heading', { name: 'Enter your code' })).toBeHidden()
})

test('a wrong password is refused with the generic message', async ({ page }) => {
  const account = newAccount('wrongpw')
  await signUpAndConfirm(page, account, createInvite())

  await signInWithPassword(page, { ...account, password: `${account.password}-wrong` })
  await expect(page.getByRole('alert')).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Sign in', exact: true })).toBeVisible()
})
