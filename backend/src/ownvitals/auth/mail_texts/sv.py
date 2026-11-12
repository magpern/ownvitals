"""The Swedish texts of the account mails (ADR 0015 section 7). Same keys and placeholders as
``en``; a test checks both. The glossary is in ``docs/dev/i18n.md``."""

from types import MappingProxyType
from typing import Final

TEXTS: Final = MappingProxyType(
    {
        "signup.subject": "Bekräfta ditt ownvitals-konto",
        "signup.body": (
            "Någon har bett om att skapa ett ownvitals-konto med den här adressen.\n\n"
            "För att slutföra, öppna den här länken (giltig i 24 timmar) och skriv det "
            "lösenord du valde:\n"
            "{link}\n\n"
            "Om det inte var du kan du bortse från det här mejlet. Inget konto skapas.\n"
        ),
        "provider_signup.subject": "Bekräfta ditt ownvitals-konto",
        "provider_signup.body": (
            "Någon loggade in med {provider} och bad om att skapa ett ownvitals-konto "
            "med den här adressen.\n\n"
            "För att slutföra, öppna den här länken i samma webbläsare (giltig i 24 timmar):\n"
            "{link}\n\n"
            "Om det inte var du kan du bortse från det här mejlet. Inget konto skapas.\n"
        ),
        "exists.subject": "Du har redan ett ownvitals-konto",
        "exists.body": (
            "Någon har bett om att skapa ett ownvitals-konto med den här adressen, som redan "
            "har ett.\n\n"
            "Logga in: {signin}\n"
            "Glömt lösenordet? {reset}\n\n"
            "Om det inte var du kan du bortse från det här mejlet.\n"
        ),
        "reset.subject": "Återställ ditt ownvitals-lösenord",
        "reset.body": (
            "{intro}För att välja ett nytt lösenord, öppna den här länken (giltig i 30 "
            "minuter):\n"
            "{link}\n\n"
            "Återställningen loggar ut dig överallt. Om det inte var du kan du bortse från "
            "det här mejlet.\n"
            "{passkeys}"
        ),
        "reset.after_mfa_reset": (
            "Operatören har återställt tvåfaktorsautentiseringen för ditt ownvitals-konto: "
            "{removed}. Välj ett nytt lösenord med länken nedan och ställ sedan in "
            "tvåfaktorsautentisering igen.\n\n"
        ),
        "reset.removed.mfa": "din autentiseringsapp och dina återställningskoder togs bort",
        "reset.removed.passkey": "din passkey togs bort",
        "reset.removed.passkeys": "dina passkeys togs bort",
        "reset.removed.google": "inloggningen med Google kopplades bort",
        "reset.removed.password": "ditt lösenord rensades",
        "reset.removed.sessions": "du loggades ut överallt",
        "reset.passkeys_kept": (
            "\nNär lösenordet återställs finns de här passkeys kvar på ditt konto:\n"
            "{names}"
            "Granska dem på säkerhetssidan: {security}\n"
        ),
        "reset.passkey_line": "- {name} (tillagd {date})\n",
        "list.separator": ", ",
        "list.last": " och ",
        "email_change.subject": "Bekräfta din nya e-postadress för ownvitals",
        "email_change.body": (
            "För att använda den här adressen för ditt ownvitals-konto, logga in och öppna "
            "den här länken (giltig i 1 timme):\n{link}\n\n"
            "Om det inte var du kan du bortse från det här mejlet.\n"
        ),
        "email_taken.subject": "Ett ownvitals-konto använder redan den här adressen",
        "email_taken.body": (
            "Någon har bett om att flytta ett ownvitals-konto till den här adressen, som "
            "redan tillhör ett konto.\n\nLogga in: {signin}\n\n"
            "Om det inte var du kan du bortse från det här mejlet.\n"
        ),
        "email_changed.subject": "E-postadressen för ditt ownvitals-konto har ändrats",
        "email_changed.body": (
            "E-postadressen för ditt ownvitals-konto har ändrats. Det här är den gamla "
            "adressen.\n"
            "Om det inte var du, kontakta operatören.\n"
        ),
        "totp_enrolled.subject": "Tvåfaktorsautentisering har ställts in på ditt ownvitals-konto",
        "totp_enrolled.body": (
            "En autentiseringsapp har registrerats (eller bytts ut) på ditt ownvitals-konto.\n"
            "Om det inte var du, återställ ditt lösenord och kontakta operatören.\n"
        ),
        "totp_removed.subject": "Tvåfaktorsautentisering har stängts av på ditt ownvitals-konto",
        "totp_removed.body": (
            "Autentiseringsappen har tagits bort från ditt ownvitals-konto, tillsammans med dess "
            "återställningskoder. Inloggning kräver nu bara ditt lösenord, Google eller en "
            "passkey.\n"
            "Om det inte var du, återställ ditt lösenord, ställ in autentiseringsappen igen och "
            "kontakta operatören.\n"
        ),
        "mfa_failures.subject": "Misslyckade tvåfaktorsförsök på ditt ownvitals-konto",
        "mfa_failures.body": (
            "Det har gjorts fem misslyckade tvåfaktorsförsök på ditt ownvitals-konto inom en "
            "timme.\n"
            "Om det inte var du, byt ditt lösenord.\n"
        ),
        "identity_linked.subject": "En inloggningsmetod har lagts till på ditt ownvitals-konto",
        "identity_linked.body": (
            "{provider} har kopplats till ditt ownvitals-konto som ett sätt att logga in.\n"
            "Om det inte var du, logga in, ta bort den från din kontosida och byt "
            "lösenord.\n"
        ),
        "passkey_added.subject": "En passkey har lagts till på ditt ownvitals-konto",
        "passkey_added.body": (
            'En passkey med namnet "{name}" har lagts till på ditt ownvitals-konto som ett '
            "sätt att logga in.\n"
            "Om det inte var du, logga in, ta bort den på säkerhetssidan och byt "
            "lösenord.\n"
        ),
        "passkey_suspended.subject": (
            "En passkey på ditt ownvitals-konto nekades och har spärrats"
        ),
        "passkey_suspended.body": (
            'Din passkey "{name}" nekades och har spärrats när den användes nyss: dess räknare '
            "gick bakåt, vilket kan betyda att den har kopierats.\n"
            "Den kan inte användas längre. Ta bort den på säkerhetssidan och lägg till den "
            "igen. Om du inte just använde den för att logga in eller bekräfta att det är du, "
            "byt ditt lösenord.\n"
        ),
        "qr_approved.subject": "En ny inloggning på ownvitals godkändes från din telefon",
        "qr_approved.body": (
            "En ny inloggning på ditt ownvitals-konto godkändes från din telefon:\n"
            "{browser} på {system} ({place}), enligt den webbläsaren.\n\n"
            "Om det inte var du, logga ut den enheten och byt ditt lösenord: "
            "{security}\n"
        ),
    }
)
