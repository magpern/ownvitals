import { useSyncExternalStore } from 'react'

// True when motion must be off: the device asks for reduced motion, or the Settings override
// `data-motion="off"` is on <html> (docs/design/motion.md, rule 1). Every JS animation calls this.

const QUERY = '(prefers-reduced-motion: reduce)'

function overrideIsOff(): boolean {
  return typeof document !== 'undefined' && document.documentElement.getAttribute('data-motion') === 'off'
}

function deviceWantsReduced(): boolean {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function' && window.matchMedia(QUERY).matches
}

export function motionIsReduced(): boolean {
  return overrideIsOff() || deviceWantsReduced()
}

function subscribe(onChange: () => void): () => void {
  const query = typeof window !== 'undefined' && typeof window.matchMedia === 'function' ? window.matchMedia(QUERY) : null
  query?.addEventListener('change', onChange)
  const observer = typeof MutationObserver === 'function' ? new MutationObserver(onChange) : null
  observer?.observe(document.documentElement, { attributes: true, attributeFilter: ['data-motion'] })
  return () => {
    query?.removeEventListener('change', onChange)
    observer?.disconnect()
  }
}

/** Follows the media query and the `data-motion` attribute; the server snapshot is "reduced". */
export function useReducedMotion(): boolean {
  return useSyncExternalStore(subscribe, motionIsReduced, () => true)
}
