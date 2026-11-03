import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, get } from './client'
import { errorMessage } from './messages'

// The data layer of the screens: one typed read of one API path, with its loading and error
// states and the refusals a read can meet.
//
//   const weight = useResource('/api/metrics/weight?range=90d', decodeSeries)
//   weight.status   'loading' | 'ready' | 'error'
//   weight.data     what `decode` returned (the last good answer stays while a reload runs)
//   weight.error    what went wrong, in plain words, with a `kind` a screen can act on
//
// `decode` checks the JSON (src/api/decode.ts): nothing from the server is trusted because it
// parsed. A read that needs a fresh authenticator code (403 `step_up_required`, or
// `reauth_required`) is run through `protect` (the shared "Confirm it's you" dialog,
// `useProtected()`) when the screen passes it; without it the refusal is reported as an error of
// kind `step-up` or `reauth`. A 401 that survives the client's own retry means the session ended:
// `onUnauthenticated` (the session's `refresh`) lets the route guards move the user to sign-in.

export type ResourceErrorKind =
  | 'unauthenticated'
  | 'step-up'
  | 'reauth'
  | 'forbidden'
  | 'not-found'
  | 'rate-limited'
  | 'network'
  | 'invalid'
  | 'failed'

export interface ResourceError {
  kind: ResourceErrorKind
  /** Plain words for the person (from `messages.ts`). */
  message: string
  /** Seconds to wait, when the server said so. */
  retryAfter: number | null
}

export type ResourceStatus = 'loading' | 'ready' | 'error'

export interface Resource<T> {
  status: ResourceStatus
  data: T | null
  error: ResourceError | null
  /** Reads again; the last good data stays on screen meanwhile. */
  reload: () => void
}

export interface ResourceOptions {
  /** The shared step-up dialog: runs the read and asks for the proofs the server wants. */
  protect?: <R>(purpose: string, action: () => Promise<R>) => Promise<R>
  /** What the dialog says needs the proof ("Reading your export"). */
  purpose?: string
  onUnauthenticated?: () => void
}

export function classify(error: unknown): ResourceError {
  const message = errorMessage(error)
  if (error instanceof ApiError) {
    const base = { message, retryAfter: error.retryAfter }
    if (error.status === 401) return { ...base, kind: 'unauthenticated' }
    if (error.status === 403) {
      if (error.reason === 'step_up_required') return { ...base, kind: 'step-up' }
      if (error.reason === 'reauth_required') return { ...base, kind: 'reauth' }
      return { ...base, kind: 'forbidden' }
    }
    if (error.status === 404) return { ...base, kind: 'not-found' }
    if (error.status === 429) return { ...base, kind: 'rate-limited' }
    return { ...base, kind: 'failed' }
  }
  if (error instanceof TypeError) return { kind: 'network', message, retryAfter: null }
  if (error instanceof Error && error.name === 'DecodeError') return { kind: 'invalid', message: error.message, retryAfter: null }
  return { kind: 'failed', message, retryAfter: null }
}

/** Reads `path` (null reads nothing) and decodes it. The read follows `path`: a new path starts a new read. */
export function useResource<T>(path: string | null, decode: (raw: unknown) => T, options: ResourceOptions = {}): Resource<T> {
  // `path` is part of the state: data read from another path is never shown for this one.
  const [state, setState] = useState<{
    path: string | null
    status: ResourceStatus
    data: T | null
    error: ResourceError | null
  }>({ path, status: path === null ? 'ready' : 'loading', data: null, error: null })
  const [attempt, setAttempt] = useState(0)

  // The latest decode and options, so a caller's inline function does not restart the read.
  const latest = useRef({ decode, options })
  latest.current = { decode, options }

  useEffect(() => {
    if (path === null) return
    const controller = new AbortController()
    setState((previous) => ({ path, status: 'loading', data: previous.path === path ? previous.data : null, error: null }))
    const { decode: decodeNow, options: optionsNow } = latest.current
    const read = () => get(path, controller.signal)
    const run = optionsNow.protect === undefined ? read() : optionsNow.protect(optionsNow.purpose ?? 'Reading this', read)
    run.then(
      (raw) => {
        if (controller.signal.aborted) return
        try {
          setState({ path, status: 'ready', data: decodeNow(raw), error: null })
        } catch (error) {
          setState((previous) => ({ path, status: 'error', data: previous.path === path ? previous.data : null, error: classify(error) }))
        }
      },
      (error: unknown) => {
        if (controller.signal.aborted) return
        const failure = classify(error)
        if (failure.kind === 'unauthenticated') latest.current.options.onUnauthenticated?.()
        setState((previous) => ({ path, status: 'error', data: previous.path === path ? previous.data : null, error: failure }))
      },
    )
    return () => {
      controller.abort()
    }
  }, [path, attempt])

  const reload = useCallback(() => {
    setAttempt((n) => n + 1)
  }, [])

  const current = state.path === path
  return {
    status: current ? state.status : path === null ? 'ready' : 'loading',
    data: current ? state.data : null,
    error: current ? state.error : null,
    reload,
  }
}
