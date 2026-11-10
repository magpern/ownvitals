import type { Conversation } from '../../api/assistant'
import { useT } from '../../i18n'
import { useFormat } from '../../i18n/useFormat'
import { browserZone } from '../../lib/format'
import { Link } from '../../router/Router'
import { Button } from '../ui/Button'

// The list side of the assistant (docs/design/README.md, "Assistant"): "New conversation", then
// the user's conversations as finding-card-style links, newest first, the chosen one marked.

export function conversationUrl(id: string): string {
  return `/assistant?c=${encodeURIComponent(id)}`
}

export function ConversationList({
  conversations,
  chosen,
  canStart,
  onNew,
}: {
  conversations: Conversation[]
  chosen: string | null
  canStart: boolean
  onNew: () => void
}) {
  const t = useT()
  const f = useFormat()
  return (
    <div className="ov-findings-grid__list">
      <div className="ov-settings-actions">
        <Button variant="primary" disabled={!canStart} onClick={onNew}>
          {t('assistant.list.new')}
        </Button>
      </div>
      <ul className="ov-flist" aria-label={t('assistant.list.label')}>
        {conversations.length === 0 ? <li className="ov-muted">{t('assistant.list.empty')}</li> : null}
        {conversations.map((conversation) => {
          const selected = conversation.id === chosen
          return (
            <li key={conversation.id}>
              <Link
                to={conversationUrl(conversation.id)}
                className="ov-fcard"
                data-selected={selected ? 'true' : undefined}
                aria-current={selected ? 'true' : undefined}
              >
                <span className="ov-fcard__name">{t('assistant.list.started', { date: f.dateTime(conversation.createdAt, browserZone()) })}</span>
                <span className="ov-fcard__meta ov-muted">{t('assistant.list.messages', { count: f.number(conversation.messages) })}</span>
              </Link>
            </li>
          )
        })}
      </ul>
    </div>
  )
}
