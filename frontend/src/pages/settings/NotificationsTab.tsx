import { TIERS, type Tier } from '../../api/profile'
import { Notice } from '../../components/forms'
import { Card, Stack } from '../../components/ui/Card'
import { useT } from '../../i18n'
import { Chip, Row, Toggle } from './controls'
import type { ProfileState } from './useProfile'

// "Notifications" (/settings/notifications). In-app notices are always on. An email only says that
// something is waiting, with a link, and never what or how much (ADR 0007, T59); the screen says
// so. Imported history never notifies.

export function NotificationsTab({ state }: { state: ProfileState }) {
  const t = useT()
  const { profile, busy, note, save } = state
  if (profile === null) return null

  const flip = (tier: Tier) => {
    const on = profile.notify_tiers.includes(tier)
    void save({
      notify_tiers: on ? profile.notify_tiers.filter((x) => x !== tier) : [...profile.notify_tiers, tier],
    })
  }

  return (
    <Stack gap="lg">
      {note !== null ? <Notice kind={note.kind}>{note.text}</Notice> : null}
      <Card title={t('settings.notify.title')}>
        <Row label={t('settings.notify.inApp.label')} help={t('settings.notify.inApp.help')}>
          <Toggle label={t('settings.notify.inApp.aria')} checked disabled />
        </Row>
        <Row label={t('settings.notify.email.label')} help={t('settings.notify.email.help')}>
          <Toggle
            label={t('settings.notify.email.label')}
            checked={profile.notify_email}
            disabled={busy}
            onChange={(checked) => {
              void save({ notify_email: checked })
            }}
          />
        </Row>
        <Row label={t('settings.notify.weekly.label')} help={t('settings.notify.weekly.help')}>
          <Toggle
            label={t('settings.notify.weekly.label')}
            checked={profile.notify_weekly}
            disabled={busy}
            onChange={(checked) => {
              void save({ notify_weekly: checked })
            }}
          />
        </Row>
        <Row label={t('settings.notify.tiers.label')} help={t('settings.notify.tiers.help')}>
          <div className="ov-settings-chips" role="group" aria-label={t('settings.notify.tiers.label')}>
            {TIERS.map((tier) => {
              const on = profile.notify_tiers.includes(tier)
              return (
                <Chip
                  key={tier}
                  label={t(`settings.tier.${tier}`)}
                  pressed={on}
                  disabled={busy || (tier === 'experimental' && !on && !profile.experimentalAvailable)}
                  onClick={() => {
                    flip(tier)
                  }}
                />
              )
            })}
          </div>
        </Row>
      </Card>
    </Stack>
  )
}
