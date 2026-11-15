import { useLayoutEffect, useRef, type ReactNode } from 'react'

interface BlockProps {
  /** Any CSS length, taken from the content it stands in for, so nothing shifts when it loads. */
  width?: string
  height?: string
}

/** A flat block with one slow shimmer (static under reduced motion). Hidden from screen readers. */
export function Skeleton({ width, height }: BlockProps) {
  const ref = useRef<HTMLSpanElement>(null)
  useLayoutEffect(() => {
    const style = ref.current?.style
    if (style === undefined) return
    if (width !== undefined) style.setProperty('--ov-skel-w', width)
    if (height !== undefined) style.setProperty('--ov-skel-h', height)
  }, [width, height])
  return <span className="ov-skeleton" ref={ref} aria-hidden="true" />
}

interface SwapProps {
  loading: boolean
  /** Shown while loading; give it the size of `children`. */
  skeleton: ReactNode
  children: ReactNode
}

/** The skeleton while `loading`, then the content, which cross-fades in. */
export function SkeletonSwap({ loading, skeleton, children }: SwapProps) {
  if (loading) {
    return (
      <div aria-busy="true">
        {skeleton}
      </div>
    )
  }
  return <div className="ov-swap-in">{children}</div>
}
