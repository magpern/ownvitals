import { expect, test, type BrowserContext, type Page } from '@playwright/test'
import {
  enrolAuthenticator,
  expectSignedIn,
  newAccount,
  signInWithPassword,
  signUpAndConfirm,
  typeCode,
  type Account,
} from '../helpers/flows.ts'
import { createInvite } from '../helpers/invite.ts'
import { resetRateLimits } from '../helpers/ratelimit.ts'
import type { Authenticator } from '../helpers/totp.ts'

// QR approve-login end to end (ADR 0011 section 5, M3 done-when) with two browser contexts: the
// test's own page is the desktop (no session), a second context is the "phone" (signed in, through
// its code). The phone opens the QR's address: the nonce is read from the desktop's
// `POST /api/auth/qr/start` answer as the camera would read it from the QR, so the app has no
// test-only hook. A synthetic account; no real device.

test.describe.configure({ mode: 'serial' })

let phoneContext: BrowserContext
let phone: Page
let account: Account
let authenticator: Authenticator

test.beforeAll(async ({ browser }, testInfo) => {
  testInfo.setTimeout(120_000)
  resetRateLimits()
  phoneContext = await browser.newContext()
  phone = await phoneContext.newPage()
  account = newAccount('qrphone')
  await signUpAndConfirm(phone, account, createInvite())
  await signInWithPassword(phone, account)
  authenticator = await enrolAuthenticator(phone)
})

test.afterAll(async () => {
  await phoneContext.close()
})

/** The desktop's "Sign in with your phone"; returns the nonce of the QR it shows. */
async function showQr(desktop: Page): Promise<string> {
  const started = desktop.waitForResponse(
    (response) => response.request().method() === 'POST' && new URL(response.url()).pathname === '/api/auth/qr/start',
  )
  await desktop.getByRole('button', { name: 'Sign in with your phone' }).click()
  const { qr_url: url } = (await (await started).json()) as { qr_url: string }
  await expect(desktop.getByRole('img', { name: 'QR code to sign in with your phone' })).toBeVisible()
  const nonce = new URL(url).hash.slice(1)
  expect(nonce).toMatch(/^[A-Za-z0-9_-]{43}$/)
  return nonce
}

/** The phone opens the QR's address and sees the request. */
async function scan(nonce: string): Promise<void> {
  await phone.goto(`/qr#${nonce}`)
  await expect(phone.getByRole('heading', { name: 'Approve a sign-in?' })).toBeVisible()
  // The nonce is gone from the address bar at once.
  expect(new URL(phone.url()).hash).toBe('')
  await expect(phone.getByText(/as reported by that browser$/)).toBeVisible()
  await expect(phone.getByText('Only approve if you are signing in on a computer in front of you right now.', { exact: false })).toBeVisible()
}

test('the phone approves with a code and the desktop is signed in to that account', async ({ page: desktop }) => {
  test.setTimeout(120_000)
  await desktop.goto('/signin')
  const nonce = await showQr(desktop)
  await scan(nonce)
  await expect(desktop.getByText('Scanned. Approve the sign-in on your phone.')).toBeVisible()

  await phone.getByRole('button', { name: 'Approve…' }).click()
  const dialog = phone.getByRole('dialog', { name: "Confirm it's you" })
  await expect(dialog).toContainText('entered for this approval only')
  await typeCode(dialog, await authenticator.next())
  await dialog.getByRole('button', { name: 'Confirm' }).click()
  await expect(phone.getByRole('heading', { name: 'Sign-in approved' })).toBeVisible()

  // "Signed in as …" before anything else, then the app.
  await expect(desktop.getByText(`Signed in as ${account.email}`)).toBeVisible()
  await desktop.getByRole('button', { name: 'Continue' }).click()
  await expectSignedIn(desktop, account.email)
})

test('the phone denies and the desktop is told, without a session', async ({ page: desktop }) => {
  await desktop.goto('/signin')
  const nonce = await showQr(desktop)
  await scan(nonce)
  await phone.getByRole('button', { name: 'Deny' }).click()
  await expect(phone.getByRole('heading', { name: 'Sign-in denied' })).toBeVisible()
  await expect(desktop.getByText('The sign-in was denied on the phone.')).toBeVisible()
  await desktop.goto('/settings/security')
  await expect(desktop.getByRole('heading', { name: 'Sign in', exact: true })).toBeVisible()
})

test('an unscanned code expires after 60 seconds, and a new code retires the old one', async ({ page: desktop }) => {
  // The desktop's clock is faked, so the 60 s pass at once on the page; the server's own
  // deadline is checked by the backend tests (test_auth_qrlogin.py).
  await desktop.clock.install()
  await desktop.goto('/signin')
  const old = await showQr(desktop)
  await desktop.clock.fastForward(59_000)
  await expect(desktop.getByText('This code has expired.')).toBeHidden()
  await desktop.clock.fastForward(1_000)
  await expect(desktop.getByText('This code has expired.')).toBeVisible()
  await expect(desktop.getByRole('img', { name: 'QR code to sign in with your phone' })).toBeHidden()

  // "Show a new code" starts a new request; the server expires the previous one, so a scan of
  // the old code gets the one generic answer.
  const started = desktop.waitForResponse((response) => new URL(response.url()).pathname === '/api/auth/qr/start')
  await desktop.getByRole('button', { name: 'Show a new code' }).click()
  await started
  await expect(desktop.getByRole('img', { name: 'QR code to sign in with your phone' })).toBeVisible()
  await phone.goto(`/qr#${old}`)
  await expect(phone.getByRole('heading', { name: 'This code cannot be used' })).toBeVisible()
})
