"""The flag rules without a database (ADR 0011 section 7; FL1, #216): the database can tighten
what the environment allows and never loosen it; a stored value that does not validate falls
back to the key's stricter default and is logged by key only."""

import logging
from typing import Any

import pytest

from ownvitals.flags import core

INVITE_ONLY: Any = None  # a missing row


@pytest.mark.parametrize(
    ("environment", "stored", "expected"),
    [
        ("open", None, "open"),
        ("open", "invite", "invite"),  # the database tightens open to invite
        ("open", "closed", "closed"),
        ("invite", None, "invite"),
        ("invite", "invite", "invite"),
        ("invite", "closed", "closed"),
        ("closed", None, "closed"),
        ("closed", "invite", "closed"),  # never loosened
        ("closed", "closed", "closed"),
        # An invalid stored value (``open`` is never storable) falls back to ``invite``.
        ("open", "open", "invite"),
        ("invite", "open", "invite"),
        ("closed", "open", "closed"),
        ("open", "anything", "invite"),
        ("open", 3, "invite"),
        ("open", {"mode": "closed"}, "invite"),
    ],
)
def test_signup_mode_is_the_stricter_of_the_environment_and_the_database(
    environment: Any, stored: Any, expected: str
) -> None:
    """Threats: T94."""
    assert core.stricter_signup(environment, stored) == expected


@pytest.mark.parametrize(
    ("environment_on", "stored", "expected"),
    [
        (False, None, "off"),
        (False, "on", "off"),  # the kill switch beats every stored value
        (False, "granted", "off"),
        (False, "off", "off"),
        (True, None, "granted"),  # the default
        (True, "granted", "granted"),
        (True, "on", "on"),
        (True, "off", "off"),
        (True, "yes", "granted"),  # invalid: the stricter default
        (True, True, "granted"),
        (True, {"x": 1}, "granted"),
    ],
)
def test_song_picking_is_off_under_the_kill_switch_and_defaults_to_granted(
    environment_on: bool, stored: Any, expected: str
) -> None:
    """Threats: T94."""
    assert core.song_picking_setting(environment_on, stored) == expected


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (None, None),
        ({"plan": "instructor", "days": 14}, core.TrialSetting("instructor", 14)),
        ({"plan": "instructor", "days": 0}, None),  # 0 days is off
        ({"plan": "instructor", "days": -1}, None),
        ({"plan": "instructor", "days": 1.5}, None),
        ({"plan": "instructor", "days": True}, None),
        ({"plan": "", "days": 5}, None),
        ({"plan": 3, "days": 5}, None),
        ({"days": 5}, None),
        ("instructor", None),
        ([], None),
    ],
)
def test_trial_is_off_unless_it_validates(stored: Any, expected: Any) -> None:
    assert core.trial_setting(stored) == expected


def text_of(length: int) -> str:
    return "x" * length


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (None, None),
        ({"text": "Back at 9", "level": "info"}, core.Banner("Back at 9", "info")),
        ({"text": "Down", "level": "warning"}, core.Banner("Down", "warning")),
        ({"text": text_of(280), "level": "info"}, core.Banner(text_of(280), "info")),
        ({"text": text_of(281), "level": "info"}, None),  # over the limit
        ({"text": "Hi", "level": "error"}, None),
        ({"text": "Hi"}, None),
        ({"level": "info"}, None),
        ({"text": 5, "level": "info"}, None),
        ({"text": "   ", "level": "info"}, None),  # nothing to show
        ("Hello", None),
    ],
)
def test_banner_is_plain_text_with_a_limit(stored: Any, expected: Any) -> None:
    assert core.banner_setting(stored) == expected


# Text a banner may not hold (#227): format characters (Cf), separators (Zl, Zp), controls (Cc).
NOT_PLAIN_TEXT = [
    "Back at \u202e9 ta kcab",  # right-to-left override
    "Back \u202aat 9",  # left-to-right embedding
    "Back \u2066at\u2069 9",  # left-to-right isolate, pop directional isolate
    "Back at\u200b 9",  # zero-width space
    "Back at\u200d 9",  # zero-width joiner
    "Back at\xad 9",  # soft hyphen
    "\ufeffBack at 9",  # byte-order mark
    "Back at\u2028 9",  # line separator
    "Back at\u2029 9",  # paragraph separator
    "Back at\x85 9",  # NEL, a C1 control character
    "Back at\x1b 9",  # ESC
]
PLAIN_TEXT = ["Vår grupp tränar kl 9", "Café \U0001f6b4 日本語", "á b"]


@pytest.mark.parametrize("text", NOT_PLAIN_TEXT)
def test_a_stored_banner_that_is_not_plain_text_is_not_served(
    text: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The read side refuses it too: a value stored before the write side did, or past it.

    Threats: T95.
    """
    with caplog.at_level(logging.WARNING, logger="ownvitals.flags.core"):
        assert core.banner_setting({"text": text, "level": "info"}) is None
    assert [r.getMessage() for r in caplog.records] == [
        "flag banner holds an invalid value; its stricter default applies"
    ]
    assert text not in caplog.text


@pytest.mark.parametrize("text", NOT_PLAIN_TEXT)
def test_a_banner_that_is_not_plain_text_is_refused_on_write(text: str) -> None:
    """Threats: T95."""
    with pytest.raises(core.FlagRefused) as refused:
        core.validate_for_write("banner", {"text": text, "level": "info"}, trial_days_max=30)
    assert refused.value.reason == "invalid_value"


@pytest.mark.parametrize("text", PLAIN_TEXT)
def test_ordinary_unicode_banner_text_is_kept(text: str) -> None:
    """Letters, combining marks, symbols and emoji are not format characters."""
    value = {"text": text, "level": "warning"}
    assert core.validate_for_write("banner", value, trial_days_max=30) == value
    assert core.banner_setting(value) == core.Banner(text, "warning")


def test_the_banner_text_is_returned_as_text_not_changed() -> None:
    """Threats: T95."""
    markup = "<b>Maintenance</b> <img src=x onerror=alert(1)>"
    shown = core.banner_setting({"text": markup, "level": "info"})
    assert shown is not None
    assert shown.text == markup  # served as data; the page renders it as text


def test_an_invalid_value_is_logged_by_key_only(caplog: pytest.LogCaptureFixture) -> None:
    secret = "do-not-log-this-value"
    with caplog.at_level(logging.WARNING, logger="ownvitals.flags.core"):
        core.stricter_signup("open", secret)
        core.song_picking_setting(True, secret)
        core.trial_setting({"plan": secret, "days": "x"})
        core.banner_setting({"text": secret, "level": "bad"})
    keys = [r.getMessage() for r in caplog.records]
    assert len(keys) == 4
    for key, message in zip(("signup_mode", "song_picking", "trial", "banner"), keys, strict=True):
        assert key in message
    assert secret not in caplog.text


def test_a_missing_row_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="ownvitals.flags.core"):
        core.stricter_signup("open", None)
        core.song_picking_setting(True, None)
        core.trial_setting(None)
        core.banner_setting(None)
    assert caplog.records == []
