// The default network guard of the test suite (AGENTS.md: tests never touch the network or a real
// account). src/test/setup.ts installs it in every test file. `fetch`, `XMLHttpRequest` and
// `WebSocket` then refuse to go anywhere, and the test that tried fails in its `afterEach`, even
// when the code under test catches the refusal and carries on. A test that needs `fetch` stubs it
// (`fakeApi`, or `vi.stubGlobal`); the stub replaces the guard for that test and the guard is
// back for the next one (`unstubGlobals`).

type Target = Record<string, unknown>

export interface NetworkGuard {
  /** Replaces what `target` has of `fetch`, `XMLHttpRequest` and `WebSocket`. Does nothing twice. */
  install(): void
  /** What the code tried to reach since the last call, as `METHOD url` lines. Empties the list. */
  drain(): string[]
}

const REFUSAL = 'the test suite has no network: stub fetch (see fakeApi in src/test/fakeApi.ts)'

/** `url` as it is worth printing: no credentials, query or fragment (they may hold a secret). */
function printable(url: string): string {
  return url.replace(/^((?:[a-z][a-z0-9+.-]*:)?\/\/)[^/?#]*@/i, '$1').split(/[?#]/, 1)[0] ?? ''
}

function urlOf(input: unknown): string {
  if (typeof input === 'string') return input
  if (input instanceof URL) return input.href
  if (typeof Request !== 'undefined' && input instanceof Request) return input.url
  return String(input)
}

function methodOf(input: unknown, init: unknown): string {
  const own = (init as { method?: unknown } | undefined)?.method
  if (typeof own === 'string') return own.toUpperCase()
  if (typeof Request !== 'undefined' && input instanceof Request) return input.method.toUpperCase()
  return 'GET'
}

/** A guard for `target`, the global object unless a test of the guard itself passes another. */
export function createNetworkGuard(target: Target = globalThis): NetworkGuard {
  const attempts: string[] = []
  let installed = false

  const refuse = (what: string): Error => {
    attempts.push(what)
    return new Error(`${what}: ${REFUSAL}`)
  }

  return {
    install() {
      if (installed) return
      installed = true
      if (typeof target.fetch === 'function') {
        target.fetch = (input: unknown, init?: unknown) =>
          Promise.reject(refuse(`${methodOf(input, init)} ${printable(urlOf(input))}`))
      }
      // The classes are only replaced where the environment has them (Node has no XMLHttpRequest).
      if (typeof target.XMLHttpRequest === 'function') {
        target.XMLHttpRequest = class {
          constructor() {
            throw refuse('new XMLHttpRequest()')
          }
        }
      }
      if (typeof target.WebSocket === 'function') {
        target.WebSocket = class {
          constructor(url?: unknown) {
            throw refuse(`new WebSocket ${printable(urlOf(url))}`)
          }
        }
      }
    },
    drain() {
      return attempts.splice(0)
    },
  }
}

/** The guard of the running test file. */
export const networkGuard = createNetworkGuard()
