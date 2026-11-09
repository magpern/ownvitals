import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { SIGNED_IN, fakeApi } from '../test/fakeApi'
import { syntheticDetail, syntheticEstimate, syntheticRecording } from '../test/ecgFixtures'
import { renderAt } from '../test/render'

// jsdom has no canvas: a fake 2D context records the dashed marker lines (the estimate's R-peaks).
let dashed = 0
beforeEach(() => {
  dashed = 0
  const store: Record<string, unknown> = {}
  const ctx = new Proxy(store, {
    get: (target, key: string) => {
      if (key === 'setLineDash') return (d: number[]) => void (d.length > 0 && dashed++)
      return key in target ? target[key] : () => {}
    },
    set: (target, key: string, value) => {
      target[key] = value
      return true
    },
  })
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(ctx as unknown as CanvasRenderingContext2D)
})
afterEach(() => {
  vi.restoreAllMocks()
})

const SESSION = { 'GET /api/auth/session': SIGNED_IN }
const ID = '11111111-1111-4111-8111-111111111111'
const DETAIL = `GET /api/ecg/${ID}`

describe('the ECG list', () => {
  it('attributes the result to Withings and says "no rate" for a missing rate', async () => {
    const second = syntheticRecording({
      id: '22222222-2222-4222-8222-222222222222',
      heart_rate_bpm: null,
    })
    fakeApi({
      ...SESSION,
      'GET /api/ecg?offset=0': {
        recordings: [syntheticRecording(), second],
        next_offset: null,
      },
    })
    renderAt('/heart/ecg')
    expect(await screen.findAllByText(/Withings classified this ECG as: no sign of atrial fibrillation/)).toHaveLength(
      2,
    )
    expect(screen.getByText('64 bpm')).toBeTruthy()
    expect(screen.getByText('no rate')).toBeTruthy()
  })

  it('words an undocumented code as "Withings code n" and marks a positive result urgent', async () => {
    const positive = syntheticRecording({
      classification: {
        code: 1,
        meaning: 'positive',
        details_code: null,
        details_meaning: null,
      },
    })
    const odd = syntheticRecording({
      id: '33333333-3333-4333-8333-333333333333',
      classification: {
        code: 7,
        meaning: null,
        details_code: 42,
        details_meaning: null,
      },
    })
    fakeApi({
      ...SESSION,
      'GET /api/ecg?offset=0': {
        recordings: [positive, odd],
        next_offset: null,
      },
    })
    renderAt('/heart/ecg')
    expect(await screen.findByText(/signs of atrial fibrillation/)).toBeTruthy()
    const rows = screen.getAllByRole('link').filter((a) => a.getAttribute('href')?.startsWith('/heart/ecg/'))
    expect(rows[0]?.getAttribute('data-positive')).toBe('true')
    expect(rows[1]?.getAttribute('data-positive')).toBeNull()
    expect(within(rows[1] as HTMLElement).getByText(/Withings code 7/)).toBeTruthy()
    expect(within(rows[1] as HTMLElement).getByText(/Withings detail: Withings code 42/)).toBeTruthy()
  })

  it('loads older recordings on request', async () => {
    fakeApi({
      ...SESSION,
      'GET /api/ecg?offset=0': {
        recordings: [syntheticRecording()],
        next_offset: 20,
      },
      'GET /api/ecg?offset=20': {
        recordings: [syntheticRecording({ id: '44444444-4444-4444-8444-444444444444' })],
        next_offset: null,
      },
    })
    renderAt('/heart/ecg')
    await userEvent.click(await screen.findByRole('button', { name: 'Older recordings' }))
    await vi.waitFor(() => expect(screen.getAllByText(/Withings classified/)).toHaveLength(2))
  })
})

