import { decodeFlags, SETTINGS_PATH, type FlagView } from '../../api/adminViews'
import { useResource } from '../../api/useResource'
import { Card } from '../../components/ui/Card'
import { ResourceView, StatePill } from '../../components/ui/DataState'
import { isMessageKey, useT } from '../../i18n'

// Flags: what is in force and where it comes from. Read-only: the change controls come with the
// step-up writes (#546 part 3), so nothing here is a button.

function FlagRow({ flag }: { flag: FlagView }) {
  const t = useT()
  const nameKey = `admin.flags.name.${flag.key}`
  const sourceKey = `admin.flags.source.${flag.source}`
  return (
    <div className="ov-setting">
      <div className="ov-setting__what">
        <span className="ov-setting__label">{isMessageKey(nameKey) ? t(nameKey) : flag.key}</span>
        <p>
          {isMessageKey(sourceKey) ? t(sourceKey) : flag.source}
          {flag.storedValid ? '' : ` ${t('admin.flags.invalid')}`}
        </p>
      </div>
      <StatePill tone={flag.storedValid ? 'neutral' : 'attn'}>{flag.effective ?? t('admin.flags.none')}</StatePill>
    </div>
  )
}

export function FlagsView() {
  const t = useT()
  const flags = useResource(SETTINGS_PATH, decodeFlags)
  return (
    <Card title={t('admin.flags.title')}>
      <ResourceView resource={flags}>
        {(rows) =>
          rows.length === 0 ? (
            <p className="ov-muted">{t('admin.flags.empty')}</p>
          ) : (
            <>
              {rows.map((flag) => (
                <FlagRow key={flag.key} flag={flag} />
              ))}
            </>
          )
        }
      </ResourceView>
      <p className="ov-small ov-muted">{t('admin.flags.note')}</p>
    </Card>
  )
}
