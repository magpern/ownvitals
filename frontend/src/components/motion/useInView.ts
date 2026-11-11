import { useEffect, useState, type RefObject } from 'react'

/**
 * Whether the element has been on screen (sticky: once true it stays true, so an animation runs
 * once). Without IntersectionObserver (old browsers, jsdom) it is true at once. Nothing animates
 * off-screen (motion.md rule 3).
 */
export function useInView(ref: RefObject<Element | null>): boolean {
  const supported = typeof IntersectionObserver === 'function'
  const [seen, setSeen] = useState(!supported)
  useEffect(() => {
    const element = ref.current
    if (seen || element === null) return
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) setSeen(true)
    })
    observer.observe(element)
    return () => observer.disconnect()
  }, [ref, seen])
  return seen
}
