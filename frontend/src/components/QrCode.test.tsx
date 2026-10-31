import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { QrCode } from './QrCode'

const draw = (svg: string) => {
  render(<QrCode svg={svg} label="QR" />)
  return screen.getByRole('img', { name: 'QR' })
}

describe('the QR code allow-list', () => {
  it('copies only g, path and rect, with only their geometry and paint', () => {
    const qr = draw(
      '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"><g transform="scale(2)" fill="#000">' +
        '<path d="M0 0h1v1z" stroke="#111" transform="translate(1 1)"/><rect x="1" y="2" width="3" height="4" fill="#fff"/></g></svg>',
    )
    expect(qr.querySelector('g')?.getAttribute('transform')).toBe('scale(2)')
    expect(qr.querySelector('g')?.getAttribute('fill')).toBe('#000')
    const path = qr.querySelector('path')
    expect(['d', 'stroke', 'transform'].map((n) => path?.getAttribute(n))).toEqual(['M0 0h1v1z', '#111', 'translate(1 1)'])
    const rect = qr.querySelector('rect')
    expect(['x', 'y', 'width', 'height', 'fill'].map((n) => rect?.getAttribute(n))).toEqual(['1', '2', '3', '4', '#fff'])
  })

  it('drops every other attribute, whatever the element', () => {
    const qr = draw(
      '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="9" height="9" onload="x()" style="fill:red" class="c" id="i">' +
        '<g onclick="x()" style="x:y" class="c" id="g" data-x="1"><path d="M0 0" onmouseover="x()" style="a:b" class="c" id="p" href="https://evil.example" xlink:href="https://evil.example" data-x="1"/></g></svg>',
    )
    for (const element of [qr, ...qr.querySelectorAll('*')]) {
      const names = [...element.attributes].map((a) => a.name).filter((n) => !(element === qr && n === 'class')) // the root's own class
      expect(names.filter((n) => /^(on|style|class|id|href|xlink|data)/.test(n))).toEqual([])
    }
  })

  it('drops other elements with everything inside them', () => {
    const qr = draw(
      '<svg xmlns="http://www.w3.org/2000/svg" width="9" height="9"><script>x()</script><style>*{}</style><image href="https://evil.example/x.png"/>' +
        '<foreignObject><div/></foreignObject><use href="#a"/><a href="https://evil.example"><path d="M1 1"/></a><g><animate/><path d="M2 2"/></g></svg>',
    )
    expect([...qr.querySelectorAll('*')].map((e) => e.localName)).toEqual(['g', 'path'])
    expect(qr.querySelector('path')?.getAttribute('d')).toBe('M2 2')
  })

  it('draws nothing for a document that is not an svg, and falls back to a box from width and height only when they are plain numbers', () => {
    const { container } = render(<QrCode svg="<html/>" label="QR" />)
    expect(container.querySelector('svg')).toBeNull()
    expect(draw('<svg xmlns="http://www.w3.org/2000/svg" width="12" height="8"/>').getAttribute('viewBox')).toBe('0 0 12 8')
  })

  it('does not take a width or height that is not a plain number for the viewBox', () => {
    const { container } = render(
      <QrCode svg={'<svg xmlns="http://www.w3.org/2000/svg" width="1e9px" height="javascript:1"/>'} label="QR" />,
    )
    expect(container.querySelector('svg')?.hasAttribute('viewBox')).toBe(false)
  })
})
