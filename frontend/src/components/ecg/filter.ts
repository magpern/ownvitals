// The opt-in "filtered 0.5-40 Hz" view: a second-order high-pass and low-pass, run forwards and
// backwards (zero phase). Display only; the stored signal is never changed and is what the PDF
// prints. Pure.

type Biquad = [number, number, number, number, number] // b0 b1 b2 a1 a2, a0 = 1

function design(kind: 'low' | 'high', cutoffHz: number, rate: number): Biquad {
  const w = (2 * Math.PI * cutoffHz) / rate
  const q = Math.SQRT1_2
  const alpha = Math.sin(w) / (2 * q)
  const c = Math.cos(w)
  const a0 = 1 + alpha
  const edge = kind === 'low' ? (1 - c) / 2 : (1 + c) / 2
  const middle = kind === 'low' ? 1 - c : -(1 + c)
  return [edge / a0, middle / a0, edge / a0, (-2 * c) / a0, (1 - alpha) / a0]
}

function run(x: readonly number[], [b0, b1, b2, a1, a2]: Biquad, passesDc: boolean): number[] {
  const y: number[] = new Array<number>(x.length).fill(0)
  let x1 = x[0] ?? 0
  let x2 = x1
  let y1 = passesDc ? x1 : 0 // start settled at the first sample, so an offset is not a step
  let y2 = y1
  for (let i = 0; i < x.length; i++) {
    const xi = x[i] ?? 0
    const yi = b0 * xi + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
    y[i] = yi
    x2 = x1
    x1 = xi
    y2 = y1
    y1 = yi
  }
  return y
}

function zeroPhase(x: readonly number[], biquad: Biquad, passesDc: boolean): number[] {
  return run(run(x, biquad, passesDc).reverse(), biquad, passesDc).reverse()
}

export function bandpass(x: readonly number[], rate: number): number[] {
  if (x.length < 3 || rate <= 80) return [...x]
  return zeroPhase(zeroPhase(x, design('high', 0.5, rate), false), design('low', 40, rate), true)
}
