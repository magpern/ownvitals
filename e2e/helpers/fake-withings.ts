import { execFileSync } from 'node:child_process'
import type { Page } from '@playwright/test'
import { BASE_URL, REPO_ROOT } from './env.ts'

const COMPOSE = ['compose', '-f', 'compose.yaml', '-f', 'compose.e2e.yaml']

/** Runs `python -` with `script` on stdin inside a service container; returns its stdout. */
export function pythonIn(service: string, script: string, args: string[] = []): string {
  return execFileSync('docker', [...COMPOSE, 'exec', '-T', service, 'python', '-', ...args], {
    cwd: REPO_ROOT,
    encoding: 'utf8',
    input: script,
    stdio: ['pipe', 'pipe', 'pipe'],
  })
}

const CONTROL = `
import json, sys, urllib.request
method, path, body = sys.argv[1], sys.argv[2], sys.argv[3]
request = urllib.request.Request(
    "http://127.0.0.1:9101" + path, method=method,
    data=body.encode() if method == "POST" else None,
)
with urllib.request.urlopen(request, timeout=10) as reply:
    sys.stdout.write(reply.read().decode())
`

/**
 * One call to fake-withings' control endpoint (/__e2e/..., plain http on 9101). The service
 * publishes no port, so the call is made from inside its own container.
 */
export function control<T = unknown>(method: 'GET' | 'POST', path: string, body: object = {}): T {
  const out = pythonIn('fake-withings', CONTROL, [method, path, JSON.stringify(body)])
  return JSON.parse(out) as T
}

export interface World {
  client_id: string
  client_secret: string
  userid: number
  tokens: { access_token: string; refresh_token: string }
}

/** A fresh synthetic world: one app, one user, one token pair. */
export const resetWorld = (): World => control<World>('POST', '/__e2e/reset')

/**
 * The app's callback base: an https origin, as `withings-apps create` requires. The fake's
 * redirect URI (compose.e2e.yaml) is under it; the stack itself answers plain http.
 */
export const CALLBACK_BASE = BASE_URL.replace(/^http:/, 'https:')

/**
 * Stores the fake's app (its client id and the current world's client secret) as the newest
 * Withings app, the operator's `withings-apps create` (owner role, in a one-off migrate container,
 * the secret on stdin). Run it after `resetWorld()`: a reset gives the fake a new secret.
 */
export function registerApp(world: World): void {
  execFileSync(
    'docker',
    [
      ...COMPOSE, 'run', '--rm', '-T', '--no-deps', 'migrate',
      'python', '-m', 'ownvitals', 'withings-apps', 'create',
      '--client-id', world.client_id, '--callback-base', CALLBACK_BASE, '--secret-file', '/dev/stdin',
    ],
    { cwd: REPO_ROOT, encoding: 'utf8', input: world.client_secret, stdio: ['pipe', 'pipe', 'pipe'] },
  )
}

/**
 * The browser's visit to the Withings authorize page never leaves the machine: the spec intercepts
 * it and hands the URL to fake-withings (`POST /__e2e/authorize`), whose synthetic user consents
 * (or, with `deny`, refuses). The fake's redirect goes to the https callback base; the browser is
 * sent to the same path and query on the stack's own http origin.
 */
export async function answerWithings(page: Page, options: { deny?: boolean } = {}): Promise<void> {
  await page.route('https://account.withings.com/**', async (route) => {
    const answer = control<{ status: number; location: string | null }>('POST', '/__e2e/authorize', {
      url: route.request().url(),
      deny: options.deny === true,
    })
    const back = answer.status === 302 && answer.location !== null ? new URL(answer.location) : null
    if (back === null || back.origin !== CALLBACK_BASE) {
      await route.abort()
      throw new Error(`fake-withings refused the authorize request (${String(answer.status)})`)
    }
    await route.fulfill({ status: 302, headers: { location: new URL(back.pathname + back.search, BASE_URL).toString() } })
  })
}
