import type { ReactNode } from 'react'
import { decodeOverview, OVERVIEW_PATH } from '../../api/adminOverview'
import { useResource } from '../../api/useResource'
import { ResourceView } from '../../components/ui/DataState'
import { plural, useT } from '../../i18n'

// The Overview's four tiles (Admin mock-up, issue 549): accounts, connections, the job queue and
// the second factor. Counts only, from one audited read; the dots mark what needs a look.

type Dot = 'urgent' | 'attn'

function Tile({ label, value, unit, children }: { label: string; value: number; unit?: string; children: ReactNode }) {
  return (
    <div className="ov-card ov-admin-tile">
      <span className="ov-eyebrow">{label}</span>
      <span className="ov-admin-tile__value ov-num">
        {value}
        {unit === undefined ? null : <span className="ov-admin-tile__unit ov-muted"> {unit}</span>}
      </span>
      <span className="ov-small ov-admin-tile__note">{children}</span>
    </div>
  )
}

/** A count that needs a look gets the mock-up's dot; a zero says so without one. */
function Flagged({ dot, count, children }: { dot: Dot; count: number; children: ReactNode }) {
  return (
    <span className="ov-admin-tile__flag">
      {count > 0 ? <span className={`ov-dot ov-dot--${dot}`} aria-hidden="true" /> : null}
      {children}
    </span>
  )
}

export function OverviewTiles() {
  const t = useT()
  const overview = useResource(OVERVIEW_PATH, decodeOverview)
  return (
    <ResourceView resource={overview}>
      {({ accounts, connections, jobs }) => {
        const pending = accounts.total - accounts.secondFactorOn
        return (
          <div className="ov-admin-tiles">
            <Tile label={t('admin.tile.accounts')} value={accounts.total}>
              <span className="ov-muted">{t(accounts.signup === 'invite' ? 'admin.tile.accounts.invite' : 'admin.tile.accounts.closed')}</span>
            </Tile>
            <Tile label={t('admin.tile.connections')} value={connections.healthy} unit={t('admin.tile.connections.healthy')}>
              <Flagged dot="urgent" count={connections.needsReconnect}>
                {plural('admin.tile.connections.reconnect', connections.needsReconnect)}
              </Flagged>
            </Tile>
            <Tile label={t('admin.tile.jobs')} value={jobs.queued} unit={t('admin.tile.jobs.queued')}>
              {plural('admin.tile.jobs.running', jobs.running)}
              {' · '}
              <Flagged dot="attn" count={jobs.deadLetter}>
                {plural('admin.tile.jobs.deadLetter', jobs.deadLetter)}
              </Flagged>
            </Tile>
            <Tile
              label={t('admin.tile.factor')}
              value={accounts.secondFactorOn}
              unit={t('admin.tile.factor.of', { total: accounts.total })}
            >
              <Flagged dot="attn" count={pending}>
                {plural('admin.tile.factor.pending', pending)}
              </Flagged>
            </Tile>
          </div>
        )
      }}
    </ResourceView>
  )
}
