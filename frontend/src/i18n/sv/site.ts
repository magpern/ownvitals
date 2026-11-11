import type { site as en } from '../en/site'

// De publika sidorna (/ och /features, ADR 0016): förrenderade, utan skript.
export const site = {
  skip: 'Hoppa till innehållet',
  'nav.label': 'Webbplats',
  'nav.home': 'Översikt',
  'nav.features': 'Funktioner',
  'nav.language': 'Språk',
  'meta.home.title': 'ownvitals: dina Withings-data på en instrumentpanel',
  'meta.home.description':
    'ownvitals är en gratis webbplats som hämtar dina egna Withings-data, håller dem privata, analyserar dem och visar dem på en instrumentpanel.',
  'meta.features.title': 'Funktioner: ownvitals',
  'meta.features.description':
    'Trender, fynd med angiven källa och säkerhet, EKG- och sömnvyer, valfri AI-hjälp samt export och radering av dina data.',
  'meta.app.description': 'En gratis webbplats som visar dina egna Withings-data på en instrumentpanel.',
  'meta.image.alt': 'Översikten i ownvitals med syntetiska exempeldata',
  'home.title': 'Dina Withings-data på en lugn instrumentpanel',
  'home.lead':
    'ownvitals kopplas till ditt eget Withings-konto, hämtar dina mätvärden och visar trender och fynd på ett lugnt och läsbart sätt. Gratis, och dina data förblir dina.',
  'cta.signUp': 'Skapa ett gratis konto',
  'cta.signIn': 'Logga in',
  'cta.features': 'Se alla funktioner',
  'home.points.title': 'Det här får du',
  'home.point.trends.title': 'Trender, inte brus',
  'home.point.trends.text':
    'Vikt, blodtryck, hjärta, sömn och aktivitet över en vecka, en månad eller all tid, med ditt typiska intervall inritat.',
  'home.point.findings.title': 'Fynd med sin källa',
  'home.point.findings.text':
    'Varje fynd anger varifrån regeln kommer, hur säkert det är och hur bra underlaget är.',
  'home.point.yours.title': 'Dina data är dina',
  'home.point.yours.text':
    'Varje konto ser bara sina egna data. Du kan exportera dem eller radera dem. Ingen reklam och ingen webbanalys.',
  'home.shots.title': 'En titt in',
  'home.shots.note': 'Bilderna nedan visar syntetiska exempeldata, ingen verklig person.',
  'shot.dashboard.alt': 'Översikten: poängrutor och trenddiagram för kropp, hjärta, sömn och aktivitet, med syntetiska data',
  'shot.findings.alt': 'Fyndlistan: varje fynd med nivå, säkerhet och datakvalitet, med syntetiska data',
  'shot.connections.alt': 'Anslutningar: Withings-status, hur uppdateringar kommer in och vilka dataströmmar som finns, med syntetiska data',
  'home.end.title': 'Redo att titta på dina egna siffror?',
  'home.end.text': 'Logga in, eller skapa ett konto och anslut Withings. Det tar några minuter.',
  'features.title': 'Funktioner',
  'features.lead': 'Vad ownvitals gör med de data dina Withings-enheter registrerar.',
  'features.overview.title': 'En enda översikt',
  'features.overview.text': 'En sida med läget för kropp, hjärta, sömn och aktivitet, och hur var och en har rört sig på sistone.',
  'features.body.title': 'Kropp',
  'features.body.text': 'Vikt och kroppssammansättning över tid, var och en med ett typiskt intervall och en länk till mätningarna dag för dag.',
  'features.heart.title': 'Hjärta och EKG',
  'features.heart.text': 'Trender för blodtryck och puls, och dina EKG-inspelningar att öppna och läsa, med intervallen visade.',
  'features.sleep.title': 'Sömn och aktivitet',
  'features.sleep.text': 'Hur länge och hur väl du sov, och hur aktiv du var, som trender du kan jämföra med ditt eget förflutna.',
  'features.nerve.title': 'Nervhälsa',
  'features.nerve.text': 'Trender från de nervhälsomätningar din enhet registrerar, visade på samma sätt som allt annat.',
  'features.findings.title': 'Fynd du kan kontrollera',
  'features.findings.text':
    'Varje fynd har en nivå, en källa, en säkerhet och en notering om datakvalitet, och säger vad det inte är. Inget här är en diagnos.',
  'features.connections.title': 'Kopplat till Withings',
  'features.connections.text':
    'Du ansluter ditt eget konto en gång. Nya mätvärden kommer via notiser och en regelbunden synk, och du ser statusen när som helst.',
  'features.assistant.title': 'En valfri assistent',
  'features.assistant.text':
    'Ställ frågor om dina siffror på vanlig svenska. Den är avstängd om den inte slås på, och dina inloggningsuppgifter går aldrig till den.',
  'features.data.title': 'Exportera och radera',
  'features.data.text': 'Ladda ner allt som är sparat om dig, eller radera det, från dina inställningar.',
  'features.security.title': 'Noggrann inloggning',
  'features.security.text': 'Tvåstegsverifiering, passkeys och en lista över var du är inloggad, med en ny kod för skyddade åtgärder.',
  'features.languages.title': 'Svenska och engelska, ljust och mörkt',
  'features.languages.text': 'Välj språk och tema, eller låt enheten avgöra.',
  'footer.disclaimer':
    'ownvitals är ingen medicinteknisk produkt och ger inga medicinska råd, diagnoser eller behandling. Det är ett oberoende projekt, inte knutet till eller godkänt av Withings.',
  'footer.nocookies': 'De här sidorna sätter inga cookies och laddar inget från andra webbplatser.',
} satisfies Record<keyof typeof en, string>
