import { useId } from 'react'
import { decodeWithingsApp, WITHINGS_APP_PATH } from '../../api/adminOverview'
import { useResource } from '../../api/useResource'
import { ResourceView } from '../../components/ui/DataState'
import { useT } from '../../i18n'

// The Withings app (Admin mock-up, issue 549): read-only, the non-secret parts only. The server
// sends the last four characters of the client id and the API host; never the client secret, the
// webhook secrets, the callback URL or the whole id.

export function WithingsAppCard() {
  const t = useT()
  const headingId = useId()
  const app = useResource(WITHINGS_APP_PATH, decodeWithingsApp)
  return (
    <section className="ov-stack" aria-labelledby={headingId}>
      <h2 id={headingId}>{t('admin.withings.label')}</h2>
      <ResourceView resource={app}>
        {(found) => (
          <div className="ov-card">
            {found.configured ? (
              <dl className="ov-kv">
                <dt>{t('admin.withings.app')}</dt>
                <dd>
                  {t('admin.withings.name')}
                  {found.clientHint === null ? null : (
                    <>
                      {' · '}
                      {t('admin.withings.client')} <span className="ov-mono">{found.clientHint}</span>
                    </>
                  )}
                </dd>
                <dt>{t('admin.withings.api')}</dt>
                <dd className="ov-mono">{found.apiHost}</dd>
              </dl>
            ) : (
              <p className="ov-muted">{t('admin.withings.none')}</p>
            )}
          </div>
        )}
      </ResourceView>
      <p className="ov-small ov-muted">{t('admin.withings.note')}</p>
    </section>
  )
}
