import type { Progress } from '../../api/metrics'
import { useT } from '../../i18n'
import { DataState } from '../ui/DataState'
import { progressPercent } from './metricData'

// "Insufficient data": the progress toward the minimum, never a trend or a range (design principle
// 2). The bar and the counts come from the API's own progress, not from a guess.
export function ProgressNote({ progress, children }: { progress: Progress | null; children: string }) {
  const t = useT()
  return (
    <DataState kind="insufficient-data" progress={progressPercent(progress)}>
      {children}
      {progress === null ? null : (
        <>
          {' '}
          {t('detail.progress.days', { have: progress.daysWithData, need: progress.daysRequired })}
          {progress.spanRequired === null
            ? ''
            : ` · ${t('detail.progress.span', { have: progress.spanDays, need: progress.spanRequired })}`}
        </>
      )}
    </DataState>
  )
}
