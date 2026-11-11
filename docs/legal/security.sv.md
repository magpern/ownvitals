# Säkerhet

> **UTKAST — juridisk grind för ägaren.** Inte granskad av jurist och inte godkänd. Skriven av en agent utifrån vad koden och ADR:erna gör; text inom `[...]` är platshållare som ägaren måste fylla i eller avgöra. Publicera inte förrän ägaren har passerat den juridiska grinden (ADR 0016 punkt 6). Källor: [claims.md](claims.md).

Senast uppdaterad: [datum]. Sidan beskriver vad tjänsten gör i dag. Den påstår ingen certifiering. [Ägaren: bekräfta varje påstående mot den körande driftsättningen före publicering.]

## Inloggning
- Lösenord lagras som argon2id-hashar. Inloggning med Google är valfri.
- **Tvåfaktorsinloggning (TOTP) är obligatorisk.** Återställningskoder lagras hashade och kan användas en gång vardera. Passkeys och godkännande av inloggning från en annan enhet stöds.
- Känsliga åtgärder (koppla eller koppla från Withings, export, radera konto, ändra inloggningsuppgifter, ge AI-samtycke) kräver en ny bekräftelse med andra faktorn.
- Sessioner upphör efter 72 timmar utan aktivitet eller högst 30 dagar; du kan logga ut överallt.

## Din data
- Withings åtkomst- och uppdateringstoken krypteras i vila (AES-256-GCM, med nyckelrotation) och skrivs aldrig till loggar.
- Varje rad hälsodata tillhör en användare och upprätthålls i databasen (radnivåsäkerhet), inte bara i applikationen.
- Tjänsten pratar med Withings och, bara om AI är aktiverat, med modellleverantören, genom ett kontrollerat nätverkslager med fast värdlista. Token och hemligheter skickas aldrig till en modell.
- Loggar innehåller aldrig mätvärden, token eller webhook-hemligheter.
- Webhooks från Withings verifieras och fungerar bara som utlösare för att hämta data.

## Din kontroll
Export, frånkoppling (med Withings-åtkomsten återkallad först) och radering av konto finns i appen. Se [integritetspolicyn](privacy.sv.md).

## Rapportera en sårbarhet
Skriv till [kontaktadress för säkerhet]. Ta inte med verklig hälsodata. Vi bekräftar inom [dagar] och vidtar inga åtgärder mot forskning i god tro. [Ägaren: lägg till policy för ansvarsfull rapportering; underrättelse till myndighet inom 72 timmar vid personuppgiftsincident är en rutin för ägaren att anta.]
