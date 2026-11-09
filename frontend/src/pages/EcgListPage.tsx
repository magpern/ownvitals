import { useState } from 'react'
import { decodeRecordingPage, ecgListPath, type Recording } from '../api/ecg'
import { useResource } from '../api/useResource'
import { Classification } from '../components/ecg/Classification'
import { isPositive } from '../components/ecg/model'
import { Button } from '../components/ui/Button'
import { Card, Stack } from '../components/ui/Card'
import { ResourceView } from '../components/ui/DataState'
import { useT } from '../i18n'
import { useFormat } from '../i18n/useFormat'
import { Link } from '../router/Router'
import '../styles/ecg.css'

// ECG recordings (docs/design/mockups/src/ECG.dc.html, the list): time with its own zone, Withings'
// heart rate ("no rate" when missing) and Withings' result, attributed. A positive result is urgent
// red (E1). Older pages load on request. The frame supplies the page header.

function Row({ recording }: { recording: Recording }) {
  const t = useT()
  const f = useFormat()
  return (
    <li>
      <Link to={`/heart/ecg/${recording.id}`} className="ov-ecg__row" data-positive={isPositive(recording.afib) ? 'true' : undefined}>
        <span className="ov-ecg__row-head">
          <span className="ov-ecg__number">{f.dateTime(recording.recordedAt, recording.sourceTz)}</span>
          <span className="ov-muted">
            {recording.heartRate === null ? t('ecg.noRate') : t('ecg.rate', { bpm: f.number(recording.heartRate) })}
          </span>
        </span>
        <Classification afib={recording.afib} details={recording.afibDetails} />
      </Link>
    </li>
  )
}

function RecordingsPage({ offset, last, more }: { offset: number; last: boolean; more: (next: number) => void }) {
  const t = useT()
  const page = useResource(ecgListPath(offset), decodeRecordingPage)
  return (
    <ResourceView resource={page}>
      {({ recordings, nextOffset }) => (
        <>
          {offset === 0 && recordings.length === 0 ? <p className="ov-muted">{t('ecg.list.empty')}</p> : null}
          <ul className="ov-ecg__list">
            {recordings.map((r) => (
              <Row key={r.id} recording={r} />
            ))}
          </ul>
          {last && nextOffset !== null ? (
            <Button onClick={() => more(nextOffset)}>{t('ecg.list.more')}</Button>
          ) : null}
        </>
      )}
    </ResourceView>
  )
}

export function EcgListPage() {
  const t = useT()
  const [offsets, setOffsets] = useState([0])
  return (
    <Stack>
      <Card title={t('ecg.list.title')}>
        {offsets.map((offset, i) => (
          <RecordingsPage key={offset} offset={offset} last={i === offsets.length - 1} more={(next) => setOffsets([...offsets, next])} />
        ))}
        <p className="ov-muted">{t('ecg.list.note')}</p>
      </Card>
    </Stack>
  )
}
