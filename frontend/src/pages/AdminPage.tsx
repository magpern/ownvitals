import { useEffect, useId, useState } from 'react'
import { listAdminAccounts, type AdminAccount } from '../api/admin'
import { errorMessage } from '../api/messages'
import { Notice } from '../components/forms'
import { Card, ComingLater } from '../components/ui/Card'
import { DataState, StatePill } from '../components/ui/DataState'
import { Icon } from '../components/ui/Icon'
import { signedInAt } from './SecurityPage'

// Admin (docs/design/mockups/html/Admin.html). Issue 127 builds the Accounts table. Overview counts,
// jobs, the Withings app, flags, the audit log, invites and "Sign out all" stay "Coming later":
// those writes are not the account helpers, and connection status cannot be read here.

function factor(account: AdminAccount) {
  if (account.secondFactor === 'on') return <StatePill tone="ok">On</StatePill>
  return <StatePill tone="attn">Setup pending</StatePill>
}

function role(account: AdminAccount): string {
  return account.role === 'admin' ? 'Admin' : 'User'
}

export function AdminPage() {
  return (
    <div className="ov-stack" data-gap="lg">
      <p className="ov-admin-band" role="note">
        <span className="ov-admin-band__lead">
          <Icon name="admin" />
          <span>
            <strong>Admin area.</strong> Accounts and system state only. Health data isn't visible
            here, by design.
          </span>
        </span>
        <span>Authenticator valid 12 h · changes need a code from the last 5 min</span>
      </p>
      <Accounts />
      <Card>
        <ComingLater label="Admin">The admin pages come later.</ComingLater>
      </Card>
    </div>
  )
}

function Accounts() {
  const headingId = useId()
  const [rows, setRows] = useState<AdminAccount[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    listAdminAccounts(controller.signal).then(
      (page) => {
        setRows(page.users)
        setError(null)
      },
      (caught: unknown) => {
        if (!controller.signal.aborted) setError(errorMessage(caught))
      },
    )
    return () => {
      controller.abort()
    }
  }, [])

  return (
    <section className="ov-stack" aria-labelledby={headingId}>
      <h2 id={headingId}>Accounts</h2>
      {error !== null ? <Notice kind="error">{error}</Notice> : null}
      {rows !== null && rows.length === 0 ? <p className="ov-muted">No accounts.</p> : null}
      {rows !== null && rows.length > 0 ? (
        <div className="ov-tbl-wrap" tabIndex={0} role="region" aria-label="Accounts">
          <table className="ov-tbl">
            <thead>
              <tr>
                <th>Account</th>
                <th>Role</th>
                <th>Second factor</th>
                <th>Withings</th>
                <th>Last sign-in</th>
                <th className="ov-r">Sessions</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((account) => (
                <tr key={account.id}>
                  <td>
                    {account.email}
                    {account.disabledAt !== null ? <StatePill tone="attn">Disabled</StatePill> : null}
                  </td>
                  <td>{role(account)}</td>
                  <td>{factor(account)}</td>
                  <td>
                    <DataState kind="not-available" />
                  </td>
                  <td className={account.lastSignIn === null ? 'ov-muted' : 'ov-num'}>
                    {account.lastSignIn === null ? 'Never' : signedInAt(account.lastSignIn)}
                  </td>
                  <td className="ov-r ov-num">{account.sessions}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      <p className="ov-small ov-muted">
        Role changes and second-factor resets run from the operator command line on the server,
        never from this page.
      </p>
    </section>
  )
}
