import { useEffect, useMemo, useRef, useState } from 'react'
import { useT } from '../../i18n'
import { useFormat } from '../../i18n/useFormat'
import { Button } from '../ui/Button'
import { drawRow, readColours } from './drawStrip'
import { bandpass } from './filter'
import { NOMINAL_GAIN, PX_PER_MM, ROW_SECONDS, peakOf, rowHeight, rowWidth, scaleOf, toMillivolts } from './geometry'
import { Overview } from './Overview'

// The calibrated strip (docs/design/mockups/src/ECG.dc.html): one 10 s row on a 1 mm / 5 mm grid at
// 25 mm/s and the chosen gain, a calibration pulse in the row, both axes zoomed together, the
// stored signal unless "filtered 0.5-40 Hz" is chosen, a 30 s overview to move through it. The gain
// is 10 mm/mV or a labelled 5 mm/mV: never automatic. The row is tall enough for the whole
// recording at that gain, so the trace is never clipped.

const ZOOMS = [1, 1.5, 2] as const
const GAINS = [NOMINAL_GAIN, 5] as const

interface Props {
  /** Microvolts, as stored. */
  microvolts: readonly number[]
  rate: number
  /** Experimental R-peaks as sample positions, not seconds: drawn only when the page was given an estimate. */
  markers?: readonly number[] | null
}

export function EcgCanvas({ microvolts, rate, markers }: Props) {
  const t = useT()
  const f = useFormat()
  const [zoom, setZoom] = useState<number>(1)
  const [gain, setGain] = useState<number>(NOMINAL_GAIN)
  const [filtered, setFiltered] = useState(false)
  const [windowIndex, setWindowIndex] = useState(0)
  const canvas = useRef<HTMLCanvasElement>(null)

  const stored = useMemo(() => toMillivolts(microvolts), [microvolts])
  const shown = useMemo(() => (filtered ? bandpass(stored, rate) : stored), [filtered, stored, rate])
  const peak = useMemo(() => peakOf(shown), [shown])
  const duration = stored.length / rate
  const windows = Math.max(1, Math.ceil(duration / ROW_SECONDS))
  const start = Math.min(windowIndex, windows - 1) * ROW_SECONDS
  const scale = scaleOf(zoom, gain)
  const width = rowWidth(scale)
  const height = rowHeight(scale, peak)

  useEffect(() => {
    const element = canvas.current
    const ctx = element?.getContext('2d') ?? null
    if (element === null || ctx === null) return
    const ratio = window.devicePixelRatio || 1
    // CSSOM, not a style attribute: the content security policy blocks the attribute.
    element.style.width = `${String(width)}px`
    element.style.height = `${String(height)}px`
    element.width = Math.round(width * ratio)
    element.height = Math.round(height * ratio)
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0)
    drawRow(ctx, { samples: shown, rate, start, scale: scaleOf(zoom, gain), peak, markers: markers ?? undefined }, readColours(element))
  }, [shown, rate, start, zoom, gain, peak, markers, width, height])

  const end = Math.min(duration, start + ROW_SECONDS)
  return (
    <figure className="ov-ecg">
      <div className="ov-ecg__bar">
        <span className="ov-muted">{t('ecg.window', { from: f.number(start), to: f.number(end), total: f.number(duration) })}</span>
        <span className="ov-mono ov-muted" data-testid="ecg-scale">
          {t('ecg.scale', { speed: '25', gain: f.number(gain) })}
          {gain === NOMINAL_GAIN ? '' : ` · ${t('ecg.scale.notNominal')}`}
        </span>
      </div>
      <div className="ov-ecg__scroll">
        <canvas ref={canvas} role="img" aria-label={t('ecg.strip.label', { gain: f.number(gain) })} />
      </div>
      <Overview samples={shown} rate={rate} start={start} onPick={(s) => setWindowIndex(Math.floor(s / ROW_SECONDS))} />
      <div className="ov-ecg__controls">
        <div className="ov-ecg__group">
          <Button disabled={windowIndex <= 0} onClick={() => setWindowIndex(windowIndex - 1)}>
            {t('ecg.earlier')}
          </Button>
          <Button disabled={windowIndex >= windows - 1} onClick={() => setWindowIndex(windowIndex + 1)}>
            {t('ecg.later')}
          </Button>
        </div>
        <div className="ov-seg" role="group" aria-label={t('ecg.gain')}>
          {GAINS.map((g) => (
            <button key={g} type="button" aria-pressed={gain === g} onClick={() => setGain(g)}>
              {t('ecg.gain.option', { gain: f.number(g) })}
            </button>
          ))}
        </div>
        <div className="ov-seg" role="group" aria-label={t('ecg.zoom')}>
          {ZOOMS.map((z) => (
            <button key={z} type="button" aria-pressed={zoom === z} onClick={() => setZoom(z)}>
              {`${f.number(z * 100)} %`}
            </button>
          ))}
        </div>
        <label className="ov-ecg__filter">
          <input type="checkbox" checked={filtered} onChange={(e) => setFiltered(e.target.checked)} />
          {t('ecg.filtered')}
        </label>
      </div>
      <p className="ov-muted ov-ecg__note">
        {filtered ? t('ecg.filtered.note') : t('ecg.stored.note')} {t('ecg.px', { px: f.number(PX_PER_MM * zoom, zoom === 1 ? 0 : 1) })}
      </p>
    </figure>
  )
}
