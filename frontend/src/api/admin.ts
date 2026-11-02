import { get } from './client'
import { DecodeError, count, list, object, optionalText, text } from './decode'

// The admin Users view (#127). Answers are decoded here; pages only see typed values.
// Synthetic in tests. Nothing here is a measurement.

export interface AdminAccount {
  id: string
  email: string
  role: string
  disabledAt: string | null
  createdAt: string
  secondFactor: 'on' | 'setup_pending'
  lastSignIn: string | null
  sessions: number
}

export interface AdminAccountPage {
  users: AdminAccount[]
  nextOffset: number | null
}

function account(value: unknown): AdminAccount {
  const row = object(value, 'user')
  const factor = text(row, 'second_factor')
  if (factor !== 'on' && factor !== 'setup_pending') {
    throw new DecodeError('second_factor')
  }
  return {
    id: text(row, 'id'),
    email: text(row, 'email'),
    role: text(row, 'role'),
    disabledAt: optionalText(row, 'disabled_at'),
    createdAt: text(row, 'created_at'),
    secondFactor: factor,
    lastSignIn: optionalText(row, 'last_sign_in'),
    sessions: count(row, 'sessions'),
  }
}

export async function listAdminAccounts(signal?: AbortSignal): Promise<AdminAccountPage> {
  const body = object(await get('/api/admin/users', signal), 'users')
  const next = body.next_offset
  return {
    users: list(body, 'users').map(account),
    nextOffset: typeof next === 'number' ? next : null,
  }
}
