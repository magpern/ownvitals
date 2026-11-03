import { execFileSync } from 'node:child_process'
import { REPO_ROOT } from './env.ts'

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
