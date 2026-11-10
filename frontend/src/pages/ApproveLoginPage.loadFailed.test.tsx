import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { fakeApi } from '../test/fakeApi'
import { renderAt } from '../test/render'

// Issue 304: a session that cannot be read must not leave /qr on "Reading the code". Synthetic.

describe('approve-login when the session cannot be read', () => {
  it('leaves "Reading the code" and says so', async () => {
    fakeApi({ 'GET /api/auth/session': () => new Response('{}', { status: 500 }) })
    renderAt('/qr#n0nce-made-up-for-the-tests_only-synthetic0')
    expect(await screen.findByRole('heading', { name: 'Cannot reach the server' })).toBeInTheDocument()
    expect(screen.queryByText('Reading the code…')).toBeNull()
  })
})
