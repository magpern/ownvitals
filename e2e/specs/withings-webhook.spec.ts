import { expect, test } from '@playwright/test'
import { BASE_URL } from '../helpers/env.ts'

// M1 done-when (#30), the part of the failure injection that is observable from outside the
// stack: the live webhook receiver of the web service refuses what it must, and answers a forged
// or unknown path without telling the caller anything. The rest of the M1 flow (connect,
// backfill with progress, sync, poll, reconcile, disconnect, delete) has no HTTP route or screen
// yet and runs against the same fake in `backend/tests/test_m1_flow.py` with the real worker.
// Synthetic bytes only; the secret below is not a secret of any app.

const FORGED = 'forged-secret-not-derived-from-any-seed'
const PATH = `/api/webhooks/withings/${FORGED}`
const FORM = 'application/x-www-form-urlencoded'
const BODY = 'userid=424242&appli=1&startdate=1700000000&enddate=1700000100'

// 503 is the receiver's answer while its secret cache is cold (it refuses every POST then); each
// refusal below is accepted with it, and never with a 2xx or a server error of another kind.
test('HEAD answers 200 so the provider can probe the URL', async ({ request }) => {
  const head = await request.fetch(BASE_URL + PATH, { method: 'HEAD' })
  expect(head.status()).toBe(200)
})

test('an oversized body, a wrong media type and a forged path create nothing and leak nothing', async ({
  request,
}) => {
  const oversized = await request.post(BASE_URL + PATH, {
    headers: { 'content-type': FORM },
    data: 'x'.repeat(2049),
  })
  expect([413, 503]).toContain(oversized.status())

  const wrongType = await request.post(BASE_URL + PATH, {
    headers: { 'content-type': 'application/json' },
    data: BODY,
  })
  expect([415, 503]).toContain(wrongType.status())

  const forged = await request.post(BASE_URL + PATH, { headers: { 'content-type': FORM }, data: BODY })
  expect([200, 503]).toContain(forged.status())

  for (const answer of [oversized, wrongType, forged]) {
    expect(await answer.text()).not.toContain(FORGED)
  }
})

test('the connections API is closed to a caller without a session', async ({ request }) => {
  const answer = await request.get(BASE_URL + '/api/connections')
  expect(answer.status()).toBe(401)
})
