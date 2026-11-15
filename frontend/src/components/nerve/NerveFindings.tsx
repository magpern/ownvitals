import type { Finding } from '../../api/findings'
import { useT } from '../../i18n'
import { Link } from '../../router/Router'
import { FindingCard } from '../findings/FindingCard'
import { nerveFindings } from './nerveModel'

// The nerve findings (rules N2, N3, N5): the cards of the findings inbox, each opening the finding
// there. Guided and routine N2 findings are told apart by their own names.

export function NerveFindings({ findings }: { findings: readonly Finding[] }) {
  const t = useT()
  const shown = nerveFindings(findings)
  if (shown.length === 0) return <p className="ov-muted">{t('nerve.findings.empty')}</p>
  return (
    <>
      <ul className="ov-flist" aria-label={t('nerve.findings.title')}>
        {shown.map((f) => (
          <FindingCard key={f.id} finding={f} to={`/findings?id=${encodeURIComponent(f.id)}`} selected={false} />
        ))}
      </ul>
      <Link to="/findings">{t('nerve.findings.all')}</Link>
    </>
  )
}
