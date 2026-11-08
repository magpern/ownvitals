import type { errors as en } from '../en/errors'

// Swedish for the errors area (glossary: docs/dev/i18n.md). `satisfies` makes a missing or extra key a type error.
export const errors = {
  'reason.invalid_invite': 'Inbjudningskoden är inte giltig, eller så har den redan använts.',
  'reason.invite_invalid': 'Inbjudan till den här registreringen är inte längre giltig. Be om en ny.',
  'reason.signups_closed': 'Det går inte att skapa konton just nu.',
  'reason.password_rule': 'Välj ett lösenord på 12 till 128 tecken.',
  'reason.invalid_credentials': 'E-postadressen eller lösenordet stämmer inte.',
  'reason.wrong_password': 'Det är inte det lösenord du valde när du skapade kontot.',
  'reason.invalid_token': 'Länken är inte längre giltig. Den kan ha gått ut eller redan ha använts.',
  'reason.wrong_browser':
    'Slutför på den enhet och i den webbläsare där du påbörjade inloggningen. Länken fungerar inte här.',
  'reason.account_exists': 'Det finns redan ett konto med den här adressen. Logga in på det i stället.',
  'reason.invalid_code': 'Koden stämmer inte. Kontrollera appen och försök igen.',
  'reason.totp_changed': 'Din tvåfaktorsinställning ändrades under tiden. Börja om.',
  'reason.totp_not_enabled': 'Tvåfaktorsautentisering är inte inställd.',
  'reason.not_authenticated': 'Du är utloggad. Logga in och försök igen.',
  'reason.no_pending_signup':
    'Ingen inloggning väntar på en adress. Starta inloggningen med Google igen.',
  'reason.provider_disabled': 'Den inloggningsmetoden är inte tillgänglig.',
  'reason.forbidden': 'Ditt konto har inte behörighet att göra det.',
  'reason.two_factor_required': 'Ange en kod från din autentiseringsapp för att fortsätta.',
  'reason.step_up_required': 'Det här kräver en ny kod från din autentiseringsapp.',
  'reason.reauth_required': 'Det här kräver ditt lösenord igen.',
  'reason.reauth_stale': 'Inloggningen hos Google var inte tillräckligt färsk. Försök igen.',
  'reason.busy': 'Servern är upptagen. Försök igen om några sekunder.',
  'oauth.invalid': 'Inloggningen kunde inte slutföras. Försök igen.',
  'oauth.denied': 'Inloggningen avbröts hos Google.',
  'oauth.session_mismatch':
    'Du blev utloggad eller bytte konto under inloggningen. Logga in igen.',
  'oauth.account_exists':
    'Det finns redan ett konto med den e-postadressen. Logga in på det med ditt lösenord i stället.',
  'oauth.invite_required':
    'Du behöver en inbjudan för att skapa ett konto. Ange din inbjudningskod nedan och försök igen.',
  'oauth.invite_invalid': 'Den inbjudan är inte längre giltig. Be om en ny.',
  tooMany: 'För många försök. Vänta en stund och försök igen.',
  tooManyWait: 'För många försök. Försök igen om {wait}.',
  'minutes.one': '{count} minut',
  'minutes.other': '{count} minuter',
  'seconds.one': '{count} sekund',
  'seconds.other': '{count} sekunder',
  fields: 'Kontrollera fälten och försök igen.',
  server: 'Något gick fel på servern. Försök igen om en stund.',
  network: 'Det gick inte att nå servern. Kontrollera din anslutning.',
  generic: 'Det fungerade inte. Försök igen.',
} satisfies Record<keyof typeof en, string>
