import { DecodeError, isObject, list, object, type JsonObject } from './decode'

// Small decoders shared by the metric, day and reading modules.

export function num(source: JsonObject, key: string): number {
  const value = source[key]
  if (typeof value !== 'number' || !Number.isFinite(value)) throw new DecodeError(key)
  return value
}

export function optNum(source: JsonObject, key: string): number | null {
  const value = source[key]
  if (value === undefined || value === null) return null
  if (typeof value !== 'number' || !Number.isFinite(value)) throw new DecodeError(key)
  return value
}

export function optObject(source: JsonObject, key: string): JsonObject | null {
  const value = source[key]
  if (value === undefined || value === null) return null
  if (!isObject(value)) throw new DecodeError(key)
  return value
}

export function objects(source: JsonObject, key: string): JsonObject[] {
  return list(source, key).map((item) => object(item, key))
}
