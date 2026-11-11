import { execFileSync } from 'node:child_process'
import { readFile } from 'node:fs/promises'
import { inflateRawSync } from 'node:zlib'
import { expect, test, type Page } from '@playwright/test'
import { REPO_ROOT } from '../helpers/env.ts'
import { answerWithings, registerApp, resetWorld } from '../helpers/fake-withings.ts'
import { enrolAuthenticator, newAccount, signInWithPassword, signUpAndConfirm, typeCode } from '../helpers/flows.ts'
import { ageStepUp, createInvite } from '../helpers/invite.ts'
import { resetRateLimits } from '../helpers/ratelimit.ts'
import type { Authenticator } from '../helpers/totp.ts'

// The JSON/CSV export (issue 211, ADR 0013 section 4): "Export…" asks for a fresh code, the
// worker builds the archive, "Download" saves it once, and the row offers a new export after.
// Made-up accounts only: one with no Withings data, and one connected to fake-withings'
// synthetic user, whose archive must hold the imported measures. Synthetic data only.

// The specs share one client IP and its sign-up budget (5 an hour, ADR 0003 section 9). This spec
// signs up two accounts, so it starts from a clean budget and leaves one for the specs after it.
test.beforeAll(() => {
  resetRateLimits()
})
test.afterAll(() => {
  resetRateLimits()
})

// The archive's entries by name, read from the central directory. Python's zipfile writes the
// real sizes and offsets there for an archive under 4 GiB (only the local headers carry the zip64
// markers of `force_zip64`), so a zip64 marker in the central directory fails the test instead
// of being read: an e2e archive is never that large.
function unzip(body: Buffer): Map<string, Buffer> {
  const end = body.lastIndexOf(Buffer.from([0x50, 0x4b, 0x05, 0x06]))
  expect(end).toBeGreaterThan(0)
  const count = body.readUInt16LE(end + 10)
  let at = body.readUInt32LE(end + 16)
  const entries = new Map<string, Buffer>()
  for (let i = 0; i < count; i++) {
    expect(body.readUInt32LE(at)).toBe(0x02014b50)
    const method = body.readUInt16LE(at + 10)
    const size = body.readUInt32LE(at + 20)
    const nameLength = body.readUInt16LE(at + 28)
    const extraLength = body.readUInt16LE(at + 30)
    const commentLength = body.readUInt16LE(at + 32)
    const offset = body.readUInt32LE(at + 42)
    const name = body.toString('utf8', at + 46, at + 46 + nameLength)
    expect(size, name).not.toBe(0xffffffff)
    expect(offset, name).not.toBe(0xffffffff)
    const start = offset + 30 + body.readUInt16LE(offset + 26) + body.readUInt16LE(offset + 28)
    const data = body.subarray(start, start + size)
    entries.set(name, method === 8 ? inflateRawSync(data) : Buffer.from(data))
    at += 46 + nameLength + extraLength + commentLength
  }
  return entries
}

// A count about the account, read as the database superuser (like `ageStepUp`). `where` is a
// fixed condition of this file, never input.
function countFor(email: string, table: 'jobs' | 'measure_groups', where: string): number {
  if (!/^[\w.+-]+@[\w.-]+$/.test(email)) throw new Error('unexpected address')
  const sql =
    `SELECT count(*) FROM ${table} WHERE ${where} AND owner_id = ` +
    `(SELECT id FROM users WHERE email_normalized = '${email.toLowerCase()}')`
  const out = execFileSync(
    'docker',
    [
      'compose', '-f', 'compose.yaml', '-f', 'compose.e2e.yaml',
      'exec', '-T', 'db',
      'psql', '-U', 'postgres', '-d', 'ownvitals', '-v', 'ON_ERROR_STOP=1', '-tA', '-c', sql,
    ],
    { cwd: REPO_ROOT, encoding: 'utf8' },
  )
  return Number(out.trim())
}

// The import is over for the export when the measures are in and no job is left. The jobs matter
// because an export request counts the queued and running ones against the per-user job limit
// (`jobs_per_user`, 2) and answers 429 `busy`. The measures matter because the job queue can be
// empty before they are: a backfill attempt whose rate claim is refused ends `rate_limited` and
// spends an attempt, so the measures backfill can be dead-lettered (3 attempts) with nothing
// imported. The import alone is one connect and seven backfills, about 270 calls at the
// limiter's 100 a minute: about 2 minutes. Since #410 the scheduler skips a connection while its
// import is queued or running, so no tick adds a poll or a reconcile to it.
const ACTIVE = "state IN ('queued', 'running')"
const MEASURES_DEAD = "kind = 'withings_backfill' AND input->>'stream' = 'measures' AND state = 'dead_letter'"
const IMPORT_WAIT = 240_000

async function waitForImport(email: string, timeout: number): Promise<void> {
  await expect
    .poll(
      () => {
        if (countFor(email, 'jobs', MEASURES_DEAD) > 0) return 'measures backfill dead-lettered'
        const groups = countFor(email, 'measure_groups', 'deleted_at IS NULL')
        const active = countFor(email, 'jobs', ACTIVE)
        return groups > 0 && active === 0 ? 'imported' : `${String(groups)} groups, ${String(active)} jobs`
      },
      { timeout, intervals: [2_000] },
    )
    .toMatch(/^(imported|measures backfill dead-lettered)$/)
  expect(countFor(email, 'jobs', MEASURES_DEAD), 'the measures backfill was dead-lettered').toBe(0)
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

  // The request was accepted (a refusal shows its reason here) and the row polls until ready.
  await expect(page.getByText('The server is busy. Try again in a few seconds.')).toHaveCount(0)
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
// the first import and its jobs to end, then export; the archive holds the imported measure
// groups and measures. The end of the import is read from the jobs, not from the onboarding
// page: its "Your history is imported" can show before the connect job has written the streams.
test('the fake Withings account: connect, import, export, download', async ({ page }) => {
  test.setTimeout(IMPORT_WAIT + 240_000)
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
  await expect(page.getByText('Step 3 of 3')).toBeVisible()
  await waitForImport(account.email, IMPORT_WAIT)

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
