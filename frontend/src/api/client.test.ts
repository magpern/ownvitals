import { describe, expect, it, vi } from 'vitest'
import { fakeApi, json, reason } from '../test/fakeApi'
import { ApiError, del, get, post, setTwoFactorRequiredHandler } from './client'
import { errorMessage } from './messages'

describe('API client', () => {
  it('sends no CSRF header on GET', async () => {
    const api = fakeApi({ 'GET /api/x': { ok: true } })
    await get('/api/x')
    expect(api.calls[0]?.headers.get('X-CSRF-Token')).toBeNull()
  })

  it('fetches the CSRF token first and sends it on POST', async () => {
    const api = fakeApi({ 'POST /api/x': { ok: true } })
    await post('/api/x', { a: 1 })
    expect(api.calls.map((c) => `${c.method} ${c.path}`)).toEqual([
      'GET /api/auth/session',
      'POST /api/x',
    ])
    expect(api.calls[1]?.headers.get('X-CSRF-Token')).toBe('csrf-anon')
    expect(api.calls[1]?.body).toEqual({ a: 1 })
  })

  it('fetches a new token after a successful unsafe request (the session may have rotated)', async () => {
    const api = fakeApi({ 'POST /api/x': { ok: true } })
    await post('/api/x')
    await post('/api/x')
    expect(api.to('GET', '/api/auth/session')).toHaveLength(2)
  })

  it('retries once with a fresh token when the server blames the CSRF token', async () => {
    let tokens = 0
    let posts = 0
    const api = fakeApi({
      'GET /api/auth/session': () => ({ authenticated: false, csrf_token: `t${String(++tokens)}` }),
      'POST /api/x': (_body: unknown, headers: Headers) =>
        ++posts === 1 ? reason(403, 'csrf_token_invalid') : { seen: headers.get('X-CSRF-Token') },
    })
    const result = await post('/api/x')
    expect(result).toEqual({ seen: 't2' })
    expect(api.to('POST', '/api/x')).toHaveLength(2)
  })

  it('does not retry twice', async () => {
    const api = fakeApi({ 'POST /api/x': reason(403, 'csrf_token_invalid') })
    await expect(post('/api/x')).rejects.toMatchObject({ status: 403, reason: 'csrf_token_invalid' })
    expect(api.to('POST', '/api/x')).toHaveLength(2)
  })

  it('does not retry other 403 answers', async () => {
    const api = fakeApi({ 'POST /api/x': reason(403, 'reauth_required') })
    await expect(post('/api/x')).rejects.toMatchObject({ reason: 'reauth_required' })
    expect(api.to('POST', '/api/x')).toHaveLength(1)
  })

  it('throws ApiError with status, reason and Retry-After', async () => {
    fakeApi({ 'GET /api/x': json({ detail: { reason: 'rate_limited' } }, 429, { 'Retry-After': '120' }) })
    const error = await get('/api/x').catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({ status: 429, reason: 'rate_limited', retryAfter: 120 })
    expect(errorMessage(error)).toBe('Too many attempts. Try again in 2 minutes.')
  })

  describe('401 after the session rotated', () => {
    it('repeats a GET once after refetching the session', async () => {
      let gets = 0
      const api = fakeApi({
        'GET /api/x': () => (++gets === 1 ? reason(401, 'not_authenticated') : { ok: true }),
      })
      expect(await get('/api/x')).toEqual({ ok: true })
      expect(api.to('GET', '/api/x')).toHaveLength(2)
      expect(api.to('GET', '/api/auth/session')).toHaveLength(1)
    })

    it('gives up after one repeat', async () => {
      const api = fakeApi({ 'GET /api/x': reason(401, 'not_authenticated') })
      await expect(get('/api/x')).rejects.toMatchObject({ status: 401 })
      expect(api.to('GET', '/api/x')).toHaveLength(2)
    })

    it.each([
      ['/api/auth/mfa/stepup', 'not_authenticated'],
      ['/api/auth/reauth', 'not_authenticated'],
      ['/api/auth/login', 'invalid_credentials'],
    ])('sends a POST to %s exactly once when it gets 401 %s', async (path, why) => {
      const api = fakeApi({ [`POST ${path}`]: reason(401, why) })
      await expect(post(path)).rejects.toMatchObject({ status: 401, reason: why })
      expect(api.to('POST', path)).toHaveLength(1)
    })

    it('never repeats a DELETE or a bodyless 401 either', async () => {
      const api = fakeApi({ 'DELETE /api/x': json({}, 401) })
      await expect(del('/api/x')).rejects.toMatchObject({ status: 401 })
      expect(api.to('DELETE', '/api/x')).toHaveLength(1)
    })

    it('reports the original 401 when the session cannot be refetched', async () => {
      const api = fakeApi({
        'GET /api/x': reason(401, 'not_authenticated'),
        'GET /api/auth/session': reason(500, 'boom'),
      })
      await expect(get('/api/x')).rejects.toMatchObject({ status: 401, reason: 'not_authenticated' })
      expect(api.to('GET', '/api/x')).toHaveLength(1)
    })
  })

  it('returns undefined for an empty 204 answer', async () => {
    fakeApi({ 'POST /api/x': new Response(null, { status: 204 }) })
    expect(await post('/api/x')).toBeUndefined()
  })

  it('turns reasons into plain language and hides unknown codes', () => {
    expect(errorMessage(new ApiError(400, 'invalid_invite', null, null))).toMatch(/invite code/)
    expect(errorMessage(new ApiError(400, 'something_new', null, null))).not.toMatch(/something_new/)
    expect(errorMessage(new TypeError('Failed to fetch'))).toMatch(/could not be reached/)
  })

  describe('two_factor_required', () => {
    it('tells the registered handler, once per refusal, and still throws', async () => {
      const handler = vi.fn()
      const unregister = setTwoFactorRequiredHandler(handler)
      fakeApi({ 'GET /api/x': reason(403, 'two_factor_required') })
      await expect(get('/api/x')).rejects.toMatchObject({ status: 403, reason: 'two_factor_required' })
      expect(handler).toHaveBeenCalledTimes(1)
      unregister()
    })

    it('ignores every other refusal', async () => {
      const handler = vi.fn()
      const unregister = setTwoFactorRequiredHandler(handler)
      fakeApi({
        'GET /api/a': reason(403, 'forbidden'),
        'GET /api/b': reason(401, 'two_factor_required'),
        'GET /api/c': reason(500, 'two_factor_required'),
      })
      for (const path of ['/api/a', '/api/b', '/api/c']) await get(path).catch(() => undefined)
      expect(handler).not.toHaveBeenCalled()
      unregister()
    })

    it('stops telling a handler that was unregistered, and works with none', async () => {
      const handler = vi.fn()
      setTwoFactorRequiredHandler(handler)()
      fakeApi({ 'GET /api/x': reason(403, 'two_factor_required') })
      await expect(get('/api/x')).rejects.toMatchObject({ status: 403 })
      expect(handler).not.toHaveBeenCalled()
    })

    it('keeps a newer handler when an older one unregisters late', async () => {
      const older = vi.fn()
      const newer = vi.fn()
      const unregisterOlder = setTwoFactorRequiredHandler(older)
      const unregisterNewer = setTwoFactorRequiredHandler(newer)
      unregisterOlder()
      fakeApi({ 'GET /api/x': reason(403, 'two_factor_required') })
      await get('/api/x').catch(() => undefined)
      expect(newer).toHaveBeenCalledTimes(1)
      expect(older).not.toHaveBeenCalled()
      unregisterNewer()
    })
  })
})
