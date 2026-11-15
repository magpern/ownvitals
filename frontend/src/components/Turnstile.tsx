import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import type { TurnstileConfig } from '../api/auth'
import { useSession } from '../auth/SessionContext'

// Cloudflare Turnstile on sign-up, sign-in and the reset request (ADR 0018). Only while the
// session says it is on: then the page loads the widget's script (its URL comes from the server,
// and the CSP admits only that origin), renders the widget explicitly with the form's action, and
// sends its token with the form. A token is single-use, so the form resets the widget after
// every submission. While it is off nothing is loaded and the forms are unchanged.

export type TurnstileAction = 'signup' | 'signin' | 'reset'

export interface RenderOptions {
  sitekey: string
  action: TurnstileAction
  callback: (token: string) => void
  'expired-callback': () => void
  'error-callback': () => void
}

export interface TurnstileApi {
  render: (element: HTMLElement, options: RenderOptions) => string
  reset: (widgetId: string) => void
  remove: (widgetId: string) => void
}

declare global {
  interface Window {
    turnstile?: TurnstileApi
  }
}

let loading: Promise<TurnstileApi> | null = null

/** Loads the widget's script once per page; a failed load can be tried again. */
export function loadTurnstile(scriptUrl: string): Promise<TurnstileApi> {
  if (window.turnstile) return Promise.resolve(window.turnstile)
  if (loading) return loading
  loading = new Promise<TurnstileApi>((resolve, reject) => {
    const script = document.createElement('script')
    script.src = scriptUrl
    script.async = true
    script.onload = () => {
      if (window.turnstile) resolve(window.turnstile)
      else reject(new Error('turnstile unavailable'))
    }
    script.onerror = () => {
      loading = null
      script.remove()
      reject(new Error('turnstile unavailable'))
    }
    document.head.appendChild(script)
  })
  return loading
}

/** For tests: forget the script load. */
export function forgetTurnstile(): void {
  loading = null
}

export interface TurnstileState {
  /** Turnstile is on: the form waits for a token. */
  required: boolean
  token: string | null
  /** Call after each submission: the token was used. */
  reset: () => void
  /** The widget's place in the form, or null while Turnstile is off. */
  widget: ReactNode
}

export function useTurnstile(action: TurnstileAction): TurnstileState {
  const { session } = useSession()
  const config: TurnstileConfig | null = session?.turnstile ?? null
  const siteKey = config?.siteKey ?? null
  const scriptUrl = config?.scriptUrl ?? null
  const [token, setToken] = useState<string | null>(null)
  const box = useRef<HTMLDivElement | null>(null)
  const widgetId = useRef<string | null>(null)

  useEffect(() => {
    if (siteKey === null || scriptUrl === null) return
    let cancelled = false
    loadTurnstile(scriptUrl)
      .then((api) => {
        if (cancelled || box.current === null) return
        widgetId.current = api.render(box.current, {
          sitekey: siteKey,
          action,
          callback: (value) => {
            setToken(value)
          },
          'expired-callback': () => {
            setToken(null)
          },
          'error-callback': () => {
            setToken(null)
          },
        })
      })
      .catch(() => {
        setToken(null)
      })
    return () => {
      cancelled = true
      if (widgetId.current !== null) window.turnstile?.remove(widgetId.current)
      widgetId.current = null
    }
  }, [siteKey, scriptUrl, action])

  const reset = useCallback(() => {
    setToken(null)
    if (widgetId.current !== null) window.turnstile?.reset(widgetId.current)
  }, [])

  return {
    required: siteKey !== null,
    token,
    reset,
    widget: siteKey !== null ? <div className="ov-turnstile" ref={box} /> : null,
  }
}
