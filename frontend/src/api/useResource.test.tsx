import { act, render, renderHook, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { ResourceView } from '../components/ui/DataState'
import { fakeApi, json, reason, SIGNED_IN } from '../test/fakeApi'
import { DecodeError } from './decode'
import { classify, useResource } from './useResource'

// Fakes only: no network, synthetic payloads.

const decode = (raw: unknown) => {
  if (typeof raw !== 'object' || raw === null || !('n' in raw) || typeof raw.n !== 'number') throw new DecodeError('n')
  return raw.n
}

describe('useResource', () => {
  it('loads, then has the decoded data', async () => {
    fakeApi({ 'GET /api/x': { n: 7 } })
    const { result } = renderHook(() => useResource('/api/x', decode))
    expect(result.current.status).toBe('loading')
    await waitFor(() => { expect(result.current.status).toBe('ready') })
    expect(result.current.data).toBe(7)
  })

  it('reads nothing for a null path', () => {
    const api = fakeApi({})
    const { result } = renderHook(() => useResource(null, decode))
    expect(result.current.status).toBe('ready')
    expect(api.calls).toHaveLength(0)
  })

  it('reports a failed decode as an error of its own kind, not as data', async () => {
    fakeApi({ 'GET /api/x': { n: 'seven' } })
    const { result } = renderHook(() => useResource('/api/x', decode))
    await waitFor(() => { expect(result.current.status).toBe('error') })
    expect(result.current.error?.kind).toBe('invalid')
    expect(result.current.data).toBeNull()
  })

  it('does not show one path’s data for another', async () => {
    fakeApi({ 'GET /api/a': { n: 1 }, 'GET /api/b': { n: 2 } })
    const { result, rerender } = renderHook(({ path }) => useResource(path, decode), { initialProps: { path: '/api/a' } })
    await waitFor(() => { expect(result.current.data).toBe(1) })
    rerender({ path: '/api/b' })
    expect(result.current.data).toBeNull()
    expect(result.current.status).toBe('loading')
    await waitFor(() => { expect(result.current.data).toBe(2) })
  })

  it('keeps the last good data while it reloads', async () => {
    let n = 1
    fakeApi({ 'GET /api/x': () => ({ n: n++ }) })
    const { result } = renderHook(() => useResource('/api/x', decode))
    await waitFor(() => { expect(result.current.data).toBe(1) })
    act(() => { result.current.reload() })
    expect(result.current.status).toBe('loading')
    expect(result.current.data).toBe(1)
    await waitFor(() => { expect(result.current.data).toBe(2) })
  })

  it.each([
    [403, 'step_up_required', 'step-up'],
    [403, 'reauth_required', 'reauth'],
    [403, 'nope', 'forbidden'],
    [404, 'not_found', 'not-found'],
    [429, 'slow_down', 'rate-limited'],
    [500, 'boom', 'failed'],
  ])('classifies %i %s as %s', async (status, why, kind) => {
    fakeApi({ 'GET /api/x': () => reason(status, why) })
    const { result } = renderHook(() => useResource('/api/x', decode))
    await waitFor(() => { expect(result.current.status).toBe('error') })
    expect(result.current.error?.kind).toBe(kind)
    expect(result.current.error?.message).not.toBe('')
  })

  it('runs a read that needs a fresh code through the step-up dialog, and tries again after it', async () => {
    let proven = false
    fakeApi({ 'GET /api/x': () => (proven ? { n: 5 } : reason(403, 'step_up_required')) })
    const purposes: string[] = []
    const protect = async <R,>(purpose: string, action: () => Promise<R>): Promise<R> => {
      purposes.push(purpose)
      try {
        return await action()
      } catch {
        proven = true
        return action()
      }
    }
    const { result } = renderHook(() => useResource('/api/x', decode, { protect, purpose: 'Reading it' }))
    await waitFor(() => { expect(result.current.data).toBe(5) })
    expect(purposes).toEqual(['Reading it'])
  })

  it('tells the session when a 401 survives the client’s own retry', async () => {
    fakeApi({ 'GET /api/x': () => reason(401, 'not_authenticated'), 'GET /api/auth/session': SIGNED_IN })
    const onUnauthenticated = vi.fn()
    const { result } = renderHook(() => useResource('/api/x', decode, { onUnauthenticated }))
    await waitFor(() => { expect(result.current.error?.kind).toBe('unauthenticated') })
    expect(onUnauthenticated).toHaveBeenCalledTimes(1)
  })

  it('does not set state after it was unmounted', async () => {
    fakeApi({ 'GET /api/x': () => json({ n: 1 }) })
    const { unmount } = renderHook(() => useResource('/api/x', decode))
    unmount()
    await act(async () => { await Promise.resolve() })
  })
})

describe('classify', () => {
  it('knows a refused connection', () => {
    expect(classify(new TypeError('failed')).kind).toBe('network')
  })
})

describe('ResourceView', () => {
  function Probe({ path }: { path: string }) {
    const resource = useResource(path, decode)
    return <ResourceView resource={resource}>{(n) => <p>Value {n}</p>}</ResourceView>
  }

  it('shows a loading line, then the data', async () => {
    fakeApi({ 'GET /api/x': { n: 3 } })
    render(<Probe path="/api/x" />)
    expect(screen.getByRole('status')).toHaveTextContent('Loading')
    expect(await screen.findByText('Value 3')).toBeInTheDocument()
  })

  it('says why it failed and offers to try again', async () => {
    let fail = true
    fakeApi({ 'GET /api/x': () => (fail ? reason(500, 'boom') : { n: 4 }) })
    render(<Probe path="/api/x" />)
    expect(await screen.findByRole('alert')).toHaveTextContent('Something went wrong on the server')
    fail = false
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByText('Value 4')).toBeInTheDocument()
  })
})
