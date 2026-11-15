import { useT } from '../../i18n'
import type { MessageKey } from '../../i18n/en'
import { Link } from '../../router/Router'
import { RANGES, type RangeId } from './overviewModel'

// The range of the overview (`?range=`): a row of links, so the choice is part of the address and
// carries to the metric pages. The current one is marked for a screen reader and by weight.

export function RangeSelector({ path, range }: { path: string; range: RangeId }) {
  const t = useT()
  return (
    <nav className="ov-ranges" aria-label={t('overview.range.label')}>
      {RANGES.map((r) => (
        <Link
          key={r}
          className="ov-ranges__item"
          to={`${path}?range=${r}`}
          aria-current={r === range ? 'true' : undefined}
        >
          {t(`overview.range.${r}` as MessageKey)}
        </Link>
      ))}
    </nav>
  )
}
