import type { assistant as en } from '../en/assistant'

// Swedish for the chat assistant (glossary: docs/dev/i18n.md). `satisfies` makes a missing or extra key a type error.
export const assistant = {
  'list.label': 'Samtal',
  'list.new': 'Nytt samtal',
  'list.empty': 'Inga samtal än.',
  'list.started': 'Startat {date}',
  'list.messages': 'Meddelanden: {count}',
  'choose': 'Välj ett samtal eller starta ett nytt.',
  'empty': 'Fråga om dina egna data, till exempel hur din vikt har förändrats de senaste månaderna.',
  'you': 'Du',
  'answer': 'Assistent',
  'label': 'AI-genererat från dina data. Det kan vara fel, och det är inte medicinsk rådgivning.',
  'composer.label': 'Ditt meddelande',
  'composer.send': 'Skicka',
  'preparing': 'Förbereder ett svar…',
  'failed': 'Assistenten kunde inte svara. Försök igen senare.',
  'paused': 'Månadens AI-budget för ditt konto är förbrukad. Den återställs {date}.',
  'limit': 'Du kan skicka 20 meddelanden i timmen. Försök igen senare.',
  'loadFailed': 'Samtalet kunde inte läsas in.',
  'delete.button': 'Radera samtal…',
  'delete.confirm': 'Radera det här samtalet och alla dess meddelanden? Det går inte att ångra.',
  'delete.yes': 'Radera samtal',
  'delete.done': 'Samtalet är raderat.',
} satisfies Record<keyof typeof en, string>
