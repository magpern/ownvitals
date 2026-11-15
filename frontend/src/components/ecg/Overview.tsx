import { useEffect, useRef } from 'react'
import { useT } from '../../i18n'
import { readColours } from './drawStrip'
import { ROW_SECONDS } from './geometry'

// The whole recording at a glance (30 s) with the row on screen outlined. Not to scale (the strip
// above is); a click or a key moves the row. Pure drawing of the samples it is given.

const WIDTH = 1000
const HEIGHT = 44

interface Props {
  samples: readonly number[]
  rate: number
  start: number
  onPick: (seconds: number) => void
}

export function Overview({ samples, rate, start, onPick }: Props) {
  const t = useT()
  const canvas = useRef<HTMLCanvasElement>(null)
  const duration = samples.length / rate

  useEffect(() => {
    const element = canvas.current
    const ctx = element?.getContext('2d') ?? null
    if (element === null || ctx === null || duration <= 0) return
    const colours = readColours(element)
    ctx.fillStyle = colours.paper
    ctx.fillRect(0, 0, WIDTH, HEIGHT)
    let peak = 0
    for (const v of samples) peak = Math.max(peak, Math.abs(v))
    const step = Math.max(1, Math.floor(samples.length / WIDTH))
    ctx.strokeStyle = colours.trace
    ctx.lineWidth = 0.8
    ctx.beginPath()
    for (let i = 0; i < samples.length; i += step) {
      const x = (i / samples.length) * WIDTH
      const y = HEIGHT / 2 - (peak === 0 ? 0 : ((samples[i] ?? 0) / peak) * (HEIGHT / 2 - 3))
      if (i === 0) ctx.moveTo(x, y)
      else ctx.lineTo(x, y)
    }
    ctx.stroke()
    ctx.strokeStyle = colours.window
    ctx.lineWidth = 2
    ctx.strokeRect((start / duration) * WIDTH, 1, (Math.min(ROW_SECONDS, duration - start) / duration) * WIDTH, HEIGHT - 2)
  }, [samples, duration, start])

  return (
    <canvas
      ref={canvas}
      className="ov-ecg__overview"
      width={WIDTH}
      height={HEIGHT}
      role="img"
      aria-label={t('ecg.overview.label')}
      onClick={(e) => {
        const box = e.currentTarget.getBoundingClientRect()
        if (box.width > 0) onPick(((e.clientX - box.left) / box.width) * duration)
      }}
    />
  )
}
