import { readFile } from 'node:fs/promises'
import { expect, test } from '@playwright/test'
import { enrolAuthenticator, newAccount, signInWithPassword, signUpAndConfirm, typeCode } from '../helpers/flows.ts'
import { ageStepUp, createInvite } from '../helpers/invite.ts'

// The JSON/CSV export (issue 211, ADR 0013 section 4): "Export…" asks for a fresh code, the
// worker builds the archive, "Download" saves it once, and the row offers a new export after.
// A made-up account with no Withings data; the archive holds its account file and the manifest.

test('step-up, export, download once', async ({ page }) => {
  test.setTimeout(120_000)
  const account = newAccount('export')
  await signUpAndConfirm(page, account, createInvite())
  await signInWithPassword(page, account)
  const authenticator = await enrolAuthenticator(page)

  await page.goto('/settings/data')
  const start = page.getByRole('button', { name: 'Export…' })
  await expect(start).toBeEnabled()
  // The enrolment's code counts as a step-up for ten minutes; age it so the dialog asks.
  ageStepUp(account.email)
  await start.click()
  const dialog = page.getByRole('dialog', { name: "Confirm it's you" })
  await expect(dialog).toBeVisible()
  await typeCode(dialog, await authenticator.next())
  await dialog.getByRole('button', { name: 'Confirm' }).click()
  await expect(dialog).toBeHidden()

  // The worker builds it; the row polls until it is ready.
  const download = page.getByRole('button', { name: 'Download' })
  await expect(download).toBeVisible({ timeout: 90_000 })
  await expect(page.getByText(/Your export is ready/)).toBeVisible()

  const saved = page.waitForEvent('download')
  await download.click()
  const file = await saved
  expect(file.suggestedFilename()).toMatch(/^ownvitals-export-\d{4}-\d{2}-\d{2}\.zip$/)
  const path = await file.path()
  const body = await readFile(path)
  expect(body.subarray(0, 4).toString('latin1')).toBe('PK\u0003\u0004')
  // Entry names are stored uncompressed in the zip's headers.
  const names = body.toString('latin1')
  for (const entry of ['manifest.json', 'data/account.json', 'csv/account.csv', 'data/findings.json']) {
    expect(names).toContain(entry)
  }
  expect(names).not.toContain(account.password)

  await expect(page.getByText(/Downloaded\. The file is now deleted/)).toBeVisible()
  await expect(page.getByRole('button', { name: 'Export…' })).toBeVisible()
})
