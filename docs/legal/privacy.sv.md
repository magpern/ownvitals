# Integritetspolicy

> **UTKAST — juridisk grind för ägaren.** Inte granskad av jurist och inte godkänd. Skriven av en agent utifrån vad koden och ADR:erna gör; text inom `[...]` är platshållare som ägaren måste fylla i eller avgöra. Publicera inte förrän ägaren har passerat den juridiska grinden (ADR 0016 punkt 6). Källor: [claims.md](claims.md).

Senast uppdaterad: [datum]. Gäller [tjänstens namn, t.ex. ownvitals] på [publikt värdnamn].

## 1. Vem ansvarar (personuppgiftsansvarig)

[Namn eller organisation, postadress, organisationsnummer om det finns]. Kontakt för integritetsfrågor och för att utöva dina rättigheter: [kontaktadress för integritet]. [Dataskyddsombud: inget utsett / namn och kontakt.]

## 2. Vad tjänsten gör

Tjänsten kopplas på din begäran till ditt Withings-konto, hämtar dina mätningar, lagrar och analyserar dem och visar dem på en översikt. Den ger inga medicinska råd, ingen diagnos och ingen behandling, och den är inte godkänd eller rekommenderad av Withings.

## 3. Vad vi lagrar

- **Konto:** e-postadress, ett lösenordshash (aldrig lösenordet), inloggningssätt (lösenord och/eller Google; för Google lagrar vi Googles kontoidentifierare, inte din Google-profil), hemligheter för tvåfaktorsinloggning och hashar av återställningskoder, publika nycklar för passkeys om du lägger till sådana, språk och visningsinställningar, födelseår och kön (används bara för referensintervall i poäng), en inställning för dagstilldelning.
- **Withings-data du kopplar:** ditt Withings-användar-id, anslutningens status och behörigheter; åtkomst- och uppdateringstoken (lagras krypterade); enheter; mätningar (till exempel vikt, kroppssammansättning, blodtryck, puls, temperatur), sömn, aktivitet och träningspass, EKG-inspelningar inklusive signalen samt de råa data vi tar emot från Withings.
- **Härledd data vi beräknar:** dagsvärden, fynd, poäng och deras ursprung.
- **Säkerhetsloggar:** inloggningshändelser (typ, tid, IP-adress, ungefärlig plats) och kända enheter, för att skydda ditt konto och visa dig din egen aktivitet.
- **Om du slår på AI-insikter:** ditt samtycke, dina samtal med assistenten och veckosammanfattningar samt en maskerad användningslogg (se avsnitt 8).

Hälsodata är en särskild kategori av personuppgifter enligt artikel 9 i GDPR.

## 4. Varför vi använder den och rättslig grund

| Ändamål | Data | Rättslig grund |
|---|---|---|
| Hämta, lagra, analysera och visa din Withings-hälsodata | Avsnitt 3, Withings-data och härledd data | [Ägaren: M8-grindpunkt, PLAN-operations 9.3: uttryckligt samtycke enligt GDPR art. 6.1 a och 9.2 a och hur det ges och återkallas. Appen har inget sådant samtyckessteg ännu; texten här måste följa det som byggs.] |
| Tillhandahålla ditt konto, logga in dig, hålla det säkert | Konto och säkerhetsloggar | Avtal (art. 6.1 b) och vårt berättigade intresse av säkerhet (art. 6.1 f) |
| Skicka tjänstemejl (verifiering, säkerhetsaviseringar, export klar, fyndaviseringar) | E-postadress (mejl innehåller också länktext, ett passkey-namn eller inloggningens webbläsare och plats i säkerhetsaviseringar, aldrig mätvärden) | Avtal och berättigat intresse [ägaren: bekräfta; fyndaviseringar kan kräva en inställning du styr] |
| AI-insikter | Sammanfattningar av din data (avsnitt 8) | Ditt **separata uttryckliga samtycke**, art. 6.1 a och 9.2 a, bara om du slår på det |

[Ägaren: bekräfta att samtycke är rätt grund för alla ändamål med hälsodata och att bedömningen av privat/icke-privat behandling är gjord.]

## 5. Vem får uppgifterna (personuppgiftsbiträden och mottagare)

