"""TOTP and recovery-code primitives, without the database (ADR 0003 section 7).

RFC 6238 through ``pyotp``: SHA-1, 6 digits, a 30 s period, a 160-bit secret and a tolerance of
one step either side. ``match_step`` returns *which* step a code belongs to, because replay
protection is a conditional ``UPDATE`` on that step (``ownvitals.auth.mfa``). The QR code is an
SVG rendered here with ``segno`` (pure Python): nothing is fetched from a third-party host.
"""

import hmac
import io
import re
import secrets
import time
from collections.abc import Callable

import pyotp
import segno

PERIOD = 30
DIGITS = 6
WINDOW = 1  # steps of tolerance either side
SECRET_LENGTH = 32  # base32 characters = 160 bits
ISSUER = "ownvitals"

RECOVERY_CODE_COUNT = 10
RECOVERY_CODE_LENGTH = 10
_RECOVERY_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
_TOTP_CODE = re.compile(r"[0-9]{6}")

# The clock is a module attribute so tests can move it; production uses the system clock.
clock: Callable[[], float] = time.time


def new_secret() -> str:
    """A fresh 160-bit base32 secret."""
    return pyotp.random_base32(SECRET_LENGTH)


def provisioning_uri(secret: str, account: str) -> str:
    return pyotp.TOTP(secret, digits=DIGITS, interval=PERIOD).provisioning_uri(
        name=account, issuer_name=ISSUER
    )


def qr_svg(uri: str) -> str:
    """The ``otpauth://`` URI as an SVG document (no XML declaration, no external references)."""
    buffer = io.BytesIO()
    segno.make(uri, error="m").save(  # pyright: ignore[reportUnknownMemberType]
        buffer, kind="svg", xmldecl=False, svgns=True, scale=4, border=2, dark="#000", light="#fff"
    )
    return buffer.getvalue().decode()


def is_totp_code(value: str) -> bool:
    """Whether ``value`` has the shape of a TOTP code (else it is tried as a recovery code)."""
    return _TOTP_CODE.fullmatch(value) is not None


def match_step(secret: str, code: str) -> int | None:
    """The step (``unix time // 30``) whose code equals ``code`` within one step of now, or
    ``None``. Every candidate is compared in constant time; the latest matching step wins."""
    if not is_totp_code(code):
        return None
    current = int(clock() // PERIOD)
    generator = pyotp.TOTP(secret, digits=DIGITS, interval=PERIOD)
    matched: int | None = None
    for step in range(current - WINDOW, current + WINDOW + 1):
        expected = generator.at(step * PERIOD)
        if hmac.compare_digest(expected.encode(), code.encode()):
            matched = step
    return matched


def code_for_step(secret: str, step: int) -> str:
    """The code for ``step`` (tests and nothing else)."""
    return pyotp.TOTP(secret, digits=DIGITS, interval=PERIOD).at(step * PERIOD)


def new_recovery_codes() -> list[str]:
    return [
        "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(RECOVERY_CODE_LENGTH))
        for _ in range(RECOVERY_CODE_COUNT)
    ]


def normalize_recovery_code(value: str) -> str:
    """Upper case, without spaces and hyphens (people copy them in groups)."""
    return re.sub(r"[\s-]", "", value).upper()
