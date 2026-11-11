import { act, render, screen } from '@testing-library/react'
import { hydrateRoot } from 'react-dom/client'
import { renderToString } from 'react-dom/server'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { CountUp } from './CountUp'
import { Reveal } from './Reveal'
import { Skeleton, SkeletonSwap } from './Skeleton'
import { useReducedMotion } from './useReducedMotion'
import { fakeFrames, fakeIntersection, fakeMatchMedia } from './motionFakes'

// Synthetic numbers only. jsdom has no layout, so "no layout shift" is tested as: the final text
// is the sizer from the first render, only opacity/transform classes change, and the DOM keeps
// its shape across the whole animation.

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  document.documentElement.removeAttribute('data-motion')
})

function live(container: HTMLElement) {
  return container.querySelector('.ov-countup__live') as HTMLElement
}

describe('useReducedMotion', () => {
  function Probe() {
    return <p>{useReducedMotion() ? 'reduced' : 'full'}</p>
  }

  it('follows the media query and its changes', () => {
    const media = fakeMatchMedia(false)
    render(<Probe />)
    expect(screen.getByText('full')).toBeInTheDocument()
    act(() => media.set(true))
    expect(screen.getByText('reduced')).toBeInTheDocument()
  })

  it('is on when the Settings override data-motion="off" is set, and follows it live', async () => {
    fakeMatchMedia(false)
    render(<Probe />)
    await act(async () => {
      document.documentElement.setAttribute('data-motion', 'off')
      await Promise.resolve()
    })
    expect(screen.getByText('reduced')).toBeInTheDocument()
  })
})

describe('CountUp', () => {
  let frames: ReturnType<typeof fakeFrames>
  beforeEach(() => {
    frames = fakeFrames()
  })

  it('shows the final value at once under reduced motion and runs no animation loop', () => {
    fakeMatchMedia(true)
    const { container } = render(<CountUp value={72} />)
    expect(live(container)).toHaveTextContent('72')
    expect(frames.pending()).toBe(0)
  })

  it('shows the final value at once for an estimate (still)', () => {
    fakeMatchMedia(false)
    const { container } = render(<CountUp value={72} still />)
    expect(live(container)).toHaveTextContent('72')
    expect(frames.pending()).toBe(0)
  })

  it('counts from 0 to the value, ends exactly on it and stops its loop', () => {
    fakeMatchMedia(false)
    const { container } = render(<CountUp value={80} />)
    expect(frames.pending()).toBe(1)
    expect(live(container)).toHaveTextContent('0')
    frames.step(350)
    const mid = Number(live(container).textContent)
    expect(mid).toBeGreaterThan(0)
    expect(mid).toBeLessThan(80)
    frames.step(400)
    expect(live(container)).toHaveTextContent('80')
    expect(frames.pending()).toBe(0)
  })

  it('counts from the previous value when the value changes', () => {
    fakeMatchMedia(false)
    const { container, rerender } = render(<CountUp value={80} />)
    frames.step(800)
    rerender(<CountUp value={60} />)
    frames.step(10)
    const n = Number(live(container).textContent)
    expect(n).toBeLessThanOrEqual(80)
    expect(n).toBeGreaterThanOrEqual(60)
    frames.step(800)
    expect(live(container)).toHaveTextContent('60')
  })

  it('keeps the final text in the DOM for width and for screen readers; digits are aria-hidden', () => {
    fakeMatchMedia(false)
    const { container } = render(<CountUp value={80} format={(n) => `${Math.round(n)} pts`} />)
    const sizer = container.querySelector('.ov-countup__final') as HTMLElement
    expect(sizer).toHaveTextContent('80 pts')
    expect(sizer).toHaveAttribute('aria-hidden', 'true')
    expect(live(container)).toHaveAttribute('aria-hidden', 'true')
    expect(container.querySelector('.ov-sr')).toHaveTextContent('80 pts')
    const before = container.innerHTML.replace(/>[^<]*</g, '><')
    frames.step(300)
    expect(container.innerHTML.replace(/>[^<]*</g, '><')).toBe(before)
    expect(container.querySelector('[aria-live]')).toBeNull()
  })

  it('waits for the element to be on screen before counting', () => {
    fakeMatchMedia(false)
    const view = fakeIntersection()
    const { container } = render(<CountUp value={80} />)
    expect(frames.pending()).toBe(0)
    expect(live(container)).toHaveTextContent('80')
    act(() => view.show())
    expect(frames.pending()).toBe(1)
    frames.step(800)
    expect(live(container)).toHaveTextContent('80')
  })

  it('shares one frame loop between several count-ups', () => {
    fakeMatchMedia(false)
    render(
      <>
        <CountUp value={10} />
        <CountUp value={20} />
        <CountUp value={30} />
      </>,
    )
    expect(frames.pending()).toBe(1)
  })
})