- **Withings** är källan till din data. Vi tar emot data därifrån; vi skickar dit bara de anrop som behövs för att hämta din data och dra tillbaka vår åtkomst.
- **Google** bara om du väljer "Logga in med Google": anrop för att verifiera inloggningen.
- **Drift:** [leverantör och plats; servern drivs av den ansvarige / av en leverantör med biträdesavtal].
- **E-postutskick:** [leverantör, plats, biträdesavtal]. Mejl innehåller inga hälsomätvärden.
- **Säkerhetskopior:** [plats och leverantör; krypterade].
- **Anthropic** (modellleverantör) **bara om du aktiverar AI-insikter** och samtycker (avsnitt 8).
- **Cloudflare** (robotkontrollen Turnstile) **bara om den som driver tjänsten slår på den** (avstängd som standard): i formulären för inloggning, registrering och återställning av lösenord får Cloudflare din IP-adress och signaler från webbläsaren för att skilja en människa från ett skript, och vi skickar dit kontrollens engångstoken (aldrig din e-post, ditt lösenord eller hälsodata). [Ägaren: biträdesvillkor och kontroll av tredjelandsöverföring innan den slås på.]
- Ingen analys-, reklam- eller spårningstjänst. Vi säljer inte data.

[Ägaren: komplettera och bifoga biträdesavtal; kontrollera tredjelandsöverföringar för varje biträde.]

## 6. Hur länge vi sparar uppgifterna

- Din Withings-data, härledda data och kontodata sparas tills du raderar dem eller ditt konto.
- Inloggningshändelser och inbjudningar: 30 dagar. Kända enheter: 90 dagar efter senaste användning. IP-adresser i sessioner: 30 dagar. Sessioner upphör efter 72 timmar utan aktivitet och senast 30 dagar efter inloggning.
- Exportfiler upphör 24 timmar efter att de blivit klara; den tomma posten för en utgången export sparas 90 dagar.
- AI-samtal: raderas efter 90 dagar, eller direkt när du återkallar AI-samtycket. Den maskerade AI-användningsloggen: 12 månader.
- Säkerhetskopior: [lagringstid; data som raderats från live-system finns kvar i krypterade kopior tills de löper ut].

## 7. Dina rättigheter och hur du utövar dem

Du har rätt till tillgång, rättelse, radering, begränsning, dataportabilitet, invändning och att när som helst återkalla samtycke, utan att tidigare behandling påverkas.

- **Tillgång och portabilitet:** Inställningar, export. Du kan ladda ned din data som JSON och CSV (kräver ny tvåfaktorsbekräftelse).
- **Sluta hämta:** koppla från Withings i appen. Vi drar först tillbaka vår åtkomst hos Withings. En vanlig frånkoppling behåller redan importerad data; för att ta bort den väljer du att radera importerad data vid frånkopplingen, eller raderar ditt konto. [Ägaren: beskriv återkallelse av samtycke när samtyckesteget finns.]
- **Radering:** radera ditt konto i appen (kräver ny tvåfaktorsbekräftelse). Vi drar tillbaka Withings-åtkomsten och tar därefter bort din data. Misslyckas återkallelsen ber appen dig ta bort åtkomsten i ditt Withings-konto.
- **Övrigt:** skriv till [kontaktadress för integritet]. Vi svarar inom en månad.
- **Klagomål:** till Integritetsskyddsmyndigheten (IMY), imy.se, eller tillsynsmyndigheten där du bor. [Ägaren: bekräfta myndighet.]

## 8. AI-insikter (avstängt som standard)

AI är **avstängt** om inte driftansvarig har aktiverat det och du har gett ett separat, uttryckligt samtycke (med ny tvåfaktorsbekräftelse). När det är på skickar tjänsten beräknade sammanfattningar (till exempel medianer, trender, dagsvärden i visningsprecision) till Anthropics API (api.anthropic.com) så att en modell kan skriva en sammanfattning eller svara på dina frågor. Skickas inte: token, e-post, namn, användar-id, rådata, EKG-signaler, tider på dygnet. Svar märks som AI-genererade, kan vara fel och är inte medicinska råd. [Ägaren: lägg till Anthropics lagringsvillkor enligt leverantörsgranskningen och eventuell överföringsmekanism.] Återkallat samtycke stoppar användningen och raderar dina samtal och sammanfattningar.

## 9. Säkerhet

Se [säkerhetssidan](security.sv.md). Withings-token krypteras i vila. [Ägaren: bekräfta före publicering: att trafiken krypteras under överföring (TLS) i driftsättningen, samt eventuell disk- eller databaskryptering.]

## 10. Cookies

Bara nödvändiga cookies används, när du öppnar appen, loggar in eller börjar logga in; inga på publika sidor. Om den som driver tjänsten slår på robotkontrollen Turnstile kan Cloudflare sätta en egen cookie i inloggningsformulären. Se [cookie-information](cookies.sv.md).

## 11. Ändringar

Ändringar publiceras här med nytt datum och, vid väsentliga ändringar, meddelas inloggade användare.
