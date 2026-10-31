import { describe, expect, it, vi } from 'vitest'
import { createNetworkGuard, networkGuard } from './networkGuard'

// The guard that src/test/setup.ts installs in every test file. Most of this runs on a stand-in
// for the global object, so a refusal here never reaches the real hooks; the last block checks
// the real one.

type Fetch = (input: unknown, init?: unknown) => Promise<unknown>
type Construct = new (url?: string) => unknown

function stand(extra: Record<string, unknown> = {}) {
  const real = vi.fn(() => Promise.resolve('the real fetch'))
  return {
    real,
    target: {
      fetch: real,
      XMLHttpRequest: class {},
      WebSocket: class {},
      ...extra,
    } as Record<string, unknown>,
  }
}

describe('the network guard on a stand-in global', () => {
  it('refuses fetch, and reports where it was going', async () => {
    const { target, real } = stand()
    const guard = createNetworkGuard(target)
    guard.install()
    await expect((target.fetch as Fetch)('/api/auth/session')).rejects.toThrow(/no network.*stub fetch/)
    await expect((target.fetch as Fetch)(new URL('https://example.test/a'), { method: 'post' })).rejects.toThrow()
    await expect((target.fetch as Fetch)(new Request('https://example.test/b', { method: 'DELETE' }))).rejects.toThrow()
    expect(guard.drain()).toEqual(['GET /api/auth/session', 'POST https://example.test/a', 'DELETE https://example.test/b'])
    expect(real).not.toHaveBeenCalled()
  })

  it('keeps credentials, query and fragment out of what it reports', async () => {
    const { target } = stand()
    const guard = createNetworkGuard(target)
    guard.install()
    await expect((target.fetch as Fetch)('https://ann:hunter2@example.test/p?token=abc123#frag')).rejects.toThrow()
    await expect((target.fetch as Fetch)('//ann:hunter2@example.test/q?token=abc123')).rejects.toThrow()
    const reported = guard.drain().join('\n')
    expect(reported).toBe('GET https://example.test/p\nGET //example.test/q')
  })

  it('refuses XMLHttpRequest and WebSocket when they are created', () => {
    const { target } = stand()
    const guard = createNetworkGuard(target)
    guard.install()
    expect(() => new (target.XMLHttpRequest as Construct)()).toThrow(/no network/)
    expect(() => new (target.WebSocket as Construct)('wss://example.test/live?key=abc123')).toThrow(/no network/)
    expect(guard.drain()).toEqual(['new XMLHttpRequest()', 'new WebSocket wss://example.test/live'])
  })

  it('empties its list when it is drained', async () => {
    const { target } = stand()
    const guard = createNetworkGuard(target)
    guard.install()
    expect(guard.drain()).toEqual([])
    await expect((target.fetch as Fetch)('/x')).rejects.toThrow()
    expect(guard.drain()).toEqual(['GET /x'])
    expect(guard.drain()).toEqual([])
  })

  it('records the attempt even when the caller swallows the refusal', async () => {
    const { target } = stand()
    const guard = createNetworkGuard(target)
    guard.install()
    const result = await (target.fetch as Fetch)('/api/auth/session').catch(() => 'recovered')
    expect(result).toBe('recovered')
    expect(guard.drain()).toEqual(['GET /api/auth/session'])
  })

  it('leaves a stub installed after it alone', async () => {
    const { target } = stand()
    const guard = createNetworkGuard(target)
    guard.install()
    const stub = vi.fn(() => Promise.resolve('stubbed'))
    target.fetch = stub
    await expect((target.fetch as Fetch)('/x')).resolves.toBe('stubbed')
    expect(stub).toHaveBeenCalledTimes(1)
    expect(guard.drain()).toEqual([])
  })

  it('installs once, and only replaces what the environment has', async () => {
    const node = { fetch: vi.fn(() => Promise.resolve('real')) } as Record<string, unknown>
    const guard = createNetworkGuard(node)
    guard.install()
    guard.install()
    expect(Object.keys(node)).toEqual(['fetch'])
    await expect((node.fetch as Fetch)('/x')).rejects.toThrow()
    expect(guard.drain()).toEqual(['GET /x'])
  })
})

describe('the network guard of this test file', () => {
  // setup.ts installed it before this file ran; its afterEach fails any test that leaves an
  // attempt behind, so each test here drains what it provokes.
  it('refuses a fetch nobody stubbed', async () => {
    await expect(fetch('/api/auth/session')).rejects.toThrow(/no network/)
    expect(networkGuard.drain()).toEqual(['GET /api/auth/session'])
  })

  it('refuses XMLHttpRequest and WebSocket', () => {
    expect(() => new XMLHttpRequest()).toThrow(/no network/)
    expect(() => new WebSocket('wss://example.test/live')).toThrow(/no network/)
    expect(networkGuard.drain()).toEqual(['new XMLHttpRequest()', 'new WebSocket wss://example.test/live'])
  })

  it('steps aside for a stub, and is back in the next test', async () => {
    vi.stubGlobal('fetch', () => Promise.resolve(new Response('ok')))
    await expect(fetch('/x').then((response) => response.text())).resolves.toBe('ok')
    expect(networkGuard.drain()).toEqual([])
  })

  it('is back after a stub (unstubGlobals)', async () => {
    await expect(fetch('/x')).rejects.toThrow(/no network/)
    expect(networkGuard.drain()).toEqual(['GET /x'])
  })
})
