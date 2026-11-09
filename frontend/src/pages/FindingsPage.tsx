import { useState } from 'react'
import { decodeFindingsPage, findingsListPath, type Filters as FilterState, type Finding } from '../api/findings'
import { useResource } from '../api/useResource'
import { FindingCard } from '../components/findings/FindingCard'
import { FindingDetail } from '../components/findings/FindingDetail'
import { Filters } from '../components/findings/Filters'
import { belongsToTab, locationUrl, readLocation } from '../components/findings/model'
import { Button } from '../components/ui/Button'
import { ResourceView } from '../components/ui/DataState'
import { useT } from '../i18n'
import { useQuery } from '../router/Router'
import '../styles/findings.css'

// The findings inbox (docs/design/mockups/src/Findings.dc.html): Live and History tabs, tier and
// area filters, the findings, and the chosen one in full. The tab, the filters and the chosen
// finding are in the address. The server decides what this account may see; this page only
// words and draws it. A finding found while importing history belongs under History.

function Items({ findings, filters, chosen }: { findings: Finding[]; filters: FilterState; chosen: string | null }) {
  return (
    <>
      {findings
        .filter((finding) => belongsToTab(finding, filters.tab))
        .map((finding) => (
          <FindingCard key={finding.id} finding={finding} to={locationUrl({ filters, id: finding.id })} selected={finding.id === chosen} />
        ))}
    </>
  )
}

function MorePage({ filters, offset, chosen, last, more }: { filters: FilterState; offset: number; chosen: string | null; last: boolean; more: (next: number) => void }) {
  const t = useT()
  const page = useResource(findingsListPath(filters, offset), decodeFindingsPage)
  return (
    <ResourceView resource={page}>
      {({ findings, nextOffset }) => (
        <>
          <Items findings={findings} filters={filters} chosen={chosen} />
          {last && nextOffset !== null ? (
            <li>
              <Button onClick={() => { more(nextOffset) }}>{t('findings.list.more')}</Button>
            </li>
          ) : null}
        </>
      )}
    </ResourceView>
  )
}

function Browser({ filters, id }: { filters: FilterState; id: string | null }) {
  const t = useT()
  const [later, setLater] = useState<number[]>([])
  const first = useResource(findingsListPath(filters, 0), decodeFindingsPage)
  const shown = first.data?.findings.filter((finding) => belongsToTab(finding, filters.tab)) ?? []
  const chosen = id ?? shown[0]?.id ?? null
  const today = new Date().toISOString().slice(0, 10)
  return (
    <div className="ov-findings-grid">
      <div className="ov-findings-grid__list">
        <p className="ov-muted ov-small">{t(filters.tab === 'live' ? 'findings.note.live' : 'findings.note.history')}</p>
        <ResourceView resource={first}>
          {({ nextOffset }) => (
            <ul className="ov-flist" aria-label={t('findings.list.label')}>
              {shown.length === 0 ? (
                <li className="ov-muted">{t(filters.tab === 'live' ? 'findings.list.empty.live' : 'findings.list.empty.history')}</li>
              ) : (
                <Items findings={shown} filters={filters} chosen={chosen} />
              )}
              {later.map((offset, i) => (
                <MorePage key={offset} filters={filters} offset={offset} chosen={chosen} last={i === later.length - 1} more={(next) => { setLater([...later, next]) }} />
              ))}
              {later.length === 0 && nextOffset !== null ? (
                <li>
                  <Button onClick={() => { setLater([nextOffset]) }}>{t('findings.list.more')}</Button>
                </li>
              ) : null}
            </ul>
          )}
        </ResourceView>
      </div>
      <div className="ov-findings-grid__detail">{chosen === null ? null : <FindingDetail key={chosen} id={chosen} today={today} />}</div>
    </div>
  )
}

export function FindingsPage() {
  const { filters, id } = readLocation(useQuery())
  return (
    <div className="ov-stack" data-gap="lg">
      <Filters filters={filters} />
      <Browser key={locationUrl({ filters, id: null })} filters={filters} id={id} />
    </div>
  )
}
