import type { ReactNode } from 'react'
import type { Status } from '../../api/overview'
import { useT } from '../../i18n'
import { ConnectButton } from './ConnectButton'
import { useStreamName } from './useStreamName'

// The same card in other states (Connections mock-up, "The same card in other states"): not
// connected, needs reconnect, Withings+ needed, and a stream that was not shared. Each says what
// happened and what to do; none of them says "no data".

function Callout({ tone, title, children, action }: { tone: string; title: string; children: ReactNode; action?: ReactNode }) {
  return (
    <div className={`ov-callout ov-callout--${tone}`} role="status">
      <p>
        <strong>{title}</strong> {children}
      </p>
      {action}
    </div>
  )
}

export function StateNotices({ status }: { status: Status }) {
  const t = useT()
  const name = useStreamName()
  const connection = status.connection
  const notShared = status.streams.filter((s) => s.state === 'not_shared').map((s) => name(s.stream))
  return (
    <>
      {connection === 'not_connected' ? (
        <Callout
          tone="neutral"
          title={t('connections.notice.not-connected.title')}
          action={<ConnectButton label={t('connections.action.connect')} />}
        >
          {t('connections.notice.not-connected.text')}
        </Callout>
      ) : null}
      {connection === 'needs_reconnect' ? (
        <Callout
          tone="urgent"
          title={t('connections.notice.needs-reconnect.title')}
          action={<ConnectButton label={t('connections.action.reconnect')} variant="default" />}
        >
          {t('connections.notice.needs-reconnect.text')}
        </Callout>
      ) : null}
      {connection === 'withings_plus_needed' ? (
        <Callout tone="info" title={t('connections.notice.withings-plus.title')}>
          {t('connections.notice.withings-plus.text')}
        </Callout>
      ) : null}
      {connection === 'connected' && notShared.length > 0 ? (
        <Callout
          tone="attn"
          title={t('connections.notice.not-shared.title')}
          action={
            <ConnectButton
              label={t('connections.action.reconnectShare', { streams: notShared.join(', ') })}
              variant="default"
            />
          }
        >
          {t('connections.notice.not-shared.text', { streams: notShared.join(', ') })}
        </Callout>
      ) : null}
    </>
  )
}
