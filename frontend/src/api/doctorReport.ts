import { post, get } from './client'
import { decodeItem, downloadFile, type ExportItem } from './exports'
import { list, object } from './decode'

// /api/reports/doctor (issue 210, ADR 0013 section 6): ask for the doctor PDF with a period, metric
// keys and up to 10 ECG recording ids (nothing else travels), follow its state, download it once.
// It shares the export row's shape and its single-use, same-origin download.

export type DoctorItem = ExportItem

export interface DoctorChoices {
  start: string
  end: string
  metrics: string[]
  recordings: string[]
}

export const MAX_ECG = 10

export async function listDoctorReports(signal?: AbortSignal): Promise<DoctorItem[]> {
  return list(object(await get('/api/reports/doctor', signal), 'reports'), 'exports').map(decodeItem)
}

export async function requestDoctorReport(choices: DoctorChoices): Promise<DoctorItem> {
  return decodeItem(object(await post('/api/reports/doctor', choices), 'requested').export)
}

export function downloadDoctorReport(id: string): Promise<void> {
  return downloadFile(
    `/api/reports/doctor/${encodeURIComponent(id)}/file`,
    'application/pdf',
    'ownvitals-health-report.pdf',
  )
}
