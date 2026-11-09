import { useT } from '../../i18n'
import { isMessageKey } from '../../i18n/store'

/** The words for a stream key ("ecg" is "ECG"); an unknown stream is shown by its key. */
export function useStreamName(): (stream: string) => string {
  const t = useT()
  return (stream) => {
    const key = `overview.stream.${stream}`
    return isMessageKey(key) ? t(key) : stream
  }
}
