import { useRouter } from '../router/Router'
import { matchAppRoute } from '../router/appRoutes'

/** The `:metric` and `:day` of the address (null when the address is not one of the metric routes). */
export function useMetricParams(): { metric: string | null; day: string | null } {
  const { pathname } = useRouter()
  const params = matchAppRoute(pathname)?.params ?? {}
  return { metric: params.metric ?? null, day: params.day ?? null }
}
