import { afterEach, describe, expect, it, vi } from 'vitest'
import config, { apiProxyTarget } from '../vite.config'

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('apiProxyTarget', () => {
  it('defaults to the local backend', () => {
    expect(apiProxyTarget(undefined)).toBe('http://localhost:8000')
    expect(apiProxyTarget('')).toBe('http://localhost:8000')
  })

  it('takes an http(s) address', () => {
    expect(apiProxyTarget('http://backend:9000')).toBe('http://backend:9000')
    expect(apiProxyTarget('https://api.example.test')).toBe('https://api.example.test')
  })

  it.each(['backend:9000', 'localhost', 'ftp://backend', 'javascript:alert(1)', 'file:///etc/passwd'])(
    'refuses %s',
    (value) => {
      expect(() => apiProxyTarget(value)).toThrow(/API_PROXY_TARGET/)
    },
  )
})

describe('the Vite configuration', () => {
  function resolved(mode = 'development') {
    if (typeof config !== 'function') throw new Error('the config is a function of the mode')
    return config({ command: 'serve', mode }) as {
      build: { assetsInlineLimit: number }
      server: { proxy: Record<string, { target: string }> }
    }
  }

  it('forwards /api to the local backend by default', () => {
    vi.stubEnv('API_PROXY_TARGET', '')
    expect(resolved().server.proxy['/api']?.target).toBe('http://localhost:8000')
  })

  it('forwards /api to API_PROXY_TARGET when it is set', () => {
    vi.stubEnv('API_PROXY_TARGET', 'http://backend:9000')
    expect(resolved().server.proxy['/api']?.target).toBe('http://backend:9000')
  })

  it('forwards nothing else', () => {
    expect(Object.keys(resolved().server.proxy)).toEqual(['/api'])
  })

  it('never inlines an asset as a data: URL (the CSP allows none)', () => {
    expect(resolved('production').build.assetsInlineLimit).toBe(0)
  })
})
