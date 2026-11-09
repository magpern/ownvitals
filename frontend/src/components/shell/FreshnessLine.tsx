import type { Status } from '../../api/overview'
import { useT } from '../../i18n'
import { isMessageKey } from '../../i18n/store'
import { Link } from '../../router/Router'
import { freshness, type FreshItem } from '../overview/overviewModel'

// The freshness line under the top bar of every app screen (docs/design/mockups/src/Dashboard.dc.html;
// PLAN section 8): when Withings last synced, imports in progress, an import that stopped, a
// connection that needs a look. What a stream cannot give (not supported, not shared, not
// available) is behind "Details", so the line stays quiet. While the status loads, or fails to
// load, the line is not shown: the screen under it says what is wrong with its own data.

function Item({ item }: { item: FreshItem }) {
  const dot = item.tone === 'ok' || item.tone === 'attn' || item.tone === 'urgent' ? item.tone : null
  return (
    <li className={`ov-fresh__item ov-fresh__item--${item.tone}`}>
      {dot === null ? null : <span className={`ov-dot ov-dot--${dot}`} aria-hidden="true" />}
      {item.text}
    </li>
  )
}

export function FreshnessLine({ status }: { status: Status | null }) {
  const t = useT()
  if (status === null) return null
  const { lead, details } = freshness(status, new Date(status.asOf), t, isMessageKey)
  const fix = lead.some((item) => item.fix === 'connect')
  return (
    <section className="ov-fresh" aria-label={t('overview.fresh.label')}>
      <ul className="ov-fresh__list">
        {lead.map((item) => (
          <Item key={item.id} item={item} />
        ))}
      </ul>
      {fix ? (
        <Link className="ov-fresh__link" to="/connections">
          {t('overview.fresh.connect')}
        </Link>
      ) : null}
      {details.length === 0 ? null : (
        <details className="ov-fresh__details">
          <summary>{t('overview.fresh.details')}</summary>
          <ul className="ov-fresh__list">
            {details.map((item) => (
              <Item key={item.id} item={item} />
            ))}
          </ul>
        </details>
      )}
    </section>
  )
}
