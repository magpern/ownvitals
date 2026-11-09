import type { Status } from '../../api/overview'
import { useT } from '../../i18n'
import type { MessageKey } from '../../i18n/en'
import { Card } from '../ui/Card'
import { DataState } from '../ui/DataState'
import { agoText, detailKey, stateKind, streamState } from './overviewModel'

// A tile for what has no daily value yet (sleep, activity, workouts): the state of its stream as
// the status reports it. No number: the stream's data has no read API of its own yet, and a tile
// never invents one.

interface Props {
  /** The catalogue key of the tile's name. */
  name: MessageKey
  status: Status
  stream: string
}

export function StreamTile({ name, status, stream }: Props) {
  const t = useT()
  // A stream the status does not list is one nothing is known about: "not available".
  const s = streamState(status, stream) ?? { state: 'not_available', percent: null, lastOkAt: null }
  const kind = stateKind(s.state)
  const detail =
    kind === null && s.lastOkAt !== null
      ? t('overview.tile.syncedAt', { when: agoText(s.lastOkAt, new Date(status.asOf), t) })
      : t(detailKey(s.state))
  return (
    <Card level={3} title={t(name)}>
      {kind === 'importing' ? (
        <DataState kind="importing" progress={s.percent ?? 0}>
          {detail}
        </DataState>
      ) : kind === 'insufficient-data' ? (
        <DataState kind="insufficient-data">{detail}</DataState>
      ) : (
        <DataState kind={kind ?? 'up-to-date'}>{detail}</DataState>
      )}
    </Card>
  )
}
