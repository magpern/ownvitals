import { decodeReading, readingPath, dayUrl, type ReadingView } from '../../api/metrics'
import { useResource } from '../../api/useResource'
import { isMessageKey, useT } from '../../i18n'
import { useFormat } from '../../i18n/useFormat'
import { Link } from '../../router/Router'
import { ResourceView, StatePill } from '../ui/DataState'
import { SourceMark } from '../ui/TierMark'
import { Mono, Value } from '../ui/Value'
import { decimalsOf } from './metricData'
import { useMetricName } from './names'

// The reading drawer (docs/design/mockups/src/Reading.dc.html): one reading, where it came from and
// what was checked before it was used. It is opened by the address (`?reading=<id>`), so a link
// reaches it and the back button closes it.

function attribText(t: ReturnType<typeof useT>, attrib: number | null): string {
  if (attrib === null) return t('detail.reading.attrib.none')
  const key = `detail.reading.attrib.${String(attrib)}`
  return isMessageKey(key) ? t(key) : t('detail.reading.attrib.unknown')
}

function gateName(t: ReturnType<typeof useT>, gate: string): string {
  const key = `detail.reading.gate.${gate}`
  return isMessageKey(key) ? t(key) : gate
}

function ReadingBody({ reading }: { reading: ReadingView }) {
  const t = useT()
  const f = useFormat()
  const device = reading.device
  const deviceLine =
    device === null || (device.type === null && device.firmware === null)
      ? t('detail.reading.deviceNone')
      : [device.type, device.firmware === null ? null : t('detail.reading.firmware', { version: device.firmware })]
          .filter((part): part is string => part !== null)
          .join(' · ')
  return (
    <>
      <Value size="large" value={f.number(reading.value, decimalsOf(reading.valueExact))} unit={reading.unit} />
      <p className="ov-small">
        <SourceMark source="withings" />
      </p>
      <dl className="ov-metric__facts">
        <dt>{t('detail.reading.at')}</dt>
        <dd>
          {f.dateTime(reading.at, reading.sourceTz)} · {t('detail.reading.zone', { zone: reading.sourceTz })}
        </dd>
        <dt>{t('detail.reading.device')}</dt>
        <dd>{deviceLine}</dd>
        <dt>{t('detail.reading.attrib')}</dt>
        <dd>{attribText(t, reading.attrib)}</dd>
        <dt>{t('detail.reading.raw')}</dt>
        <dd>
          <Mono>
            {t('detail.reading.rawFormula', { raw: reading.raw.valueRaw, exponent: reading.raw.unitExponent })}
          </Mono>
        </dd>
      </dl>
      <p>
        <StatePill tone={reading.usedInDaily ? 'ok' : 'attn'}>
          {t(reading.usedInDaily ? 'detail.reading.used' : 'detail.reading.notUsed')}
        </StatePill>
      </p>
      <h4 className="ov-card__title ov-card__title--small">{t('detail.reading.gates')}</h4>
      <ul className="ov-metric__list">
        {reading.gates.map((gate) => (
          <li key={gate.gate} data-passed={gate.passed ? 'true' : 'false'}>
            {gateName(t, gate.gate)}{' '}
            <span className="ov-muted">
              {t(gate.passed ? 'detail.reading.gate.passed' : 'detail.reading.gate.failed')}
              {gate.advisory ? ` · ${t('detail.reading.gate.advisory')}` : ''}
            </span>
          </li>
        ))}
      </ul>
      <h4 className="ov-card__title ov-card__title--small">{t('detail.reading.provenance')}</h4>
      <p className="ov-small ov-muted">
        <Mono>
          {t('detail.reading.provenanceLine', {
            version: reading.provenance.algoVersion,
            revision: reading.provenance.inputRevision,
          })}
        </Mono>
        {reading.provenance.recomputePending ? ` ${t('detail.reading.pending')}` : ''}
      </p>
    </>
  )
}

export function ReadingPanel({ id, metric, day }: { id: string; metric: string; day: string }) {
  const t = useT()
  const name = useMetricName()(metric)
  const resource = useResource(readingPath(id), decodeReading)
  return (
    <aside className="ov-metric__drawer" aria-label={t('detail.reading.title')}>
      <div className="ov-card__head">
        <h3 className="ov-card__title">{`${t('detail.reading.title')} · ${name}`}</h3>
        <Link to={dayUrl(metric, day)} className="ov-btn">
          {t('detail.reading.close')}
        </Link>
      </div>
      <ResourceView resource={resource}>{(reading) => <ReadingBody reading={reading} />}</ResourceView>
    </aside>
  )
}
