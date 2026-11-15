"""The AI consent text and its fingerprint (ADR 0014 section 3). Pure: no I/O.

The text is a document the user agrees to, so it lives here as code, one copy per language, and
the consent API serves it as it is; the SHA-256 of the exact UTF-8 bytes shown is what a consent
row records. A change to any text is a new ``CONSENT_VERSION``: every user must consent again
before the next model call (``ai.consent``).

What it says (ADR 0014 section 3): what is sent and to whom, that the provider is outside
ownvitals, retention (section 2's answer: a placeholder until the owner's provider review is
recorded; AI stays off until then, lock 2, and the recorded answer comes in a new version),
that consent can be withdrawn and what that deletes, and that answers can be wrong and are not
medical advice.
"""

import hashlib
from types import MappingProxyType
from typing import Final

CONSENT_VERSION: Final = 1

_EN = """\
AI insights send parts of your health data to Anthropic, a company outside ownvitals, so that \
its Claude model can answer your questions about your own data.

What is sent: only what a question needs, computed from your data: summaries such as medians, \
typical ranges and trends, daily values for at most 90 days, what the analysis found and your \
scores. Never your name, email, Withings account, device identifiers, raw ECG signals or the \
time of day of a reading.

Retention: Anthropic keeps what it receives under its own terms. What ownvitals has already \
sent cannot be called back by ownvitals.

You can withdraw this consent at any time in Settings. Withdrawing stops AI processing at once \
and deletes your AI conversation history in ownvitals.

AI answers can be wrong. They are not medical advice and do not replace a doctor. For anything \
urgent, contact a doctor or emergency services.
"""

_SV = """\
AI-insikter skickar delar av dina hälsodata till Anthropic, ett företag utanför ownvitals, så \
att dess Claude-modell kan svara på dina frågor om dina egna data.

Vad som skickas: bara det en fråga behöver, beräknat från dina data: sammanfattningar som \
medianer, typiska intervall och trender, dagliga värden för högst 90 dagar, vad analysen har \
hittat och dina poäng. Aldrig ditt namn, din e-post, ditt Withings-konto, enhetsidentiteter, \
råa EKG-signaler eller tiden på dagen för en mätning.

Lagring: Anthropic sparar det som tas emot enligt sina egna villkor. Det som ownvitals redan \
har skickat kan ownvitals inte kalla tillbaka.

Du kan när som helst återkalla samtycket under Inställningar. Då stoppas AI-behandlingen direkt \
och din AI-konversationshistorik i ownvitals raderas.

AI-svar kan vara fel. De är inte medicinska råd och ersätter inte en läkare. Vid något akut, \
kontakta en läkare eller larmnumret.
"""

TEXTS: Final = MappingProxyType({"en": _EN, "sv": _SV})
DEFAULT_LOCALE: Final = "en"


def text_for(locale: str | None) -> tuple[str, str]:
    """``(locale, text)`` of the current version: the requested language, else English."""
    chosen = locale if locale is not None and locale in TEXTS else DEFAULT_LOCALE
    return chosen, TEXTS[chosen]


def fingerprint(text: str) -> bytes:
    """SHA-256 of the exact UTF-8 bytes of ``text``."""
    return hashlib.sha256(text.encode("utf-8")).digest()


def matches(version: int, locale: str, digest: bytes) -> bool:
    """Whether ``(version, locale, digest)`` is the current text in that language."""
    text = TEXTS.get(locale)
    return version == CONSENT_VERSION and text is not None and digest == fingerprint(text)
