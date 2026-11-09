import type { ComponentType } from 'react'
import type { RouteId } from '../router/appRoutes'
import { useT } from '../i18n'
import { DataTab } from './settings/DataTab'
import { NotificationsTab } from './settings/NotificationsTab'
import { ProfileTab } from './settings/ProfileTab'
import { SettingsTabs } from './settings/SettingsTabs'
import { useProfile, type ProfileState } from './settings/useProfile'

/** Issue 203: Settings, "Profile and display", "Notifications" and "Your data". */

function SettingsScreen({ tab: Tab }: { tab: ComponentType<{ state: ProfileState }> }) {
  const t = useT()
  const state = useProfile()
  return (
    <div className="ov-stack" data-gap="lg">
      <SettingsTabs />
      {state.profile !== null ? (
        <Tab state={state} />
      ) : state.resource.status === 'error' ? (
        <p role="alert">{state.resource.error?.message ?? t('settings.loadFailed')}</p>
      ) : (
        <p className="ov-muted">{t('common.loadingDots')}</p>
      )}
    </div>
  )
}

function Profile() {
  return <SettingsScreen tab={ProfileTab} />
}
function Notifications() {
  return <SettingsScreen tab={NotificationsTab} />
}
function Data() {
  return <SettingsScreen tab={DataTab} />
}

export const SETTINGS_PAGES: Partial<Record<RouteId, ComponentType>> = {
  settings: Profile,
  notifications: Notifications,
  data: Data,
}
