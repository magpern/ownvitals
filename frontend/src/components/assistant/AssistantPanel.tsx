import { useCallback, useEffect, useState } from 'react'
import type { AiConsent } from '../../api/ai'
import { createConversation, getBudget, listConversations, type Budget, type Conversation } from '../../api/assistant'
import { errorMessage } from '../../api/messages'
import { useT } from '../../i18n'
import { AiSection } from '../../pages/settings/AiSection'
import { useQuery, useRouter } from '../../router/Router'
import { Notice } from '../forms'
import { Card } from '../ui/Card'
import { ConversationList, conversationUrl } from './ConversationList'
import { ConversationPane } from './ConversationPane'
import { DigestView } from './DigestView'
import '../../styles/findings.css'
import '../../styles/assistant.css'

// The assistant page (issue 208; docs/design/README.md, "Assistant"). The AI row of Settings sits
// on top: it shows the consent form when consent is not given (step-up included) and withdrawal
// when it is. Only with a given consent and AI available does the conversation grid appear: the
// list (left) and the chosen conversation (right, `?c=` in the address). A withdrawal deletes
// every conversation on the server; the grid then goes, and the consent form comes back. The
// weekly digest (issue 209) sits under the grid while consent is given.

export function AssistantPanel() {
  const t = useT()
  const query = useQuery()
  const { navigate } = useRouter()
  const [consent, setConsent] = useState<AiConsent | null>(null)
  const [conversations, setConversations] = useState<Conversation[] | null>(null)
  const [budget, setBudget] = useState<Budget | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [deleted, setDeleted] = useState(false)
  const on = consent !== null && consent.enabled && consent.state === 'given'

  const refresh = useCallback(() => {
    listConversations().then(setConversations, (caught: unknown) => {
      setError(errorMessage(caught))
    })
    getBudget().then(setBudget, () => {
      setBudget(null)
    })
  }, [])

  useEffect(() => {
    if (on) refresh()
  }, [on, refresh])

  const chosen = query.get('c') ?? conversations?.[0]?.id ?? null
  const start = async () => {
    setError(null)
    setDeleted(false)
    try {
      const created = await createConversation()
      refresh()
      navigate(conversationUrl(created.id))
    } catch (caught) {
      setError(errorMessage(caught))
    }
  }

  return (
    <div className="ov-stack" data-gap="lg">
      <Card>
        <AiSection available embedded onChange={setConsent} />
      </Card>
      {error !== null ? <Notice kind="error">{error}</Notice> : null}
      {deleted ? <Notice kind="success">{t('assistant.delete.done')}</Notice> : null}
      {on && conversations !== null ? (
        <div className="ov-findings-grid">
          <ConversationList conversations={conversations} chosen={chosen} canStart onNew={() => void start()} />
          <div className="ov-findings-grid__detail">
            {chosen === null ? (
              <p className="ov-muted">{t('assistant.choose')}</p>
            ) : (
              <ConversationPane
                key={chosen}
                id={chosen}
                budget={budget}
                onChanged={refresh}
                onDeleted={() => {
                  setDeleted(true)
                  refresh()
                  navigate('/assistant', { replace: true })
                }}
              />
            )}
          </div>
        </div>
      ) : null}
      {on ? <DigestView /> : null}
    </div>
  )
}
