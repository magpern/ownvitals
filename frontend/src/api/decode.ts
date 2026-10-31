/** Small runtime checks for JSON from the API: nothing is trusted just because it parsed. */

export type JsonObject = Record<string, unknown>

export class DecodeError extends Error {
  constructor(what: string) {
    super(`Unexpected response from the server (${what}).`)
    this.name = 'DecodeError'
  }
}

export function isObject(value: unknown): value is JsonObject {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

export function object(value: unknown, what: string): JsonObject {
  if (!isObject(value)) throw new DecodeError(what)
  return value
}

export function text(source: JsonObject, key: string): string {
  const value = source[key]
  if (typeof value !== 'string') throw new DecodeError(key)
  return value
}

export function optionalText(source: JsonObject, key: string): string | null {
  const value = source[key]
  if (value === undefined || value === null) return null
  if (typeof value !== 'string') throw new DecodeError(key)
  return value
}

export function flag(source: JsonObject, key: string): boolean {
  const value = source[key]
  if (typeof value !== 'boolean') throw new DecodeError(key)
  return value
}

export function count(source: JsonObject, key: string): number {
  const value = source[key]
  if (typeof value !== 'number') throw new DecodeError(key)
  return value
}

export function list(source: JsonObject, key: string): unknown[] {
  const value = source[key]
  if (!Array.isArray(value)) throw new DecodeError(key)
  return value as unknown[]
}

export function strings(source: JsonObject, key: string): string[] {
  return list(source, key).map((item) => {
    if (typeof item !== 'string') throw new DecodeError(key)
    return item
  })
}

export function optionalCount(source: JsonObject, key: string): number | null {
  const value = source[key]
  if (value === undefined || value === null) return null
  if (typeof value !== 'number') throw new DecodeError(key)
  return value
}

export function optionalFlag(source: JsonObject, key: string): boolean | null {
  const value = source[key]
  if (value === undefined || value === null) return null
  if (typeof value !== 'boolean') throw new DecodeError(key)
  return value
}
