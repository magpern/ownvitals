# Cookie-information

> **UTKAST — juridisk grind för ägaren.** Inte granskad av jurist och inte godkänd. Skriven av en agent utifrån vad koden och ADR:erna gör; text inom `[...]` är platshållare som ägaren måste fylla i eller avgöra. Publicera inte förrän ägaren har passerat den juridiska grinden (ADR 0016 punkt 6). Källor: [claims.md](claims.md).

Senast uppdaterad: [datum].

**Sajtens publika sidor sätter inga cookies, använder ingen analys, reklam eller spårning och laddar inget innehåll från tredje part.**

När du öppnar appen (även inloggningssidan), loggar in eller börjar logga in sätter appen bara strikt nödvändiga cookies (ingen samtyckesruta behövs för dessa). Alla är förstapartscookies, `Secure` och utan `Domain`.

| Cookie | Ändamål | Livslängd |
|---|---|---|
| `__Host-ownvitals_session` | Håller dig inloggad (HttpOnly) | Högst 30 dagar; upphör efter 72 timmar utan aktivitet |
| `__Host-ownvitals_device` | Känner igen en känd enhet för inloggningssäkerhet (HttpOnly) | 365 dagar |
| `__Host-ownvitals_csrf` | Skyddar formulär mot förfrågningar från andra sajter; sätts när appen laddas medan du är utloggad | 30 dagar |
| `__Host-ownvitals_oauth` | Bär steget vid inloggning med Google (HttpOnly) | 10 minuter |
| `__Host-ownvitals_pending` | Bär ett pågående registreringssteg (HttpOnly) | 24 timmar |
| `__Host-ownvitals_pre` | Bär steget innan en session finns (HttpOnly) | 5 minuter |

**Bara om den som driver tjänsten slår på robotkontrollen (Cloudflare Turnstile; avstängd som standard):** formulären för inloggning, registrering och återställning av lösenord visar Cloudflares Turnstile-kontroll. Den körs i en ram från Cloudflare, som kan sätta en egen strikt nödvändig cookie (till exempel `cf_clearance`) och läsa tekniska signaler från din webbläsare för att skilja en människa från ett automatiskt skript. Cloudflare får då också din IP-adress och uppgifter om webbläsaren. [Ägaren: bekräfta cookienamn och livslängder i Cloudflares aktuella dokumentation, och om detta räknas som strikt nödvändigt, innan kontrollen slås på.]

Appen lagrar också ditt val av ljust/mörkt tema i webbläsarens lokala lagring (nyckel `ownvitals.theme`), ditt språkval (nyckel `ov-locale`) och att du har sett cookie-informationen (nyckel `ownvitals.cookie-notice`, värdet är ett versionsnummer); det är inställningar som ligger på din enhet och aldrig skickas till oss. Du kan radera cookies och webbplatsdata i webbläsaren; du loggas då ut. [Ägaren: bekräfta att appen inte lagrar något annat i webbläsaren före publicering.]
