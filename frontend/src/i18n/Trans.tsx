import { Fragment, type ReactNode } from 'react'
import type { MessageKey } from './en'
import { t } from './store'
import { splitTemplate } from './translate'
import { useLocale } from './useI18n'

/**
 * A message with parts that are not text: a link, an emphasised word. The message names the
 * parts as `{name}` placeholders and holds no markup; the caller passes each part under its name.
 * A name with no slot stays as written. Text values in `slots` are inserted as text.
 *
 *   <Trans k="auth.example" slots={{ link: <Link to="/reset">{t('auth.signIn.forgot')}</Link> }} />
 */
export function Trans({ k, slots }: { k: MessageKey; slots: Readonly<Record<string, ReactNode>> }) {
  useLocale()
  const parts = splitTemplate(t(k))
  return (
    <>
      {parts.map((part, index) => {
        // splitTemplate alternates text (even) and placeholder names (odd).
        if (index % 2 === 0) return part === '' ? null : <Fragment key={index}>{part}</Fragment>
        const slot = Object.hasOwn(slots, part) ? slots[part] : undefined
        return <Fragment key={index}>{slot ?? `{${part}}`}</Fragment>
      })}
    </>
  )
}
