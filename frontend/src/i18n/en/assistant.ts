// The chat assistant (issue 208; docs/design/README.md, "Assistant"). The `label` key is health
// wording: listed in docs/dev/i18n.md for the owner's medical-claims review at M8.
// sv/assistant.ts satisfies this shape.
export const assistant = {
  'list.label': 'Conversations',
  'list.new': 'New conversation',
  'list.empty': 'No conversations yet.',
  'list.started': 'Started {date}',
  'list.messages': 'Messages: {count}',
  'choose': 'Choose a conversation or start a new one.',
  'empty': 'Ask about your own data, for example how your weight has changed over the last months.',
  'you': 'You',
  'answer': 'Assistant',
  'label': 'AI-generated from your data. It can be wrong, and it is not medical advice.',
  'composer.label': 'Your message',
  'composer.send': 'Send',
  'preparing': 'Preparing an answer…',
  'failed': "The assistant couldn't answer. Try again later.",
  'paused': "This month's AI budget for your account is used up. It resets on {date}.",
  'limit': 'You can send 20 messages an hour. Try again later.',
  'loadFailed': "The conversation couldn't be loaded.",
  'delete.button': 'Delete conversation…',
  'delete.confirm': 'Delete this conversation and all its messages? This cannot be undone.',
  'delete.yes': 'Delete conversation',
  'delete.done': 'Conversation deleted.',
}
