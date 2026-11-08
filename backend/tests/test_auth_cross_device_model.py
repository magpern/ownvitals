"""The pure cross-device rules (ADR 0011 section 3, amendment of 2026-11-08; #252): which
sign-in counts as cross-device, the idle limit per origin, and no QR approval from one."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from ownvitals.auth.passkey_model import is_cross_device
from ownvitals.auth.session_model import (
    CROSS_DEVICE,
    CROSS_DEVICE_ABSOLUTE,
    CROSS_DEVICE_IDLE,
    SessionInfo,
    idle_limit,
)

INTERNAL = ("internal", "hybrid")


@pytest.mark.parametrize(
    ("attachment", "transports", "expected"),
    [
        ("platform", INTERNAL, False),
        ("platform", ("internal",), False),
        ("cross-platform", INTERNAL, True),  # a phone over the hybrid transport
        ("cross-platform", ("usb",), True),  # a security key
        (None, INTERNAL, True),  # no attachment reported: fail closed
        ("platform", ("hybrid",), True),  # the claim contradicts the transports
        ("platform", ("usb", "nfc"), True),
        ("platform", (), True),  # nothing registered to back the claim
        ("Platform", INTERNAL, True),  # only the exact value counts
    ],
)
def test_cross_device_detection(
    attachment: str | None, transports: tuple[str, ...], expected: bool
) -> None:
    assert is_cross_device(attachment, transports) is expected


def test_the_limits() -> None:
    assert timedelta(hours=24) == CROSS_DEVICE_ABSOLUTE
    assert timedelta(hours=1) == CROSS_DEVICE_IDLE


def test_the_idle_limit_per_origin() -> None:
    hours72 = timedelta(hours=72)
    assert idle_limit(hours72, "signin") == hours72
    assert idle_limit(hours72, "qr_approval") == hours72
    assert idle_limit(hours72, CROSS_DEVICE) == CROSS_DEVICE_IDLE
    # A setting that is already shorter wins.
    assert idle_limit(timedelta(minutes=30), CROSS_DEVICE) == timedelta(minutes=30)


def _info(origin: str) -> SessionInfo:
    now = datetime(2026, 11, 8, 12, tzinfo=UTC)
    return SessionInfo(
        id=uuid4(),
        user_id=uuid4(),
        email="user@example.com",
        stored_role="user",
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(hours=1),
        reauth_at=now,
        mfa_at=now,
        stepup_at=None,
        now=now,
        totp_enabled=True,
        origin=origin,  # pyright: ignore[reportArgumentType]
    )


def test_a_cross_device_session_may_not_approve_a_qr_login() -> None:
    assert _info("signin").may_approve_qr
    assert not _info(CROSS_DEVICE).may_approve_qr
