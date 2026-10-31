import { createElement, type ReactNode } from 'react'

// The QR code is drawn inline from the server's SVG: the CSP's `img-src` allows no `data:` URL.
// Only shapes and their geometry and paint are copied, as React elements: no markup is inserted,
// and nothing in it can load or run.
const QR_ELEMENTS = new Set(['g', 'path', 'rect'])
const QR_ATTRIBUTES = ['d', 'fill', 'stroke', 'transform', 'x', 'y', 'width', 'height'] as const

function qrShapes(parent: Element, prefix: string): ReactNode[] {
  return [...parent.children].flatMap((child, i) => {
    if (!QR_ELEMENTS.has(child.localName)) return []
    const props: Record<string, string> = {}
    for (const name of QR_ATTRIBUTES) {
      const value = child.getAttribute(name)
      if (value !== null) props[name] = value
    }
    const key = `${prefix}.${String(i)}`
    return [createElement(child.localName, { key, ...props }, ...qrShapes(child, key))]
  })
}

export function QrCode({ svg, label }: { svg: string; label: string }) {
  const root = new DOMParser().parseFromString(svg, 'image/svg+xml').documentElement
  if (root.localName !== 'svg') return null
  const width = root.getAttribute('width') ?? ''
  const height = root.getAttribute('height') ?? ''
  const box = /^\d+(\.\d+)?$/.test(width) && /^\d+(\.\d+)?$/.test(height) ? `0 0 ${width} ${height}` : undefined
  return (
    <svg className="qr" role="img" aria-label={label} viewBox={root.getAttribute('viewBox') ?? box}>
      {qrShapes(root, 'qr')}
    </svg>
  )
}
