// An area file holds short keys; the catalogue joins them as `<area>.<key>`.

export type Prefixed<A extends string, T> = { [K in keyof T & string as `${A}.${K}`]: string }

export function prefixed<A extends string, T extends Record<string, string>>(
  area: A,
  messages: T,
): Prefixed<A, T> {
  const entries = Object.entries(messages).map(([key, text]) => [`${area}.${key}`, text])
  return Object.fromEntries(entries) as Prefixed<A, T>
}
