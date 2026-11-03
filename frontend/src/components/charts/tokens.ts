import { useEffect, useState } from 'react'

// A canvas cannot read a stylesheet, so the chart reads the design tokens (--ov-*) from the
// document when it draws. There is no colour in this file: the values come from tokens.css, in
// whichever theme is on.

export type ChartColours = Record<
  'series' | 'band' | 'bandEdge' | 'grid' | 'axis' | 'text' | 'surface' | 'ground' | 'line' | 'attn' | 'font',
  string
>

const VARIABLES: Record<keyof ChartColours, string> = {
  series: '--ov-series',
  band: '--ov-band',
  bandEdge: '--ov-band-edge',
  grid: '--ov-grid',
  axis: '--ov-muted',
  text: '--ov-text',
  surface: '--ov-surface',
  ground: '--ov-bg',
  line: '--ov-line',
  attn: '--ov-attn',
  font: '--ov-font-sans',
}

export function readChartColours(root: Element = document.documentElement): ChartColours {
  const style = getComputedStyle(root)
  const read = (name: string) => style.getPropertyValue(name).trim()
  const colours = {} as ChartColours
  for (const key of Object.keys(VARIABLES) as (keyof ChartColours)[]) colours[key] = read(VARIABLES[key])
  return colours
}

/** A number that changes when the theme does (the attribute on <html>, or the device's choice). */
export function useThemeVersion(): number {
  const [version, setVersion] = useState(0)
  useEffect(() => {
    const bump = () => {
      setVersion((v) => v + 1)
    }
    const observer = new MutationObserver(bump)
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    const media = typeof window.matchMedia === 'function' ? window.matchMedia('(prefers-color-scheme: dark)') : null
    media?.addEventListener('change', bump)
    return () => {
      observer.disconnect()
      media?.removeEventListener('change', bump)
    }
  }, [])
  return version
}
