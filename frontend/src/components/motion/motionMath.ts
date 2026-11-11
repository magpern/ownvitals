// Pure helpers for the motion primitives: no DOM, no clock (the caller passes the time in).

/** The `--ov-ease-out` curve, cubic-bezier(0.2, 0, 0, 1), solved for y at progress x. */
export function easeOut(x: number): number {
  if (x <= 0) return 0
  if (x >= 1) return 1
  const [x1, y1, x2, y2] = [0.2, 0, 0, 1]
  const bez = (t: number, a: number, b: number) => 3 * a * t * (1 - t) ** 2 + 3 * b * t * t * (1 - t) + t ** 3
  let lo = 0
  let hi = 1
  for (let i = 0; i < 24; i += 1) {
    const mid = (lo + hi) / 2
    if (bez(mid, x1, x2) < x) lo = mid
    else hi = mid
  }
  return bez((lo + hi) / 2, y1, y2)
}

/** The value `elapsed` ms into a count from `from` to `to` over `duration` ms. */
export function countValue(from: number, to: number, elapsed: number, duration: number): number {
  if (duration <= 0 || elapsed >= duration) return to
  return from + (to - from) * easeOut(elapsed / duration)
}

/** The stagger step of the nth sibling: whole steps, never negative. */
export function staggerIndex(index: number): number {
  return Number.isFinite(index) && index > 0 ? Math.floor(index) : 0
}
