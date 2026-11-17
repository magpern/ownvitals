import { useId } from 'react'
import { decodeJobs, JOBS_PATH } from '../../api/adminViews'
import { useResource } from '../../api/useResource'
import { ResourceView } from '../../components/ui/DataState'
import { useT } from '../../i18n'

// Jobs (ADR 0005): counts per state, nothing else. A dead-lettered job is the one to look at.

export function JobsView() {
  const t = useT()
  const headingId = useId()
  const jobs = useResource(JOBS_PATH, decodeJobs)
  return (
    <section className="ov-stack" aria-labelledby={headingId}>
      <h2 id={headingId}>{t('admin.jobs.title')}</h2>
      <ResourceView resource={jobs}>
        {(states) =>
          states.length === 0 ? (
            <p className="ov-muted">{t('admin.jobs.empty')}</p>
          ) : (
            <div className="ov-tbl-wrap" tabIndex={0} role="region" aria-label={t('admin.jobs.title')}>
              <table className="ov-tbl">
                <thead>
                  <tr>
                    <th>{t('admin.jobs.col.state')}</th>
                    <th className="ov-r">{t('admin.jobs.col.count')}</th>
                  </tr>
                </thead>
                <tbody>
                  {states.map((row) => (
                    <tr key={row.state}>
                      <td className="ov-mono">{row.state}</td>
                      <td className="ov-r ov-num">{row.count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        }
      </ResourceView>
      <p className="ov-small ov-muted">{t('admin.jobs.note')}</p>
    </section>
  )
}
