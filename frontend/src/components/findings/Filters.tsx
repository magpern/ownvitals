import { AREAS, TABS, TIERS, type Area, type Filters as FilterState, type FindingTier } from '../../api/findings'
import { useT, type MessageKey } from '../../i18n'
import { Link, useRouter } from '../../router/Router'
import { TIER_LABELS } from '../ui/TierMark'
import { locationUrl } from './model'

// The tab, the tier chips and the area list of the findings screen. All of them live in the
// address: a choice is a navigation, so it can be linked to and the back button undoes it.

export function Filters({ filters }: { filters: FilterState }) {
  const t = useT()
  const { navigate } = useRouter()
  const go = (next: FilterState) => {
    navigate(locationUrl({ filters: next, id: null }))
  }
  return (
    <>
      <nav className="ov-tabs" aria-label={t('findings.tabs.label')}>
        {TABS.map((tab) => (
          <Link
            key={tab}
            to={locationUrl({ filters: { ...filters, tab }, id: null })}
            aria-current={tab === filters.tab ? 'page' : undefined}
          >
            {t(`findings.tab.${tab}`)}
          </Link>
        ))}
      </nav>
      <div className="ov-ffilters">
        <div className="ov-ffilters__chips" role="group" aria-label={t('findings.filter.tier')}>
          <button type="button" className="ov-chip" aria-pressed={filters.tier === null} onClick={() => { go({ ...filters, tier: null }) }}>
            {t('findings.filter.tier.all')}
          </button>
          {TIERS.map((tier: FindingTier) => (
            <button key={tier} type="button" className="ov-chip" aria-pressed={filters.tier === tier} onClick={() => { go({ ...filters, tier }) }}>
              {t(TIER_LABELS[tier])}
            </button>
          ))}
        </div>
        <select
          className="ov-input ov-ffilters__area"
          aria-label={t('findings.filter.area')}
          value={filters.area ?? ''}
          onChange={(event) => {
            const value = event.target.value
            go({ ...filters, area: AREAS.find((a): a is Area => a === value) ?? null })
          }}
        >
          <option value="">{t('findings.filter.area.all')}</option>
          {AREAS.map((area) => (
            <option key={area} value={area}>
              {t(`findings.area.${area}` as MessageKey)}
            </option>
          ))}
        </select>
      </div>
    </>
  )
}
