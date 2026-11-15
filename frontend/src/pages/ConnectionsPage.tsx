import { useState } from 'react'
import type { Disconnected } from '../api/connections'
import { ConnectionCard } from '../components/connections/ConnectionCard'
import { StateNotices } from '../components/connections/StateNotice'
import { StreamsTable } from '../components/connections/StreamsTable'
import { useOutcome } from '../components/connections/useOutcome'
import { useStreamName } from '../components/connections/useStreamName'
import { useWithings } from '../components/connections/useWithings'
import { Notice } from '../components/forms'
import { Card, ComingLater, Stack } from '../components/ui/Card'
import { ResourceView } from '../components/ui/DataState'
import { useT } from '../i18n'

// Connections (docs/design/mockups/src/Connections.dc.html): the Withings card with its state, how
// updates arrive, per-stream availability, devices, and Disconnect. The other states are notices
// above the card. The frame supplies the page header.

export function ConnectionsPage() {
  const t = useT()
  const name = useStreamName()
  const { connection, status } = useWithings()
  const [done, setDone] = useState<Disconnected | null>(null)
  const outcome = useOutcome(window.location.search)

  const onDisconnected = (result: Disconnected) => {
    setDone(result)
    connection.reload()
    status.reload()
  }

  return (
    <Stack>
      {outcome !== null ? <Notice kind="error">{outcome}</Notice> : null}
      {done !== null ? (
        <Notice kind="success">
          {done.importedDeleted ? t('connections.disconnect.doneDeleted') : t('connections.disconnect.done')}
        </Notice>
      ) : null}
      {done?.revokeFailed === true ? <Notice kind="error">{t('connections.disconnect.revokeFailed')}</Notice> : null}
      <ResourceView resource={status}>
        {(s) => (
          <>
            <StateNotices status={s} />
            <ResourceView resource={connection}>
              {(c) => (c === null ? null : <ConnectionCard connection={c} status={s} onDisconnected={onDisconnected} />)}
            </ResourceView>
            <Card title={t('connections.streams.title')}>
              <StreamsTable streams={s.streams} name={name} />
              <p className="ov-muted">{t('connections.streams.note')}</p>
            </Card>
          </>
        )}
      </ResourceView>
      <Card title={t('connections.devices.title')}>
        <ComingLater label={t('connections.devices.title')}>{t('connections.devices.later')}</ComingLater>
        <p className="ov-muted">{t('connections.devices.note')}</p>
      </Card>
    </Stack>
  )
}
