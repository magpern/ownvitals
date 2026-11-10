import { readFile } from 'node:fs/promises'
import { inflateRawSync } from 'node:zlib'
import { expect, test, type Page } from '@playwright/test'
import { answerWithings, registerApp, resetWorld } from '../helpers/fake-withings.ts'
import { enrolAuthenticator, newAccount, signInWithPassword, signUpAndConfirm, typeCode } from '../helpers/flows.ts'
import { ageStepUp, createInvite } from '../helpers/invite.ts'
import type { Authenticator } from '../helpers/totp.ts'

// The JSON/CSV export (issue 211, ADR 0013 section 4): "Export…" asks for a fresh code, the
// worker builds the archive, "Download" saves it once, and the row offers a new export after.
// Made-up accounts only: one with no Withings data, and one connected to fake-withings'
// synthetic user, whose archive must hold the imported measures. Synthetic data only.

// The archive's entries by name, read from the central directory. The builder opens every entry
// with `force_zip64`, so a size or an offset may sit in the zip64 extra field instead.
function unzip(body: Buffer): Map<string, Buffer> {
  const end = body.lastIndexOf(Buffer.from([0x50, 0x4b, 0x05, 0x06]))
  expect(end).toBeGreaterThan(0)
  const count = body.readUInt16LE(end + 10)
  let at = body.readUInt32LE(end + 16)
  const entries = new Map<string, Buffer>()
  for (let i = 0; i < count; i++) {
    expect(body.readUInt32LE(at)).toBe(0x02014b50)
    const method = body.readUInt16LE(at + 10)
    let size = body.readUInt32LE(at + 20)
    const uncompressedIs64 = body.readUInt32LE(at + 24) === 0xffffffff
    const nameLength = body.readUInt16LE(at + 28)
    const extraLength = body.readUInt16LE(at + 30)
    const commentLength = body.readUInt16LE(at + 32)
    let offset = body.readUInt32LE(at + 42)
    const name = body.toString('utf8', at + 46, at + 46 + nameLength)
    let extra = at + 46 + nameLength
    const extraEnd = extra + extraLength
    while (extra + 4 <= extraEnd) {
      const id = body.readUInt16LE(extra)
      const length = body.readUInt16LE(extra + 2)
      if (id === 0x0001) {
        let field = extra + 4
        if (uncompressedIs64) field += 8
        if (size === 0xffffffff) {
          size = Number(body.readBigUInt64LE(field))
          field += 8
        }
        if (offset === 0xffffffff) offset = Number(body.readBigUInt64LE(field))
      }
      extra += 4 + length
    }
    const start = offset + 30 + body.readUInt16LE(offset + 26) + body.readUInt16LE(offset + 28)
    const data = body.subarray(start, start + size)
    entries.set(name, method === 8 ? inflateRawSync(data) : Buffer.from(data))
    at = extraEnd + commentLength
  }
  return entries
}

interface Manifest {
  format: string
  files: { path: string; rows: number }[]
}

function text(entries: Map<string, Buffer>, name: string): string {
  const content = entries.get(name)
  expect(content, name).toBeDefined()
  return content?.toString('utf8') ?? ''
}

function rowsOf(manifest: Manifest, path: string): number {
  const entry = manifest.files.find((file) => file.path === path)
  expect(entry, path).toBeDefined()
  return entry?.rows ?? 0
}

// Settings → Your data: "Export…" behind the step-up dialog, the worker's build, then the one
// download. Returns the archive's entries.
async function exportAndDownload(page: Page, email: string, authenticator: Authenticator): Promise<Map<string, Buffer>> {
  await page.goto('/settings/data')
  const start = page.getByRole('button', { name: 'Export…' })
  await expect(start).toBeEnabled()
  // An earlier code counts as a step-up for ten minutes; age it so the dialog asks.
  ageStepUp(email)
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
  const body = await readFile(await file.path())
  expect(body.subarray(0, 4).toString('latin1')).toBe('PK\u0003\u0004')

  await expect(page.getByText(/Downloaded\. The file is now deleted/)).toBeVisible()
  await expect(page.getByRole('button', { name: 'Export…' })).toBeVisible()
  return unzip(body)
}

test('step-up, export, download once', async ({ page }) => {
  test.setTimeout(120_000)
  const account = newAccount('export')
  await signUpAndConfirm(page, account, createInvite())
  await signInWithPassword(page, account)
  const authenticator = await enrolAuthenticator(page)

  const entries = await exportAndDownload(page, account.email, authenticator)
  for (const entry of ['manifest.json', 'data/account.json', 'csv/account.csv', 'data/findings.json']) {
    expect(entries.has(entry), entry).toBe(true)
  }
  const manifest = JSON.parse(text(entries, 'manifest.json')) as Manifest
  expect(manifest.format).toBe('ownvitals-export/1')
  const accountRows = JSON.parse(text(entries, 'data/account.json')) as { email: string }[]
  expect(accountRows.map((row) => row.email)).toEqual([account.email])
  for (const content of entries.values()) expect(content.toString('utf8')).not.toContain(account.password)
})

// The fake account: connect fake-withings' synthetic user (as onboarding.spec.ts does), wait for
// the first import, then export; the archive holds the imported measure groups and measures.
test('the fake Withings account: connect, import, export, download', async ({ page }) => {
  test.setTimeout(180_000)
  registerApp(resetWorld())
  await answerWithings(page)
  const account = newAccount('export-withings')
  await signUpAndConfirm(page, account, createInvite())
  await signInWithPassword(page, account)
  const authenticator = await enrolAuthenticator(page)

  ageStepUp(account.email)
  await page.getByRole('button', { name: 'Connect Withings' }).click()
  const dialog = page.getByRole('dialog', { name: "Confirm it's you" })
  await typeCode(dialog, await authenticator.next())
  await dialog.getByRole('button', { name: 'Confirm' }).click()
  await expect(page).toHaveURL(/\/onboarding\?withings=connected/)
  await expect(page.getByRole('heading', { name: 'Your history is imported' })).toBeVisible({ timeout: 60_000 })

  const entries = await exportAndDownload(page, account.email, authenticator)
  const manifest = JSON.parse(text(entries, 'manifest.json')) as Manifest
  expect(rowsOf(manifest, 'data/connections.json')).toBe(1)
  const groups = rowsOf(manifest, 'data/measure_groups.json')
  const measures = rowsOf(manifest, 'data/measures.json')
  expect(groups).toBeGreaterThan(0)
  expect(measures).toBeGreaterThan(0)
  expect((JSON.parse(text(entries, 'data/measure_groups.json')) as unknown[]).length).toBe(groups)
  expect((JSON.parse(text(entries, 'data/measures.json')) as unknown[]).length).toBe(measures)
  for (const content of entries.values()) expect(content.toString('utf8')).not.toContain(account.password)
})
