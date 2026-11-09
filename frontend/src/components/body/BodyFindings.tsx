import type { Finding } from '../../api/findings'
import { useT } from '../../i18n'
import { Link } from '../../router/Router'
import { FindingCard } from '../findings/FindingCard'
import { bodyFindings } from './bodyModel'

// The body findings (rules B2, B4 to B6; B1 and B3 only when the API sends them, which it does
// only with the owner flag): the cards of the findings inbox, each opening the finding there.

export function BodyFindings({ findings }: { findings: readonly Finding[] }) {
  const t = useT()
  const shown = bodyFindings(findings)
  if (shown.length === 0) return <p className="ov-muted">{t('body.findings.empty')}</p>
  return (
    <>
      <ul className="ov-flist" aria-label={t('body.findings.title')}>
        {shown.map((f) => (
          <FindingCard key={f.id} finding={f} to={`/findings?id=${encodeURIComponent(f.id)}`} selected={false} />
        ))}
      </ul>
      <Link to="/findings">{t('body.findings.all')}</Link>
    </>
  )
}
