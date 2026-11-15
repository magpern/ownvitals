import type { StreamStatus } from '../../api/overview'
import { useT } from '../../i18n'
import { useFormat } from '../../i18n/useFormat'
import { browserZone } from '../../lib/format'
import { DataState, StatePill } from '../ui/DataState'
import { isBlocked, stateKind } from './connectionModel'

// "What we import" (docs/design/mockups/src/Connections.dc.html): one row per stream, with what
// Withings lets us read, when it last gave data and the stream's own state. A stream that cannot
// be read says why and shows no date; it is never a zero.

function State({ stream }: { stream: StreamStatus }) {
  const t = useT()
  const kind = stateKind(stream.state)
  if (kind === 'importing') return <DataState kind="importing" progress={stream.progressPercent ?? 0} />
  if (kind === null) return <StatePill>{t('connections.streams.queued')}</StatePill>
  if (kind === 'insufficient-data') return null
  return <DataState kind={kind} />
}

export function StreamsTable({
  streams,
  name,
}: {
  streams: readonly StreamStatus[]
  name: (stream: string) => string
}) {
  const t = useT()
  const format = useFormat()
  return (
    <div className="ov-tbl-wrap">
      <table className="ov-tbl">
        <thead>
          <tr>
            <th scope="col">{t('connections.streams.col.stream')}</th>
            <th scope="col">{t('connections.streams.col.available')}</th>
            <th scope="col">{t('connections.streams.col.lastSync')}</th>
            <th scope="col">{t('connections.streams.col.state')}</th>
          </tr>
        </thead>
        <tbody>
          {streams.map((stream) => {
            const blocked = isBlocked(stream.state)
            return (
              <tr key={stream.stream}>
                <th scope="row" className={blocked ? 'ov-muted' : undefined}>
                  {name(stream.stream)}
                </th>
                <td>{blocked ? t('connections.streams.no') : t('connections.streams.yes')}</td>
                <td className="ov-num">
                  {stream.lastOkAt === null || blocked
                    ? t('connections.streams.never')
                    : format.dateTime(stream.lastOkAt, browserZone())}
                </td>
                <td>
                  <State stream={stream} />
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
