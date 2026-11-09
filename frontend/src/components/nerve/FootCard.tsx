import { useT } from '../../i18n'
import { Card } from '../ui/Card'
import { DataState } from '../ui/DataState'

// The per-foot pairs and the N5 symmetric index. The catalogue lists no per-foot metric yet, so the
// card reads "not supported" with the overview's own sentence: never an empty card, never a zero.

export function FootCard() {
  const t = useT()
  return (
    <Card level={3} title={t('nerve.foot.title')}>
      <DataState kind="not-supported">{t('overview.tile.detail.not_supported')}</DataState>
    </Card>
  )
}
