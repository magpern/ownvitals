import { useCallback } from 'react'
import { decodeWithings, type Connection } from '../../api/connections'
import { decodeStatus, type Status } from '../../api/overview'
import { useResource, type Resource } from '../../api/useResource'

/** The two reads the Withings screens share: the live connection row and the stream status. */
export interface Withings {
  connection: Resource<Connection | null>
  status: Resource<Status>
  reload: () => void
}

export function useWithings(): Withings {
  const connection = useResource('/api/connections', decodeWithings)
  const status = useResource('/api/status', decodeStatus)
  const reloadConnection = connection.reload
  const reloadStatus = status.reload
  const reload = useCallback(() => {
    reloadConnection()
    reloadStatus()
  }, [reloadConnection, reloadStatus])
  return { connection, status, reload }
}
