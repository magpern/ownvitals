import { INTL_TAG, type Locale } from './locale'

// Looking up and filling in a message. Pure: the catalogue and the locale are passed in.

export type Params = Readonly<Record<string, string | number>>
export type Catalogue = Readonly<Record<string, string>>

const PLACEHOLDER = /\{([A-Za-z][A-Za-z0-9]*)\}/g

/** Fills `{name}` placeholders. A name without a value stays as written. */
export function interpolate(template: string, params?: Params): string {
  if (params === undefined) return template
  return template.replace(PLACEHOLDER, (whole: string, name: string) => {
    if (!Object.hasOwn(params, name)) return whole
    return String(params[name])
  })
}

/** The text for `key` in `messages`, else in `fallback` (English), else the key itself. */
export function lookup(messages: Catalogue | undefined, fallback: Catalogue, key: string): string {
  if (messages !== undefined && Object.hasOwn(messages, key)) return messages[key] ?? key
  return Object.hasOwn(fallback, key) ? (fallback[key] ?? key) : key
}

export function hasKey(catalogue: Catalogue, key: string): boolean {
  return Object.hasOwn(catalogue, key)
}

/** The plural category of `count` in `locale`: "one", "other", ... */
export function pluralCategory(locale: Locale, count: number): Intl.LDMLPluralRule {
  return new Intl.PluralRules(INTL_TAG[locale]).select(count)
}

/** The categories a locale needs a form for. */
export function pluralCategories(locale: Locale): readonly Intl.LDMLPluralRule[] {
  return new Intl.PluralRules(INTL_TAG[locale]).resolvedOptions().pluralCategories
}

/**
 * The text of a plural family: `<base>.<category>` for the count, else `<base>.other`. `{count}`
 * is filled with `shown` (the count as the locale writes it) unless the caller passes its own.
 */
export function pluralText(
  messages: Catalogue | undefined,
  fallback: Catalogue,
  locale: Locale,
  base: string,
  count: number,
  shown: string,
  params?: Params,
): string {
  const category = pluralCategory(locale, count)
  const key = `${base}.${category}`
  const chosen = hasKey(messages ?? {}, key) || hasKey(fallback, key) ? key : `${base}.other`
  return interpolate(lookup(messages, fallback, chosen), { count: shown, ...params })
}

/** The parts of a template around its placeholders: text, name, text, name, ... */
export function splitTemplate(template: string): string[] {
  return template.split(PLACEHOLDER)
}
