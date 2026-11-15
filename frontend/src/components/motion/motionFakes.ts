import { vi } from 'vitest'

/** Test fakes for the browser pieces the motion primitives read. */

export function fakeMatchMedia(reduced: boolean) {
  const listeners = new Set<() => void>()
  let current = reduced
  vi.stubGlobal(
    'matchMedia',
    (query: string) => ({
      get matches() {
        return query.includes('prefers-reduced-motion') && current
      },
      media: query,
      addEventListener: (_: string, fn: () => void) => listeners.add(fn),
      removeEventListener: (_: string, fn: () => void) => listeners.delete(fn),
    }),
  )
  window.matchMedia = globalThis.matchMedia
  return {
    set(next: boolean) {
      current = next
      listeners.forEach((fn) => fn())
    },
  }
}

/** A manual requestAnimationFrame: `step(ms)` advances the fake clock and runs the callbacks. */
export function fakeFrames() {
  let now = 0
  let next = 1
  const pending = new Map<number, FrameRequestCallback>()
  vi.spyOn(performance, 'now').mockImplementation(() => now)
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
    pending.set(next, cb)
    return next++
  })
  vi.stubGlobal('cancelAnimationFrame', (id: number) => pending.delete(id))
  return {
    pending: () => pending.size,
    step(ms: number) {
      now += ms
      const run = [...pending]
      pending.clear()
      run.forEach(([, cb]) => cb(now))
    },
  }
}

/** An IntersectionObserver that reports nothing until `show()`. */
export function fakeIntersection() {
  const observers = new Set<{ cb: IntersectionObserverCallback; el: Element }>()
  class Fake {
    cb: IntersectionObserverCallback
    constructor(cb: IntersectionObserverCallback) {
      this.cb = cb
    }
    observe(el: Element) {
      observers.add({ cb: this.cb, el })
    }
    disconnect() {
      observers.forEach((o) => o.cb === this.cb && observers.delete(o))
    }
    unobserve() {}
    takeRecords() {
      return []
    }
  }
  vi.stubGlobal('IntersectionObserver', Fake)
  return {
    show() {
      observers.forEach((o) =>
        o.cb([{ isIntersecting: true, target: o.el } as IntersectionObserverEntry], {} as IntersectionObserver),
      )
    },
  }
}
