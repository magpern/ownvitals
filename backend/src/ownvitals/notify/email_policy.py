"""Which mail a user may switch off (roadmap Amendment 7, ADR 0019; #459). Pure: no I/O.

**Suppressible** (``SUPPRESSIBLE_KINDS``): the outbox kinds that tell the user something is
waiting in the app. Each has an email preference (``email_prefs``, default on), and every mail
of one carries the signed unsubscribe link and the RFC 8058 headers.

**Never suppressible, explicitly:**

- ``export_ready`` (``ALWAYS_SENT_KINDS``): the user asked for the export minutes before, and
  ADR 0013 section 4 offers no opt-out for it;
- every account and security mail (sign-up and email-change confirmations, password reset,
  step-up and sign-in notices, the passkey, TOTP, QR and MFA-failure notices, the operator's
  reconnect mail): ``ownvitals.auth.mails`` and ``ownvitals.keys_cli`` send them directly
  through ``ownvitals.mail``, never through the outbox, and nothing on their path reads a
  preference or adds an unsubscribe link;
- owner notices (``owner_notices``): mail to the admins about the service, not to a user.

The database's check on ``email_prefs.kind`` admits exactly ``SUPPRESSIBLE_KINDS``
(migration ``0029_email_prefs``), and so do the two definer functions of
``docker/postgres/notify/50-outbox.sql``.
"""

from typing import Final, Literal, get_args

# APPEND-ONLY: the position of a kind in this list is its index in every unsubscribe token sent
# (``ownvitals.notify.unsubscribe``). Reordering or removing one would make a link already in an
# inbox name another kind; a new kind goes at the end (``tests/test_notify_unsubscribe_token.py``
# pins the order).
SuppressibleKind = Literal["findings_waiting", "connection_attention", "ai_digest_ready"]

SUPPRESSIBLE_KINDS: Final[tuple[str, ...]] = get_args(SuppressibleKind)
ALWAYS_SENT_KINDS: Final[tuple[str, ...]] = ("export_ready",)


def suppressible(kind: str) -> bool:
    """Whether the user may switch off the email of ``kind``. False for every kind not listed,
    so a new kind is always sent until it is added here on purpose."""
    return kind in SUPPRESSIBLE_KINDS
