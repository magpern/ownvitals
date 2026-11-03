import { randomBytes } from 'node:crypto'
import { expect, type Page } from '@playwright/test'
import { FAKE_OIDC_URL } from './env.ts'
import { tokenLink, waitForMail } from './mailpit.ts'
import { Authenticator } from './totp.ts'

// The user-facing steps the specs share. Everything is synthetic: addresses on example.com, a
// password and Google subject made up per test.

export interface Account {
  email: string
  password: string
}

export function newAccount(label: string): Account {
  const id = randomBytes(4).toString('hex')
  return { email: `e2e-${label}-${id}@example.com`, password: `synthetic-${randomBytes(9).toString('hex')}` }
}

/** Sign-up form, then the confirmation mail's link and password: the account exists afterwards. */
export async function signUpAndConfirm(page: Page, account: Account, invite: string): Promise<void> {
  await page.goto('/signup')
  await page.getByLabel('Invite code').fill(invite)
  await page.getByLabel('Email').fill(account.email)
  await page.getByLabel('Password', { exact: true }).fill(account.password)
  await page.getByRole('button', { name: 'Create account' }).click()
  await expect(page.getByRole('heading', { name: 'Check your inbox' })).toBeVisible()

  const mail = await waitForMail(account.email, 'Confirm your ownvitals account')
  await page.goto(tokenLink(mail))
  await expect(page.getByRole('heading', { name: 'Confirm your account' })).toBeVisible()
  await page.getByLabel('Password').fill(account.password)
  await page.getByRole('button', { name: 'Confirm account' }).click()
  await expect(page.getByRole('heading', { name: 'Account created' })).toBeVisible()
}

export async function signInWithPassword(page: Page, account: Account): Promise<void> {
  await page.goto('/signin')
  await page.getByLabel('Email').fill(account.email)
  await page.getByLabel('Password').fill(account.password)
  await page.getByRole('button', { name: 'Sign in', exact: true }).click()
}

export async function typeCode(scope: Page | ReturnType<Page['getByRole']>, code: string): Promise<void> {
  // A whole code in the first box is spread over the six (autofill's way in).
  await scope.getByLabel('Digit 1').fill(code)
}

/** The mandatory first enrolment: reads the key from the page, confirms, keeps the codes. */
export async function enrolAuthenticator(page: Page): Promise<Authenticator> {
  await expect(page.getByRole('heading', { name: 'Set up your authenticator app' })).toBeVisible()
  const key = page.locator('.ov-auth__qr .ov-mono')
  await expect(key).toBeVisible()
  const authenticator = new Authenticator((await key.innerText()).replace(/\s/g, ''))

  await typeCode(page, await authenticator.next())
  await page.getByRole('button', { name: 'Confirm', exact: true }).click()

  await expect(page.getByRole('heading', { name: 'Save your recovery codes' })).toBeVisible()
  await expect(page.getByRole('list', { name: 'Recovery codes' }).getByRole('listitem')).not.toHaveCount(0)
  await page.getByLabel("I've saved them somewhere safe").check()
  await page.getByRole('button', { name: 'Continue to Connect Withings' }).click()
  await expect(page).toHaveURL(/\/connections$/)
  return authenticator
}

/** The code prompt after a sign-in. */
export async function passSecondFactor(page: Page, authenticator: Authenticator): Promise<void> {
  await expect(page.getByRole('heading', { name: 'Enter your code' })).toBeVisible()
  await typeCode(page, await authenticator.next())
  await page.getByRole('button', { name: 'Verify' }).click()
}

export async function expectSignedIn(page: Page, email: string): Promise<void> {
  await expect(page.getByRole('button', { name: email })).toBeVisible()
}

export async function signOut(page: Page, email: string): Promise<void> {
  await page.getByRole('button', { name: email }).click()
  await page.getByRole('menuitem', { name: 'Sign out' }).click()
  await expect(page.getByRole('heading', { name: 'Sign in', exact: true })).toBeVisible()
}

export interface GoogleIdentity {
  sub: string
  email: string
  email_verified: boolean
}

export function newGoogleIdentity(label: string): GoogleIdentity {
  const id = randomBytes(4).toString('hex')
  return {
    sub: `synthetic-google-${randomBytes(8).toString('hex')}`,
    email: `e2e-${label}-${id}@example.com`,
    email_verified: true,
  }
}

/**
 * Stands in for Google's consent page: the browser's visit to accounts.google.com is answered
 * here. fake-oidc checks the request (client id, redirect URI, PKCE, state, nonce), remembers the
 * identity under a one-time code and gives the redirect back to web's callback, where web
 * exchanges the code with fake-oidc over the compose network.
 */
export async function answerGoogleAs(page: Page, identity: GoogleIdentity): Promise<void> {
  await page.route('https://accounts.google.com/**', async (route) => {
    const response = await fetch(`${FAKE_OIDC_URL}/__test/authorize`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ authorizeUrl: route.request().url(), identity }),
    })
    if (!response.ok) {
      await route.abort()
      throw new Error(`fake-oidc refused the authorization request (${String(response.status)})`)
    }
    const { location } = (await response.json()) as { location: string }
    await route.fulfill({ status: 302, headers: { location } })
  })
}

/** "Continue with Google" on the sign-in page; `invite` only for the first sign-in. */
export async function continueWithGoogle(page: Page, invite?: string): Promise<void> {
  await page.goto('/signin')
  if (invite) await page.getByLabel('Invite code (only for your first sign-in with Google)').fill(invite)
  await page.getByRole('button', { name: 'Continue with Google' }).click()
}
