import { useEffect, useId, useState } from 'react'
import { listAdminAccounts, type AdminAccount } from '../api/admin'
import { errorMessage } from '../api/messages'
import { Notice } from '../components/forms'
import { StatePill } from '../components/ui/DataState'
import { Icon } from '../components/ui/Icon'
import { signedInAt } from './SecurityPage'
import { AdminSection, Overview } from './admin/AdminSections'
import { AdminTabs, useAdminTab } from './admin/AdminTabs'
import { SessionAnomalies } from './admin/SessionAnomalies'

// Admin (docs/design/mockups/html/Admin.html). Issue 127 builds the Accounts table; issue 461 adds
// the session anomalies (pages/admin/SessionAnomalies.tsx, no mock-up: it reuses the table). Issue
// 548 adds the tabs and the read-only Jobs, Flags and Audit log (pages/admin/). Issue 549 adds each
// account's Withings state here, the Overview tiles and the Withings app card. Invites, flag
// changes and "Sign out all" come with the admin writes.

function factor(account: AdminAccount) {
  if (account.secondFactor === 'on') return <StatePill tone="ok">On</StatePill>
  return <StatePill tone="attn">Setup pending</StatePill>
}

// The words of the user's own Connections page; "Healthy" is the mock-up's word for connected.
function withings(account: AdminAccount) {
  if (account.withings === 'healthy') return <StatePill tone="ok">Healthy</StatePill>
  if (account.withings === 'needs_reconnect') return <StatePill tone="urgent">Needs reconnect</StatePill>
  return <StatePill tone="off">Not connected</StatePill>
}

function role(account: AdminAccount): string {
  return account.role === 'admin' ? 'Admin' : 'User'
}

export function AdminPage() {
  const tab = useAdminTab()
  const accounts = (
    <>
      <Accounts />
      <SessionAnomalies />
    </>
  )
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
      <AdminTabs active={tab} />
      {tab === 'overview' ? <Overview accounts={accounts} /> : null}
      {tab === 'accounts' ? accounts : null}
      {tab !== 'overview' && tab !== 'accounts' ? <AdminSection tab={tab} /> : null}
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
                  <td>{withings(account)}</td>
                  <td className={account.lastSignIn === null ? 'ov-muted' : 'ov-num'}>
                    {account.lastSignIn === null ? 'Never' : signedInAt(account.lastSignIn, new Date(), 'en')}
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
