import { describe, expect, it } from 'vitest'
import { stillImporting } from './connectionModel'

describe('stillImporting', () => {
  it('treats a connection with no streams yet as still importing', () => {
    expect(stillImporting([])).toBe(true)
  })
  it('is true while any stream imports or has an unknown state', () => {
    expect(stillImporting([{ state: 'up_to_date' }, { state: 'importing' }])).toBe(true)
    expect(stillImporting([{ state: 'something_new' }])).toBe(true)
  })
  it('is false once every stream has settled', () => {
    expect(stillImporting([{ state: 'up_to_date' }, { state: 'not_shared' }])).toBe(false)
  })
})
