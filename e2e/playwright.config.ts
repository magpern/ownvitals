import { resolve } from 'node:path'
import { defineConfig, devices } from '@playwright/test'

// The stack's ports come from the repository's .env when it exists (scripts/worktree.sh init
// writes it); a variable already in the environment wins. Without either: 8000 and 8025.
try {
  process.loadEnvFile(resolve(import.meta.dirname, '..', '.env'))
} catch {
  // no .env: the defaults in helpers/env.ts apply
}

// localhost, as OWNVITALS_PUBLIC_BASE_URL in compose.e2e.yaml: passkeys need a host name.
const base = `http://localhost:${process.env.WEB_PORT ?? '8000'}`

export default defineConfig({
  testDir: './specs',
  // One worker, no retries: the specs share one stack, one client IP and its rate limits, and a
  // retry would spend sign-up and sign-in budget a failing run needs to be read.
  workers: 1,
  fullyParallel: false,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  forbidOnly: Boolean(process.env.CI),
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: base,
    locale: 'en-GB',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    // Every context starts as a visitor who has seen the cookie notice (it sits over the page
    // and would intercept clicks); cookie-notice.spec.ts starts from an empty state instead.
    storageState: {
      cookies: [],
      origins: [{ origin: base, localStorage: [{ name: 'ownvitals.cookie-notice', value: '1' }] }],
    },
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
})
