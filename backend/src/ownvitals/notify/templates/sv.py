"""The Swedish outbox templates (ADR 0015 section 7). Same keys as ``en``."""

from types import MappingProxyType
from typing import Final

from ownvitals.notify.templates import en
from ownvitals.notify.templates.base import LINK, Template

_FOOTER: Final = (
    "\n\nDet här mejlet säger med avsikt inte mer än så: vad som väntar visas först när du "
    "har loggat in.\n\n- ownvitals\n"
)

TABLE: Final = MappingProxyType(
    {
        "findings_waiting": Template(
            subject="ownvitals: något väntar på dig",
            body="Något nytt väntar på dig i ownvitals.\n\nÖppna det här: " + LINK + _FOOTER,
        ),
        "connection_attention": Template(
            subject="ownvitals: en anslutning behöver din uppmärksamhet",
            body="En av dina anslutningar i ownvitals behöver din uppmärksamhet.\n\nÖppna den här: "
            + LINK
            + _FOOTER,
        ),
        # Still the English copy (``untranslated.UNTRANSLATED_SV``) until the translation PR.
        "export_ready": en.TABLE["export_ready"],
        "ai_digest_ready": Template(
            subject="ownvitals: din veckosammanfattning väntar",
            body="Din veckosammanfattning väntar på dig i ownvitals.\n\nÖppna den här: "
            + LINK
            + _FOOTER,
        ),
        "connection_needs_reconnect": Template(
            subject="ownvitals: något väntar på administrationssidan",
            body="Något väntar på en administratör i ownvitals.\n\nÖppna administrationssidan "
            "här: " + LINK + _FOOTER,
        ),
    }
)