describe('Reveal', () => {
  it('renders in its final state at once under reduced motion', () => {
    fakeMatchMedia(true)
    fakeIntersection()
    render(<Reveal>card</Reveal>)
    expect(screen.getByText('card')).toHaveClass('ov-reveal--in')
  })

  it('stays hidden until on screen, then reveals once, with the stagger index as a custom property', () => {
    fakeMatchMedia(false)
    const view = fakeIntersection()
    render(<Reveal index={3}>card</Reveal>)
    const el = screen.getByText('card')
    expect(el).not.toHaveClass('ov-reveal--in')
    expect(el.style.getPropertyValue('--ov-reveal-i')).toBe('3')
    act(() => view.show())
    expect(el).toHaveClass('ov-reveal--in')
    expect(el).toBeVisible()
  })

  it('is visible in the server-rendered markup and adds the hidden class only after mount', () => {
    fakeMatchMedia(false)
    fakeIntersection()
    // The server snapshot of useReducedMotion is true, so the prerendered card is in its final state.
    const html = renderToString(<Reveal>card</Reveal>)
    expect(html).toContain('ov-reveal--in')
    expect(html).not.toContain('ov-reveal--pre')
    // Hydrating it with motion on: nothing hidden until the layout effect has run, then --pre.
    const container = document.createElement('div')
    container.innerHTML = html
    document.body.appendChild(container)
    const origError = console.error
    console.error = () => {}
    act(() => {
      hydrateRoot(container, <Reveal>card</Reveal>)
    })
    console.error = origError
    const el = container.querySelector('.ov-reveal') as HTMLElement
    expect(el).toHaveClass('ov-reveal--pre')
    expect(el).not.toHaveClass('ov-reveal--in')
    container.remove()
  })

  it('has no hidden class on the first render, before mount effects run', () => {
    fakeMatchMedia(false)
    fakeIntersection()
    expect(renderToString(<Reveal>card</Reveal>)).not.toContain('ov-reveal--pre')
  })

  it('reveals at once without IntersectionObserver, and keeps the children and tag it was given', () => {
    fakeMatchMedia(false)
    vi.stubGlobal('IntersectionObserver', undefined)
    render(
      <Reveal as="section" className="mine">
        <b>inner</b>
      </Reveal>,
    )
    const el = screen.getByText('inner').parentElement as HTMLElement
    expect(el.tagName).toBe('SECTION')
    expect(el).toHaveClass('ov-reveal', 'ov-reveal--in', 'mine')
  })
})

describe('Skeleton', () => {
  it('reserves the size of the content with custom properties and hides from screen readers', () => {
    const { container } = render(<Skeleton width="8rem" height="2rem" />)
    const el = container.querySelector('.ov-skeleton') as HTMLElement
    expect(el.style.getPropertyValue('--ov-skel-w')).toBe('8rem')
    expect(el.style.getPropertyValue('--ov-skel-h')).toBe('2rem')
    expect(el).toHaveAttribute('aria-hidden', 'true')
  })

  it('swaps to the content, which carries the fade-in class', () => {
    const { container, rerender } = render(
      <SkeletonSwap loading skeleton={<Skeleton height="1rem" />}>
        done
      </SkeletonSwap>,
    )
    expect(container.querySelector('[aria-busy="true"]')).not.toBeNull()
    expect(screen.queryByText('done')).toBeNull()
    rerender(
      <SkeletonSwap loading={false} skeleton={<Skeleton height="1rem" />}>
        done
      </SkeletonSwap>,
    )
    expect(screen.getByText('done')).toHaveClass('ov-swap-in')
    expect(container.querySelector('.ov-skeleton')).toBeNull()
  })
})
