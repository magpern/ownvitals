"""The sign-up mode's stricter-wins rule without a database (ADR 0003 section 6; classbeat ADR
0011 section 7): a stored flag can tighten what the environment allows and never loosen it; a
stored value that does not validate falls back to ``invite`` and is logged by key only; ``open``
exists nowhere until the M8 gate. #10 ports the sign-up mode only; #12 adds the other flags."""

import logging
from typing import Any

import pytest

from ownvitals.flags import core, effective


@pytest.mark.parametrize(
    ("environment", "stored", "expected"),
    [
        ("invite", None, "invite"),
        ("invite", "invite", "invite"),
        ("invite", "closed", "closed"),
        ("closed", None, "closed"),
        ("closed", "invite", "closed"),  # never loosened
        ("closed", "closed", "closed"),
        # An invalid stored value (``open`` is never storable) falls back to ``invite``.
        ("invite", "open", "invite"),
        ("closed", "open", "closed"),
        ("invite", "anything", "invite"),
        ("invite", 3, "invite"),
        ("invite", {"mode": "closed"}, "invite"),
    ],
)
def test_signup_mode_is_the_stricter_of_the_environment_and_the_database(
    environment: Any, stored: Any, expected: str
) -> None:
    assert core.stricter_signup(environment, stored) == expected


@pytest.mark.parametrize("environment", ["open", "public", None])
def test_an_environment_mode_other_than_invite_or_closed_is_refused(environment: Any) -> None:
    """The setting refuses ``open`` (``test_config_milestone.py``); this is the second wall."""
    with pytest.raises(ValueError, match="invite or closed"):
        core.stricter_signup(environment, None)


async def test_the_effective_mode_is_the_environment_until_flags_are_stored() -> None:
    """No stored flags before #12: the environment's mode applies, ``closed`` included."""
    assert await effective.signup_mode(None, "invite") == "invite"  # pyright: ignore[reportArgumentType]
    assert await effective.signup_mode(None, "closed") == "closed"  # pyright: ignore[reportArgumentType]


def test_an_invalid_value_is_logged_by_key_only(caplog: pytest.LogCaptureFixture) -> None:
    secret = "do-not-log-this-value"
    with caplog.at_level(logging.WARNING, logger="ownvitals.flags.core"):
        core.stricter_signup("invite", secret)
    (record,) = caplog.records
    assert "signup_mode" in record.getMessage()
    assert secret not in caplog.text


def test_a_missing_row_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="ownvitals.flags.core"):
        core.stricter_signup("invite", None)
    assert caplog.records == []
