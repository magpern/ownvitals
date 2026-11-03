import { createHmac } from 'node:crypto'

// RFC 6238 as the server checks it: SHA-1, 6 digits, 30 s steps, one step of tolerance either
// side, and a code of a step that was already used is refused (replay protection). So a test that
// types several codes in a row needs a new step each time.

const PERIOD = 30
const ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'

export function base32Decode(text: string): Buffer {
  const clean = text.replace(/[\s=]/g, '').toUpperCase()
  let bits = 0
  let value = 0
  const bytes: number[] = []
  for (const char of clean) {
    const index = ALPHABET.indexOf(char)
    if (index < 0) throw new Error('not a base32 key')
    value = (value << 5) | index
    bits += 5
    if (bits >= 8) {
      bytes.push((value >>> (bits - 8)) & 0xff)
      bits -= 8
    }
  }
  return Buffer.from(bytes)
}

/** The code of one step (`floor(unix time / 30)`). */
export function totpCode(secret: string, step: number): string {
  const counter = Buffer.alloc(8)
  counter.writeBigUInt64BE(BigInt(step))
  const digest = createHmac('sha1', base32Decode(secret)).update(counter).digest()
  const offset = (digest[digest.length - 1] ?? 0) & 0x0f
  const binary =
    (((digest[offset] ?? 0) & 0x7f) << 24) |
    ((digest[offset + 1] ?? 0) << 16) |
    ((digest[offset + 2] ?? 0) << 8) |
    (digest[offset + 3] ?? 0)
  return String(binary % 1_000_000).padStart(6, '0')
}

const currentStep = (): number => Math.floor(Date.now() / 1000 / PERIOD)
const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms))

/**
 * One account's authenticator. `next()` returns a code for a step later than the one it handed
 * out before. The server accepts the step after the current one, so a code is usually available
 * at once; when that one is used up too, it waits for the clock to catch up (at most 30 s).
 */
export class Authenticator {
  private last = -1
  readonly secret: string

  constructor(secret: string) {
    this.secret = secret
  }

  async next(): Promise<string> {
    const step = Math.max(currentStep(), this.last + 1)
    while (currentStep() < step - 1) await sleep(500)
    this.last = step
    return totpCode(this.secret, step)
  }
}
