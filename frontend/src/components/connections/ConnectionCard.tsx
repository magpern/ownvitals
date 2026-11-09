import { useState } from 'react'
import type { Connection, Disconnected } from '../../api/connections'
import type { Status } from '../../api/overview'
import { useT } from '../../i18n'
import { useFormat } from '../../i18n/useFormat'
import { browserZone } from '../../lib/format'
import { Button } from '../ui/Button'
import { Card } from '../ui/Card'
import { StatePill } from '../ui/DataState'
import { DisconnectDialog } from './DisconnectDialog'

// The Withings card (Connections mock-up): the state, when it was connected, when data last
// arrived, how updates arrive, and Disconnect. "Sync now" is not here: the API has no route for
// it (docs/design/README.md, "Gaps").

export function ConnectionCard({
  connection,
  status,
  onDisconnected,
}: {
  connection: Connection
  status: Status
  onDisconnected: (result: Disconnected) => void
}) {
  const t = useT()
  const format = useFormat()
  const [asking, setAsking] = useState(false)
  const zone = browserZone()
  const live = status.connection === 'connected'
  return (
    <Card
      title={t('connections.withings.title')}
      aside={
        <div className="ov-card__actions">
          {live ? (
            <StatePill tone="ok" dot="ok">
              {t('connections.state.connected')}
            </StatePill>
          ) : null}
          <Button
            variant="danger"
            lock
            onClick={() => {
              setAsking(true)
            }}
          >
            {t('connections.action.disconnect')}
          </Button>
        </div>
      }
    >
      <dl className="ov-kv">
        <dt>{t('connections.kv.connectedSince')}</dt>
        <dd>{format.dateTime(connection.connectedAt, zone)}</dd>
        <dt>{t('connections.kv.lastSync')}</dt>
        <dd>
          {status.lastSyncAt === null ? t('connections.kv.lastSync.never') : format.dateTime(status.lastSyncAt, zone)}
        </dd>
        <dt>{t('connections.kv.updates')}</dt>
        <dd>{t('connections.kv.updates.text')}</dd>
      </dl>
      {asking ? (
        <DisconnectDialog
          onClose={() => {
            setAsking(false)
          }}
          onDone={(result) => {
            setAsking(false)
            onDisconnected(result)
          }}
        />
      ) : null}
    </Card>
  )
}
