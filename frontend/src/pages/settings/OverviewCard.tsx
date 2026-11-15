import { AREAS, type Area } from '../../api/profile'
import { Card } from '../../components/ui/Card'
import { Button } from '../../components/ui/Button'
import { useT } from '../../i18n'
import { Row, Toggle } from './controls'
import type { ProfileState } from './useProfile'

// "Overview": which areas the overview shows and in what order (the shown ones first, in the
// saved order), and whether metrics the devices don't support appear as "Not supported". The
// mock-up says "Drag to reorder"; a row moves with its up and down buttons (keyboard and touch
// work the same way).

/** The shown areas in order, then the hidden ones, so every area has a row. */
function rows(shown: readonly Area[]): { area: Area; shown: boolean }[] {
  return [
    ...shown.map((area) => ({ area, shown: true })),
    ...AREAS.filter((area) => !shown.includes(area)).map((area) => ({ area, shown: false })),
  ]
}

export function OverviewCard({ state }: { state: ProfileState }) {
  const t = useT()
  const { profile, busy, save } = state
  if (profile === null) return null
  const shown = profile.overview_order
  const list = rows(shown)

  const toggle = (area: Area, on: boolean) => {
    void save({ overview_order: on ? [...shown, area] : shown.filter((a) => a !== area) })
  }
  const move = (area: Area, by: -1 | 1) => {
    const next = [...shown]
    const at = next.indexOf(area)
    const to = at + by
    if (at < 0 || to < 0 || to >= next.length) return
    next[at] = next[to] as Area
    next[to] = area
    void save({ overview_order: next })
  }

  return (
    <Card title={t('settings.overview.title')} aside={<span className="ov-small ov-muted">{t('settings.overview.hint')}</span>}>
      <ul className="ov-settings-list">
        {list.map(({ area, shown: on }) => {
          const name = t(`settings.area.${area}`)
          const at = shown.indexOf(area)
          return (
            <li key={area} className="ov-setting">
              <div className="ov-setting__what ov-settings-row">
                <span>{t(`settings.overview.${area}`)}</span>
              </div>
              <div className="ov-settings-actions">
                <Button
                  aria-label={t('settings.overview.up', { area: name })}
                  disabled={busy || !on || at === 0}
                  onClick={() => {
                    move(area, -1)
                  }}
                >
                  ↑
                </Button>
                <Button
                  aria-label={t('settings.overview.down', { area: name })}
                  disabled={busy || !on || at === shown.length - 1}
                  onClick={() => {
                    move(area, 1)
                  }}
                >
                  ↓
                </Button>
                <Toggle
                  label={t('settings.overview.show', { area: name })}
                  checked={on}
                  disabled={busy}
                  onChange={(checked) => {
                    toggle(area, checked)
                  }}
                />
              </div>
            </li>
          )
        })}
      </ul>
      <Row label={t('settings.unsupported.label')} help={t('settings.unsupported.help')}>
        <Toggle
          label={t('settings.unsupported.label')}
          checked={profile.show_unsupported}
          disabled={busy}
          onChange={(checked) => {
            void save({ show_unsupported: checked })
          }}
        />
      </Row>
    </Card>
  )
}
