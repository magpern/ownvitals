import { useId } from 'react'
import { LOCALES, LOCALE_NAMES, isLocale } from './locale'
import { setLocale } from './store'
import { useI18n } from './useI18n'

/**
 * The language picker of the signed-out pages: the design's select control (`ov-input`). Each
 * option is the language's own name. A choice applies at once, without a reload, and is kept in
 * this browser (`ov-locale`).
 */
export function LanguagePicker() {
  const id = useId()
  const { locale, t } = useI18n()
  return (
    <div className="picker">
      <label htmlFor={id} className="picker__label">
        {t('common.language')}
      </label>
      <select
        id={id}
        className="ov-input"
        value={locale}
        onChange={(event) => {
          if (isLocale(event.target.value)) void setLocale(event.target.value)
        }}
      >
        {LOCALES.map((code) => (
          <option key={code} value={code} lang={code}>
            {LOCALE_NAMES[code]}
          </option>
        ))}
      </select>
    </div>
  )
}
