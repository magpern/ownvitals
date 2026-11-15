import { createContext, useContext } from 'react'
import type { Status } from '../../api/overview'
import type { Resource } from '../../api/useResource'

// The one read of /api/status per frame: the freshness line, the rail's marks and the stream tiles
// of the area pages all show what this single answer says.

export const StatusContext = createContext<Resource<Status> | null>(null)

export function useStatus(): Resource<Status> {
  const value = useContext(StatusContext)
  if (value === null) throw new Error('useStatus needs the app frame')
  return value
}
