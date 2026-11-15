import { useT } from '../../i18n'
import { afibKey, detailsKey } from './model'

/** "Withings classified this ECG as ...": Withings' result, attributed; the page adds nothing. */
export function Classification({ afib, details }: { afib: number | null; details: number | null }) {
  const t = useT()
  const code = (n: number) => t('ecg.code', { code: String(n) })
  const meaning = afib === null ? null : (afibKey(afib) ?? null)
  const detailKey = details === null ? null : detailsKey(details)
  return (
    <>
      <p className="ov-ecg__result" data-positive={afib === 1 ? 'true' : undefined}>
        {afib === null
          ? t('ecg.classified.none')
          : t('ecg.classified', { result: meaning === null ? code(afib) : t(meaning) })}
      </p>
      {details === null ? null : (
        <p className="ov-muted">{t('ecg.detail', { result: detailKey === null ? code(details) : t(detailKey) })}</p>
      )}
    </>
  )
}
