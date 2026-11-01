"""The sign-up mode's stricter-wins rule without a database (ADR 0003 section 6; classbeat ADR
0011 section 7): a stored flag can tighten what the environment allows and never loosen it; a
stored value that does not validate falls back to ``invite`` and is logged by key only; ``open``
exists nowhere until the M8 gate. #12 adds the AI and experimental switches, the banner and the
write-side validation; ``test_flags_db.py`` reads them from the database."""

import logging
from pathlib import Path
from typing import Any

import pytest

from ownvitals.flags import core


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


@pytest.mark.parametrize("key", ["experimental_findings", "ai_insights"])
@pytest.mark.parametrize(
    ("environment", "stored", "expected"),
    [
        (False, None, False),  # default off
        (False, "on", False),  # a stored ``on`` never loosens the environment
        (False, "off", False),
        (True, None, True),  # no row: the environment's value
        (True, "on", True),
        (True, "off", False),  # the database tightens
        # An invalid stored value falls back to the stricter default, off.
        (True, "granted", False),
        (True, True, False),
        (True, {"on": True}, False),
        (True, "ON", False),
    ],
)
def test_switches_are_on_only_when_both_allow(
    key: str, environment: bool, stored: Any, expected: bool
) -> None:
    assert core.stricter_switch(key, environment, stored) is expected


def test_a_switch_refuses_an_unknown_key_or_a_non_bool_environment() -> None:
    with pytest.raises(ValueError, match="not a switch"):
        core.stricter_switch("signup_mode", True, None)
    with pytest.raises(ValueError, match="bool"):
        core.stricter_switch("ai_insights", "on", None)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (None, None),
        ({"text": "Hello", "level": "info"}, core.Banner("Hello", "info")),
        ({"text": "   ", "level": "info"}, None),
        ({"text": "x" * 281, "level": "info"}, None),
        ({"text": "a\u202eb", "level": "info"}, None),  # a bidi override
        ({"text": "a\u200bb", "level": "info"}, None),  # a zero-width space
        ({"text": "a\nb", "level": "info"}, None),
        ({"text": "ok", "level": "critical"}, None),
        ({"text": "ok", "level": "info", "extra": 1}, None),
        ("Hello", None),
    ],
)
def test_the_banner_falls_back_to_none_when_invalid(stored: Any, expected: Any) -> None:
    assert core.banner_setting(stored) == expected


REFUSED = ("environment_only", "invalid_value", "unknown_key")


@pytest.mark.parametrize(
    ("key", "value", "result"),
    [
        ("signup_mode", "closed", "closed"),
        ("signup_mode", "open", "environment_only"),
        ("signup_mode", ["closed"], "invalid_value"),
        ("experimental_findings", "on", "on"),
        ("ai_insights", "off", "off"),
        ("ai_insights", False, "invalid_value"),
        ("banner", {"text": "Hi", "level": "warning"}, {"text": "Hi", "level": "warning"}),
        ("banner", {"text": "Hi"}, "invalid_value"),
        ("banner", {"text": "a\u2066b", "level": "info"}, "invalid_value"),
        ("plans", "free", "unknown_key"),
        ("", "x", "unknown_key"),
    ],
)
def test_the_write_side_refuses_what_the_read_side_would_fall_back_from(
    key: str, value: Any, result: Any
) -> None:
    if result in REFUSED:
        with pytest.raises(core.FlagRefused) as refused:
            core.validate_for_write(key, value)
        assert refused.value.reason == result
    else:
        assert core.validate_for_write(key, value) == result


def test_the_keys_match_the_table_check() -> None:
    versions = Path(__file__).resolve().parent.parent / "migrations" / "versions"
    source = (versions / "0006_admin.py").read_text("utf-8")
    listed = ", ".join(f"'{k}'" for k in core.KEYS)
    assert f'SETTING_KEYS = "{listed}"' in source


@pytest.mark.parametrize("key", ["experimental_findings", "ai_insights", "banner"])
def test_an_invalid_stored_value_is_logged_by_key_only(
    key: str, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "do-not-log-this-value"
    with caplog.at_level(logging.WARNING, logger="ownvitals.flags.core"):
        if key == "banner":
            core.banner_setting({"text": secret, "level": "loud"})
        else:
            core.stricter_switch(key, True, secret)
    (record,) = caplog.records
    assert key in record.getMessage()
    assert secret not in caplog.text
