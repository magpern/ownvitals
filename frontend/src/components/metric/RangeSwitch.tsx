import { RANGES, metricUrl, type Range } from '../../api/metrics'
import { useT } from '../../i18n'
import { Link } from '../../router/Router'

// The range of a metric page, as links: the range is in the address, so a range can be linked to
// and the back button returns to the one before.
export function RangeSwitch({ metric, range }: { metric: string; range: Range }) {
  const t = useT()
  return (
    <nav className="ov-range" aria-label={t('detail.range.label')}>
      {RANGES.map((option) => (
        <Link
          key={option}
          to={metricUrl(metric, option)}
          className="ov-range__item"
          aria-current={option === range ? 'true' : undefined}
        >
          {t(`detail.range.${option}`)}
        </Link>
      ))}
    </nav>
  )
}
