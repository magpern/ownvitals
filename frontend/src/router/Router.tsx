import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type AnchorHTMLAttributes,
  type MouseEvent,
  type ReactNode,
} from 'react'

// A tiny history-API router: the app has a handful of flat routes and no need for a library.

interface Location {
  pathname: string
  search: string
}

interface RouterValue extends Location {
  navigate: (to: string, options?: { replace?: boolean }) => void
}

const RouterContext = createContext<RouterValue | null>(null)

function readLocation(): Location {
  return { pathname: window.location.pathname, search: window.location.search }
}

export function RouterProvider({ children }: { children: ReactNode }) {
  const [location, setLocation] = useState<Location>(readLocation)

  useEffect(() => {
    const onPop = () => {
      setLocation(readLocation())
    }
    window.addEventListener('popstate', onPop)
    return () => {
      window.removeEventListener('popstate', onPop)
    }
  }, [])

  const navigate = useCallback((to: string, options?: { replace?: boolean }) => {
    if (options?.replace) window.history.replaceState(null, '', to)
    else window.history.pushState(null, '', to)
    setLocation(readLocation())
    window.scrollTo?.(0, 0)
  }, [])

  const value = useMemo(() => ({ ...location, navigate }), [location, navigate])
  return <RouterContext.Provider value={value}>{children}</RouterContext.Provider>
}

export function useRouter(): RouterValue {
  const value = useContext(RouterContext)
  if (value === null) throw new Error('useRouter needs a RouterProvider')
  return value
}

export function useQuery(): URLSearchParams {
  const { search } = useRouter()
  return useMemo(() => new URLSearchParams(search), [search])
}

interface LinkProps extends Omit<AnchorHTMLAttributes<HTMLAnchorElement>, 'href'> {
  to: string
}

export function Link({ to, onClick, children, ...rest }: LinkProps) {
  const { navigate } = useRouter()
  const handle = (event: MouseEvent<HTMLAnchorElement>) => {
    onClick?.(event)
    if (
      event.defaultPrevented ||
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    ) {
      return
    }
    event.preventDefault()
    navigate(to)
  }
  return (
    <a {...rest} href={to} onClick={handle}>
      {children}
    </a>
  )
}

/** `return_to` is honoured only as a relative path. */
export function safeReturnTo(value: string | null): string | null {
  if (value === null || !isPlainPath(value)) return null
  // Also refuse what a decoder further along would turn into `//host` or a backslash.
  let decoded: string
  try {
    decoded = decodeURIComponent(value)
  } catch {
    return null
  }
  return isPlainPath(decoded) ? value : null
}

function isPlainPath(value: string): boolean {
  if (!value.startsWith('/') || value.startsWith('//')) return false
  if (value.includes('\\') || value.includes('://')) return false
  for (const char of value) {
    if (char.charCodeAt(0) < 0x20 || char.charCodeAt(0) === 0x7f) return false
  }
  return true
}

/**
 * The `token` of a mail link (`/reset#token=...`). Reading is pure; `stripFragment` removes the
 * token from the address bar and history so it cannot leak through screenshots or sharing.
 */
export function readFragmentToken(): string | null {
  const params = new URLSearchParams(window.location.hash.replace(/^#/, ''))
  return params.get('token')
}

export function stripFragment(): void {
  if (window.location.hash !== '') {
    window.history.replaceState(null, '', window.location.pathname + window.location.search)
  }
}

/** For mail-link pages: the token from the fragment, removed from the URL on mount. */
export function useMailToken(): string | null {
  const [token, setToken] = useState(readFragmentToken)
  useEffect(() => {
    stripFragment()
    // The same tab may be pointed at a mail link without a reload (a fragment-only change).
    const onHashChange = () => {
      const next = readFragmentToken()
      if (next !== null) setToken(next)
      stripFragment()
    }
    window.addEventListener('hashchange', onHashChange)
    return () => {
      window.removeEventListener('hashchange', onHashChange)
    }
  }, [])
  return token
}
