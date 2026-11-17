// The AI monthly caps are stored in micro-USD (one millionth of a dollar) and shown in USD. These
// pure helpers convert between the two; the server checks every amount again.

export const MICRO_PER_USD = 1_000_000
const MICRO_PER_CENT = 10_000

/** The cap's micro-USD from the flag's value as the views decode it ("10000000"), or null. */
export function microFromFlag(value: string | null): number | null {
  if (value === null || !/^\d{1,16}$/.test(value)) return null
  return Number(value)
}

/**
 * Micro-USD from what an admin typed in USD: whole dollars with at most two decimals, a dot or a
 * comma as the decimal mark ("10", "7.5", "2,25"). Anything else is null.
 */
export function microFromUsd(typed: string): number | null {
  const match = /^(\d{1,10})(?:[.,](\d{1,2}))?$/.exec(typed.trim())
  if (match === null) return null
  const dollars = Number(match[1])
  const cents = Number((match[2] ?? '').padEnd(2, '0'))
  return (dollars * 100 + cents) * MICRO_PER_CENT
}

/** The decimals to show: cents for an amount in whole cents, else all six. */
export function usdDigits(micro: number): number {
  return micro % MICRO_PER_CENT === 0 ? 2 : 6
}