describe('the ECG viewer', () => {
  it('shows Withings values as reported, "not reported" where missing, and the PDF link', async () => {
    fakeApi({ ...SESSION, [DETAIL]: syntheticDetail() })
    renderAt(`/heart/ecg/${ID}`)
    expect(await screen.findByText('94')).toBeTruthy()
    expect(screen.getByText('162')).toBeTruthy()
    expect(screen.getAllByText('not reported')).toHaveLength(2) // QT and QTc
    expect(screen.getByText(/All values reported by Withings/)).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Download PDF' }).getAttribute('href')).toBe(`/api/ecg/${ID}.pdf`)
    expect(screen.getAllByText(/lead unverified/).length).toBeGreaterThan(0)
  })

  it('shows the nominal scale, a labelled 5 mm/mV option and never an automatic gain', async () => {
    fakeApi({ ...SESSION, [DETAIL]: syntheticDetail() })
    renderAt(`/heart/ecg/${ID}`)
    expect((await screen.findByTestId('ecg-scale')).textContent).toBe('25 mm/s · 10 mm/mV')
    await userEvent.click(screen.getByRole('button', { name: '5 mm/mV' }))
    expect(screen.getByTestId('ecg-scale').textContent).toContain('not the standard 10 mm/mV')
  })

  it('offers the filter only as an opt-in, off by default', async () => {
    fakeApi({ ...SESSION, [DETAIL]: syntheticDetail() })
    renderAt(`/heart/ecg/${ID}`)
    const box = await screen.findByRole<HTMLInputElement>('checkbox', { name: /Filtered 0\.5/ })
    expect(box.checked).toBe(false)
    expect(screen.getByText(/The stored signal, unmodified/)).toBeTruthy()
    await userEvent.click(box)
    expect(screen.getByText(/Filtered for display only/)).toBeTruthy()
  })

  it('draws and shows no estimate without the flag (the API sends null)', async () => {
    fakeApi({ ...SESSION, [DETAIL]: syntheticDetail(null) })
    renderAt(`/heart/ecg/${ID}`)
    await screen.findByTestId('ecg-scale')
    expect(screen.queryByText(/Estimated by ownvitals/)).toBeNull()
    expect(screen.queryByText('Experimental')).toBeNull()
    expect(dashed).toBe(0)
  })

  it('boxes the estimate, labels it as ours and draws its markers when the API sent one', async () => {
    fakeApi({ ...SESSION, [DETAIL]: syntheticDetail(syntheticEstimate()) })
    renderAt(`/heart/ecg/${ID}`)
    expect(
      await screen.findByText('Estimated by ownvitals from the raw signal; not a Withings measurement'),
    ).toBeTruthy()
    expect(screen.getByText('Signal quality: passed')).toBeTruthy()
    expect(screen.getByText('63.5')).toBeTruthy()
    expect(dashed).toBeGreaterThan(0)
  })

  it('hides the values of a failed gate and never shows a reason code as a label', async () => {
    const failed = syntheticEstimate({
      gate: 'fail',
      reasons: ['too_short', 'beat_gap', 'some_new_code'],
      hr_bpm: null,
      hrv: null,
      rpeaks: null,
    })
    fakeApi({ ...SESSION, [DETAIL]: syntheticDetail(failed) })
    renderAt(`/heart/ecg/${ID}`)
    expect(await screen.findByText('Signal quality: not passed')).toBeTruthy()
    expect(screen.getByText(/did not pass our signal quality check/)).toBeTruthy()
    expect(screen.getByText('There is a gap between beats.')).toBeTruthy()
    expect(screen.queryByText(/too_short|beat_gap|some_new_code/)).toBeNull()
    expect(screen.queryByText(/RMSSD/)).toBeNull()
  })

  it('says "not evaluated by ownvitals" when there is no gate result', async () => {
    fakeApi({
      ...SESSION,
      [DETAIL]: syntheticDetail(
        syntheticEstimate({
          gate: null,
          hr_bpm: null,
          hrv: null,
          rpeaks: null,
        }),
      ),
    })
    renderAt(`/heart/ecg/${ID}`)
    expect(await screen.findAllByText(/Not evaluated by ownvitals/)).not.toHaveLength(0)
  })

  it('has no strip and no PDF link for a recording without a stored signal', async () => {
    const detail = { ...syntheticDetail(), signal: null }
    fakeApi({ ...SESSION, [DETAIL]: detail })
    renderAt(`/heart/ecg/${ID}`)
    expect(await screen.findByText(/No signal is stored/)).toBeTruthy()
    expect(screen.queryByRole('link', { name: 'Download PDF' })).toBeNull()
  })
})
