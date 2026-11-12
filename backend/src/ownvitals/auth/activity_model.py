"""What the user's own security activity list shows of an ``auth_events`` row (#251). Pure.

- **Only the kinds a person can act on** (``SHOWN``, an allowlist): sign-ins with their method,
  refused sign-ins and re-authentications, sign-outs (this session, another session, everywhere),
  two-factor, passkey, password, email and Google changes, and the export's and doctor PDF's
  events (ADR 0013 section 5: requested, ready, failed, downloaded, expired), and an unusual use
  of a session (``session_anomaly``, #461). A new kind stays
  hidden until it is named here and worded in the catalogue; operator rows
  (``connections_wiped``, the CLI's role changes) and the QR and step-up bookkeeping stay out.
- **Only codes and allowlisted fields:** the kind (a code), the time, the method (``METHODS``),
  the browser and operating system families (``useragent.BROWSERS``, ``SYSTEMS``) and the place
  text. Never the address, a token, a session or row id, an email or the ``User-Agent`` string,
  and nothing else from ``detail``: the method is the only value read from it, and only as one
  of the fixed names.

The cursor is ``<microseconds since the epoch>_<event id hex>``: the position after the last
row of a page. It only bounds the caller's own query, so a forged or foreign one shows nothing
but the caller's own older rows.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

from ownvitals.auth import geo
from ownvitals.auth.useragent import BROWSERS, SYSTEMS

PASSWORD: Final = "password"  # noqa: S105 - a method name, not a secret
GOOGLE: Final = "google"
PASSKEY: Final = "passkey"
QR: Final = "qr"
METHODS: Final = (PASSWORD, GOOGLE, PASSKEY, QR)

# stored kind -> shown kind. The method comes from ``_method``.
SHOWN: Final[dict[str, str]] = {
    "sign_in": "sign_in",
    "passkey_signin": "sign_in",
    "qr_login_completed": "sign_in",
    "sign_in_failed": "sign_in_failed",
    "passkey_signin_failed": "sign_in_failed",  # or reauth_failed: see ``shown_kind``
    "reauth_failed": "reauth_failed",
    "mfa_failed": "mfa_failed",
    "sign_out": "sign_out",
    "session_signed_out": "session_signed_out",
    "logout_everywhere": "logout_everywhere",
    "totp_enrolled": "totp_enrolled",
    "totp_replaced": "totp_replaced",
    "totp_removed": "totp_removed",
    "recovery_codes_regenerated": "recovery_codes_regenerated",
    "mfa_reset": "mfa_reset",
    "passkey_registered": "passkey_added",
    "passkey_renamed": "passkey_renamed",
    "passkey_removed": "passkey_removed",
    "passkey_suspended": "passkey_suspended",
    "password_changed": "password_changed",
    "password_reset": "password_reset",
    "email_changed": "email_changed",
    "identity_linked": "google_linked",
    "identity_unlinked": "google_unlinked",
    # The export and the doctor PDF (ADR 0013 section 5; #333).
    "export_requested": "export_requested",
    "export_ready": "export_ready",
    "export_failed": "export_failed",
    "export_downloaded": "export_downloaded",
    "export_expired": "export_expired",
    "doctor_pdf_requested": "doctor_pdf_requested",
    "doctor_pdf_downloaded": "doctor_pdf_downloaded",
    # The session anomaly signal (#461, ADR 0020): the origin is the request that looked odd.
    "session_anomaly": "session_anomaly",
}
STORED_KINDS: Final = tuple(sorted(SHOWN))
SHOWN_KINDS: Final = frozenset(SHOWN.values())

# Kinds whose row is not the user's own doing (the operator's reset; an export that the build,
# the expiry or the tidy of another request changed): no origin is shown, even if one was stored.
NO_ORIGIN: Final = frozenset({"mfa_reset", "export_ready", "export_failed", "export_expired"})

PAGE_DEFAULT: Final = 20
PAGE_MAX: Final = 50
CURSOR_PATTERN: Final = r"^[0-9]{1,19}_[0-9a-f]{32}$"
CURSOR_MAX: Final = 52
_CURSOR = re.compile(CURSOR_PATTERN)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)


@dataclass(frozen=True, slots=True)
class StoredEvent:
    """The columns the list reads; ``detail`` never leaves this module."""

    id: UUID
    kind: str
    at: datetime
    detail: Any
    browser: str | None
    os: str | None
    place: str | None


@dataclass(frozen=True, slots=True)
class ActivityEntry:
    kind: str  # one of ``SHOWN_KINDS``
    at: datetime
    method: str | None  # one of ``METHODS``
    browser: str | None  # one of ``useragent.BROWSERS``; None: not recorded
    os: str | None  # one of ``useragent.SYSTEMS``; None: not recorded
    place: str  # display text, ``geo.UNKNOWN`` when not known


def _purpose(detail: Any) -> Any:
    return detail.get("purpose") if isinstance(detail, dict) else None


def shown_kind(kind: str, detail: Any) -> str | None:
    """The shown code of a stored kind, or None for a kind the list does not show."""
    if kind == "passkey_signin_failed" and _purpose(detail) == "reauth":
        return "reauth_failed"
    return SHOWN.get(kind)


def _method(kind: str, detail: Any) -> str | None:
    if kind in ("passkey_signin", "passkey_signin_failed"):
        return PASSKEY
    if kind == "qr_login_completed":
        return QR
    provider = detail.get("provider") if isinstance(detail, dict) else None
    if kind in ("identity_linked", "identity_unlinked"):
        return GOOGLE
    if kind in ("sign_in", "reauth_failed"):
        return GOOGLE if provider == GOOGLE else PASSWORD
    return None


def _allowed(value: str | None, names: tuple[str, ...]) -> str | None:
    if value is None:
        return None
    return value if value in names else "other"


def entry(row: StoredEvent) -> ActivityEntry | None:
    """The shown entry of a row, or None when its kind is not shown."""
    kind = shown_kind(row.kind, row.detail)
    if kind is None:
        return None
    origin = row.kind not in NO_ORIGIN
    place = geo.clean(row.place) if origin and row.place else ""
    return ActivityEntry(
        kind=kind,
        at=row.at,
        method=_method(row.kind, row.detail),
        browser=_allowed(row.browser, BROWSERS) if origin else None,
        os=_allowed(row.os, SYSTEMS) if origin else None,
        place=geo.shown(place or None),
    )


def encode_cursor(at: datetime, event_id: UUID) -> str:
    """The position after ``(at, event_id)``: whole microseconds since the epoch and the id."""
    return f"{(at - _EPOCH) // _MICROSECOND}_{event_id.hex}"


def decode_cursor(value: str) -> tuple[datetime, UUID] | None:
    """The ``(at, id)`` of a cursor, or None when it is not one ``encode_cursor`` could make."""
    if len(value) > CURSOR_MAX or not _CURSOR.fullmatch(value):
        return None
    micros, hex_id = value.split("_")
    try:
        return _EPOCH + int(micros) * _MICROSECOND, UUID(hex=hex_id)
    except OverflowError:
        return None
