import { execFileSync } from 'node:child_process'
import { expect, test } from '@playwright/test'
import { control, pythonIn, resetWorld } from '../helpers/fake-withings.ts'
import { REPO_ROOT } from '../helpers/env.ts'

// Smoke test of the fake Withings (#36): the worker's own client reaches it as
// wbsapi.withings.net over TLS from the throwaway CA, and nothing outside the compose network
// can. Synthetic data only; the real Withings is never contacted (the browser never visits a
// Withings host here: it resolves, on a developer machine, to the real one).

// The fake is shared: the worker also calls it for the live connections of earlier specs (their
// backfills, and the scheduler's poll and reconcile, #403), at any moment. Those calls carry
// tokens of an earlier world, so after `resetWorld()` they are refused, but the fake still counts
// them. So the specs below never assume a request number or a count is theirs alone.

// What the worker does: the real egress allowlist and the real Withings client, given a token pair
// of the fake's synthetic user. Prints shapes only.
const WORKER_CALLS = `
import asyncio, json, sys
from ownvitals import egress
from ownvitals.withings.client import WithingsApp, WithingsClient
from ownvitals.withings.errors import TokenInvalid

client_id, secret, token = sys.argv[1:4]

async def main():
    egress.install_worker_allowlist(ai_enabled=False)
    out = {}
    async with WithingsClient.open(WithingsApp(client_id, secret)) as client:
        goals = await client.getgoals(token)
        out["steps_is_int"] = isinstance(goals.goals.steps, int)
        try:
            await client.getgoals("not-a-token")
        except TokenInvalid as error:
            out["bad_token"] = error.args[0]
    print(json.dumps(out))

asyncio.run(main())
`

// Plain urllib against the TLS side, trusting certifi's bundle (which holds the e2e CA in this
// stack) and the control side by its service name.
const WORKER_PROBES = `
import json, ssl, sys, urllib.request
import certifi
context = ssl.create_default_context(cafile=certifi.where())
out = {}
request = urllib.request.Request("https://wbsapi.withings.net/__e2e/state")
with urllib.request.urlopen(request, context=context, timeout=10) as reply:
    out["tls_synthetic"] = reply.headers.get("x-ownvitals-synthetic")
    out["tls_body_has_state"] = "request_count" in reply.read().decode()
with urllib.request.urlopen("http://fake-withings:9101/__e2e/ready", timeout=10) as reply:
    out["control_in_network"] = reply.status
print(json.dumps(out))
`

test('the worker reaches the fake as Withings and its calls are counted', () => {
  const world = resetWorld()
  const out = JSON.parse(
    pythonIn('worker', WORKER_CALLS, [world.client_id, world.client_secret, world.tokens.access_token]),
  ) as { steps_is_int: boolean; bad_token: string }
  expect(out.steps_is_int).toBe(true)
  expect(out.bad_token).toBeTruthy()

  const state = control<{
    request_count: number
    endpoint_counts: Record<string, number>
    log: { endpoint: string; api_status: number | null; params: Record<string, string> }[]
  }>('GET', '/__e2e/state')
  // Both calls are counted. Only this world's token can be answered 0, so the one success is
  // the spec's; the worker's calls, if any, are refused like the spec's bad token.
  const goals = state.log.filter((entry) => entry.endpoint === 'v2/user:getgoals')
  expect(goals.length).toBeGreaterThanOrEqual(2)
  expect(state.endpoint_counts['v2/user:getgoals']).toBe(goals.length)
  expect(goals.filter((entry) => entry.api_status === 0)).toHaveLength(1)
  expect(goals.filter((entry) => entry.api_status === 401).length).toBeGreaterThanOrEqual(1)
  // The log never holds a token or the app secret.
  expect(JSON.stringify(state)).not.toContain(world.tokens.access_token)
  expect(JSON.stringify(state)).not.toContain(world.client_secret)
})

// The failure is armed on every `getgoals` of this world, not on request 1: a worker call that
// reached the fake first took request 1, and the 601, and the spec's call succeeded (#403).
test('an injected failure reaches the worker as the client sees it', () => {
  const world = resetWorld()
  const every = Array.from({ length: 100 }, (_, i) => i + 1)
  control('POST', '/__e2e/inject', {
    kind: 'status',
    endpoint: 'v2/user:getgoals',
    at: every,
    status: 601,
    wait_seconds: 7,
  })
  const script = `
import asyncio, sys
from ownvitals import egress
from ownvitals.withings.client import WithingsApp, WithingsClient
from ownvitals.withings.errors import RateLimited

async def main():
    egress.install_worker_allowlist(ai_enabled=False)
    async with WithingsClient.open(WithingsApp(sys.argv[1], sys.argv[2])) as client:
        try:
            await client.getgoals(sys.argv[3])
        except RateLimited as error:
            print("rate_limited", int(error.wait_seconds))

asyncio.run(main())
`
  try {
    const out = pythonIn('worker', script, [
      world.client_id,
      world.client_secret,
      world.tokens.access_token,
    ])
    expect(out.trim()).toBe('rate_limited 7')
  } finally {
    resetWorld() // disarm it: no later call meets the 601
  }
})

test('the control endpoints are reachable inside the compose network only', async ({ page }) => {
  const probes = JSON.parse(pythonIn('worker', WORKER_PROBES)) as {
    tls_synthetic: string
    tls_body_has_state: boolean
    control_in_network: number
  }
  // Inside the network: the fake answers on the API names over TLS (the CA is trusted), the
  // control port answers by service name, and the TLS side does not serve /__e2e/.
  expect(probes.tls_synthetic).toBe('true')
  expect(probes.tls_body_has_state).toBe(false)
  expect(probes.control_in_network).toBe(200)

  // Outside: compose publishes nothing for the service, and the host cannot reach its ports.
  const ps = execFileSync(
    'docker',
    ['compose', '-f', 'compose.yaml', '-f', 'compose.e2e.yaml', 'ps', '--format', 'json', 'fake-withings'],
    { cwd: REPO_ROOT, encoding: 'utf8' },
  )
  // `compose ps --format json` prints one object per line, or one array, by version.
  type Row = { Publishers?: { PublishedPort: number }[] }
  const parsed = ps.trim().startsWith('[')
    ? (JSON.parse(ps) as Row[])
    : ps.trim().split('\n').map((line) => JSON.parse(line) as Row)
  const rows = parsed
  expect(rows.length).toBeGreaterThan(0)
  for (const row of rows) {
    expect((row.Publishers ?? []).filter((p) => p.PublishedPort > 0)).toEqual([])
  }
  // The browser: no route to the control port from the host.
  await expect(page.request.get('http://127.0.0.1:9101/__e2e/ready', { timeout: 5_000 })).rejects.toThrow()
})
