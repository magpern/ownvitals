import { useId, useState } from 'react'
import type { ChartRange, DatetimeFormat, Profile, Sex, TemperatureUnit, WeightUnit } from '../../api/profile'
import { Notice } from '../../components/forms'
import { Card, Stack } from '../../components/ui/Card'
import { useT } from '../../i18n'
import { browserZone } from '../../lib/format'
import { setThemePreference, type ThemePreference } from '../../theme/theme'
import { AccountLanguage } from './AccountLanguage'
import { Row, Seg, type Option } from './controls'
import { OverviewCard } from './OverviewCard'
import type { ProfileState } from './useProfile'

// "Profile and display" (/settings/profile; docs/design/mockups/src/Settings.dc.html): the
// analysis profile, the display options and the overview layout. Each change is saved at once as
// the whole profile; the server validates it. Height is not here: ownvitals has no stored height
// (the mock-up reads it from Withings), so the row waits for a data source.

const THIS_YEAR = new Date().getFullYear()

function BirthYear({ state }: { state: ProfileState }) {
  const t = useT()
  const id = useId()
  const stored = state.profile?.birth_year ?? null
  // `draft` is what is being typed; null shows the stored year.
  const [draft, setDraft] = useState<string | null>(null)
  const [invalid, setInvalid] = useState(false)
  const value = draft ?? (stored === null ? '' : String(stored))

  const commit = () => {
    const trimmed = value.trim()
    if (trimmed === '') {
      setInvalid(false)
      setDraft(null)
      if (stored !== null) void state.save({ birth_year: null })
      return
    }
    const year = Number(trimmed)
    if (!/^\d{4}$/.test(trimmed) || year < 1900 || year > THIS_YEAR) {
      setInvalid(true)
      return
    }
    setInvalid(false)
    setDraft(null)
    if (year !== stored) void state.save({ birth_year: year })
  }

  return (
    <Row
      label={t('settings.birth.label')}
      help={t('settings.birth.help')}
      htmlFor={id}
      below={invalid ? <Notice kind="error">{t('settings.birth.invalid')}</Notice> : null}
    >
      <input
        id={id}
        className="ov-input ov-input--short"
        inputMode="numeric"
        value={value}
        disabled={state.busy}
        aria-invalid={invalid}
        onChange={(event) => {
          setDraft(event.target.value)
        }}
        onBlur={commit}
        onKeyDown={(event) => {
          if (event.key === 'Enter') commit()
        }}
      />
    </Row>
  )
}

function AnalysisCard({ state, profile }: { state: ProfileState; profile: Profile }) {
  const t = useT()
  const sexId = useId()
  const dayId = useId()
  const zone = profile.day_tz ?? browserZone()
  return (
    <Card title={t('settings.analysis.title')}>
      <BirthYear state={state} />
      <Row label={t('settings.sex.label')} help={t('settings.sex.help')} htmlFor={sexId}>
        <select
          id={sexId}
          className="ov-input"
          value={profile.sex ?? ''}
          disabled={state.busy}
          onChange={(event) => {
            const sex = event.target.value
            void state.save({ sex: sex === 'female' || sex === 'male' ? (sex satisfies Sex) : null })
          }}
        >
          <option value="">{t('settings.sex.unset')}</option>
          <option value="female">{t('settings.sex.female')}</option>
          <option value="male">{t('settings.sex.male')}</option>
        </select>
      </Row>
      <Row label={t('settings.day.label')} help={t('settings.day.help')} htmlFor={dayId}>
        <select
          id={dayId}
          className="ov-input"
          value={profile.day_assignment}
          disabled={state.busy}
          onChange={(event) => {
            void state.save(
              event.target.value === 'fixed_tz'
                ? { day_assignment: 'fixed_tz', day_tz: zone }
                : { day_assignment: 'reading_tz', day_tz: null },
            )
          }}
        >
          <option value="reading_tz">{t('settings.day.reading')}</option>
          <option value="fixed_tz">{t('settings.day.fixed', { zone })}</option>
        </select>
      </Row>
    </Card>
  )
}

function DisplayCard({ state, profile }: { state: ProfileState; profile: Profile }) {
  const t = useT()
  const formatId = useId()
  const busy = state.busy
  const theme: Option<ThemePreference>[] = (['system', 'light', 'dark'] as const).map((value) => ({
    value,
    label: t(`settings.theme.${value}`),
  }))
  const weight: Option<WeightUnit>[] = (['kg', 'lb', 'st'] as const).map((value) => ({
    value,
    label: t(`settings.weight.unit.${value}`),
  }))
  const temperature: Option<TemperatureUnit>[] = (['c', 'f'] as const).map((value) => ({
    value,
    label: t(`settings.temperature.unit.${value}`),
  }))
  const ranges: Option<ChartRange>[] = (['30d', '90d', '6mo', '1y'] as const).map((value) => ({
    value,
    label: t(`settings.range.${value}`),
  }))
  const formats: DatetimeFormat[] = ['d_mon_y', 'mon_d_y', 'iso']
  return (
    <Card title={t('settings.display.title')}>
      <AccountLanguage />
      <Row label={t('settings.theme.label')} help={t('settings.theme.help')}>
        <Seg
          label={t('settings.theme.label')}
          value={profile.theme}
          options={theme}
          disabled={busy}
          onChange={(value) => {
            setThemePreference(value) // at once on this device; the account keeps it for the others
            void state.save({ theme: value })
          }}
        />
      </Row>
      <Row label={t('settings.weight.label')}>
        <Seg
          label={t('settings.weight.label')}
          value={profile.weight_unit}
          options={weight}
          disabled={busy}
          onChange={(value) => {
            void state.save({ weight_unit: value })
          }}
        />
      </Row>
      <Row label={t('settings.temperature.label')}>
        <Seg
          label={t('settings.temperature.label')}
          value={profile.temperature_unit}
          options={temperature}
          disabled={busy}
          onChange={(value) => {
            void state.save({ temperature_unit: value })
          }}
        />
      </Row>
      <Row label={t('settings.format.label')} htmlFor={formatId}>
        <select
          id={formatId}
          className="ov-input"
          value={profile.datetime_format}
          disabled={busy}
          onChange={(event) => {
            const found = formats.find((f) => f === event.target.value)
            if (found !== undefined) void state.save({ datetime_format: found })
          }}
        >
          {formats.map((f) => (
            <option key={f} value={f}>
              {t(`settings.format.${f}`)}
            </option>
          ))}
        </select>
      </Row>
      <Row label={t('settings.range.label')}>
        <Seg
          label={t('settings.range.label')}
          value={profile.chart_range}
          options={ranges}
          disabled={busy}
          onChange={(value) => {
            void state.save({ chart_range: value })
          }}
        />
      </Row>
    </Card>
  )
}

export function ProfileTab({ state }: { state: ProfileState }) {
  const { profile, note } = state
  if (profile === null) return null
  return (
    <Stack gap="lg">
      {note !== null ? <Notice kind={note.kind}>{note.text}</Notice> : null}
      <div className="ov-settings-grid">
        <AnalysisCard state={state} profile={profile} />
        <DisplayCard state={state} profile={profile} />
        <OverviewCard state={state} />
      </div>
    </Stack>
  )
}
