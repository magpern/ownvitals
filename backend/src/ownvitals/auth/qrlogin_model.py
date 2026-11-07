"""QR approve-login: the windows, states and values of a request (ADR 0011 section 5).

Pure: no database, no clock and no randomness. ``qrlogin`` holds the rows' state machine and
re-exports these names.
"""

import base64
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal
from uuid import UUID

from ownvitals.auth.session_model import IssuedSession

SCAN_WINDOW: Final = timedelta(seconds=60)
APPROVE_WINDOW: Final = timedelta(seconds=120)
COMPLETE_WINDOW: Final = timedelta(seconds=30)
# The longest a request can live: scan, approve and complete windows end to end (3.5 minutes).
LONGEST: Final = SCAN_WINDOW + APPROVE_WINDOW + COMPLETE_WINDOW
NONCE_BYTES: Final = 32
NONCE_TEXT: Final = 43  # base64url of 32 bytes, without padding
CHANNEL: Final = "qr_logins"
IP_MAX: Final = 64

State = Literal["pending", "scanned", "approved", "completed", "denied", "expired"]
LIVE: Final = ("pending", "scanned", "approved")
ENDED: Final = frozenset({"approved", "completed", "denied", "expired"})  # the stream stops
FACTORS: Final[dict[str, Literal["totp", "recovery_code"]]] = {
    "totp": "totp",
    "recovery": "recovery_code",
}


@dataclass(frozen=True, slots=True)
class Desktop:
    """What the phone is shown about the desktop: its IP, the browser and OS its
    ``User-Agent`` claims (``useragent``), and its place (``geo``; ``None`` is unknown)."""

    ip: str | None
    browser: str
    os: str
    place: str | None


@dataclass(frozen=True, slots=True)
class Scanned:
    id: UUID
    desktop: Desktop
    requested_at: datetime
    approve_deadline: datetime
    now: datetime


@dataclass(frozen=True, slots=True)
class Approval:
    outcome: Literal["approved", "unavailable", "invalid_code"]
    mail_failures: bool = False  # the fifth failed code in an hour: mail the user


@dataclass(frozen=True, slots=True)
class Completed:
    issued: IssuedSession
    user_id: UUID
    email: str
    role: str
    desktop: Desktop


@dataclass(frozen=True, slots=True)
class StreamState:
    state: State
    seconds_left: float  # until the current state's deadline (0 once ended)


def nonce_hash(nonce: bytes) -> bytes:
    return hashlib.sha256(nonce).digest()


def encode_nonce(nonce: bytes) -> str:
    return base64.urlsafe_b64encode(nonce).rstrip(b"=").decode()


def _desktop(row: Any) -> Desktop:
    return Desktop(row.desktop_ip, row.desktop_browser, row.desktop_os, row.desktop_place)
