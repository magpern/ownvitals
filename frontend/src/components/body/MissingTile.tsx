import { useT } from '../../i18n'
import { Card } from '../ui/Card'
import { DataState } from '../ui/DataState'

// A body metric the API did not list at all: the tile still stands, as "not available", so the
// page never has a hole where a number might be (design principle 2).

export function MissingTile({ name }: { name: string }) {
  const t = useT()
  return (
    <Card level={3} title={name}>
      <DataState kind="not-available">{t('overview.tile.detail.not_available')}</DataState>
    </Card>
  )
}
