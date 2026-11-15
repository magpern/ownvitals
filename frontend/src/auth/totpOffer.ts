// Whether this browser session already offered the authenticator app to the signed-in user
// (issue 475: optional for users). A per-tab convenience only: the server decides what the session
// may do. The answer is kept in memory too, so a missing or throwing sessionStorage (private
// windows, blocked site data) never sends the user back to the offer after "Skip for now"; only a
// reload then shows it again.

const KEY = 'ov.totpOffered'
let offeredTo: string | null = null

export function totpOffered(userId: string): boolean {
  if (offeredTo === userId) return true
  try {
    return window.sessionStorage.getItem(KEY) === userId
  } catch {
    return false
  }
}

export function markTotpOffered(userId: string): void {
  offeredTo = userId
  try {
    window.sessionStorage.setItem(KEY, userId)
  } catch {
    // Kept in memory only (above).
  }
}

/** For tests: forget the offer in memory and in storage. */
export function resetTotpOffer(): void {
  offeredTo = null
  try {
    window.sessionStorage.removeItem(KEY)
  } catch {
    // nothing stored
  }
}
