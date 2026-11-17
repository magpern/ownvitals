"""The AI monthly caps as stored flags without a database (#528, ``ownvitals.flags.caps``): a
stored cap only lowers the environment's, the per-user cap never ends above the global one, an
invalid stored cap counts as 0 and is logged by key only, and the write side refuses what the
read side would clamp. Amounts are synthetic."""

import logging
from typing import Any

import pytest

from ownvitals.flags import caps, core
from ownvitals.flags.caps import Caps
from ownvitals.flags.core import GLOBAL_CAP, USER_CAP, FlagRefused

ENV_USER, ENV_GLOBAL = 10_000_000, 100_000_000


@pytest.mark.parametrize(
    ("stored_user", "stored_global", "expected"),
    [
        (None, None, Caps(ENV_USER, ENV_GLOBAL)),  # no rows: the environment's
        (5_000_000, None, Caps(5_000_000, ENV_GLOBAL)),
        (None, 50_000_000, Caps(ENV_USER, 50_000_000)),
        (20_000_000, 200_000_000, Caps(ENV_USER, ENV_GLOBAL)),  # never above the environment
        (None, 3_000_000, Caps(3_000_000, 3_000_000)),  # the per-user cap follows a lower global
        (8_000_000, 2_000_000, Caps(2_000_000, 2_000_000)),
        (0, None, Caps(0, ENV_GLOBAL)),
        ("lots", None, Caps(0, ENV_GLOBAL)),  # invalid: 0, nothing is reserved
        (None, 1.5, Caps(0, 0)),
        (True, None, Caps(0, ENV_GLOBAL)),
        (-1, None, Caps(0, ENV_GLOBAL)),
        (core.MAX_CAP_MICROUSD + 1, None, Caps(0, ENV_GLOBAL)),
        (None, {"amount": 1}, Caps(0, 0)),
    ],
)
def test_the_caps_in_force_are_never_above_the_environment(
    stored_user: Any, stored_global: Any, expected: Caps
) -> None:
    in_force = caps.effective_caps(ENV_USER, ENV_GLOBAL, stored_user, stored_global)
    assert in_force == expected
    assert in_force.user <= in_force.global_ <= ENV_GLOBAL
    assert in_force.user <= ENV_USER


def test_an_environment_of_zero_stays_zero_whatever_is_stored() -> None:
    """ADR 0014 open question 2: caps of 0 mean no request can be reserved."""
    assert caps.effective_caps(0, 0, None, None) == Caps(0, 0)
    assert caps.effective_caps(0, 0, ENV_USER, ENV_GLOBAL) == Caps(0, 0)
    with pytest.raises(FlagRefused) as refused:
        caps.check_environment(USER_CAP, 1, 0, 0)
    assert refused.value.reason == "above_environment"
    caps.check_environment(USER_CAP, 0, 0, 0)


@pytest.mark.parametrize("env", [-1, 1.0, True, core.MAX_CAP_MICROUSD + 1])
def test_an_environment_cap_out_of_range_is_a_bug(env: Any) -> None:
    with pytest.raises(ValueError, match="cap"):
        caps.effective_caps(env, ENV_GLOBAL, None, None)
    with pytest.raises(ValueError, match="cap"):
        caps.effective_caps(ENV_USER, env, None, None)


def test_only_the_cap_keys_are_caps() -> None:
    with pytest.raises(ValueError, match="cap flag"):
        caps.stored_cap("banner", 1)


@pytest.mark.parametrize(
    ("value", "result"),
    [
        (0, 0),
        (10_000_000, 10_000_000),
        (core.MAX_CAP_MICROUSD, core.MAX_CAP_MICROUSD),
        (core.MAX_CAP_MICROUSD + 1, "invalid_value"),
        (-1, "invalid_value"),
        (10.0, "invalid_value"),
        (True, "invalid_value"),
        ("10000000", "invalid_value"),
        (None, "invalid_value"),
    ],
)
@pytest.mark.parametrize("key", [USER_CAP, GLOBAL_CAP])
def test_the_write_side_takes_only_an_integer_amount(key: str, value: Any, result: Any) -> None:
    if result == "invalid_value":
        with pytest.raises(FlagRefused) as refused:
            core.validate_for_write(key, value)
        assert refused.value.reason == result
    else:
        assert core.validate_for_write(key, value) == result


@pytest.mark.parametrize(
    ("key", "value", "refused"),
    [
        (USER_CAP, ENV_USER, False),
        (USER_CAP, ENV_USER + 1, True),
        (GLOBAL_CAP, ENV_GLOBAL, False),
        (GLOBAL_CAP, ENV_GLOBAL + 1, True),
        (GLOBAL_CAP, ENV_USER, False),
    ],
)
def test_a_stored_cap_may_not_be_above_the_environment(key: str, value: int, refused: bool) -> None:
    if refused:
        with pytest.raises(FlagRefused) as caught:
            caps.check_environment(key, value, ENV_USER, ENV_GLOBAL)
        assert caught.value.reason == "above_environment"
    else:
        caps.check_environment(key, value, ENV_USER, ENV_GLOBAL)


@pytest.mark.parametrize(
    ("key", "value", "stored_user", "stored_global", "refused"),
    [
        # The per-user cap against the global one in force.
        (USER_CAP, 5_000_000, None, 5_000_000, False),
        (USER_CAP, 5_000_001, None, 5_000_000, True),
        (USER_CAP, ENV_USER, None, None, False),
        (USER_CAP, 1, None, "invalid", True),  # an invalid global counts as 0
        # The global cap against the per-user one in force.
        (GLOBAL_CAP, ENV_USER, None, None, False),
        (GLOBAL_CAP, ENV_USER - 1, None, None, True),  # below the environment's per-user cap
        (GLOBAL_CAP, 2_000_000, 2_000_000, None, False),
        (GLOBAL_CAP, 1_999_999, 2_000_000, None, True),
        (GLOBAL_CAP, 0, 0, 50_000_000, False),
    ],
)
def test_the_per_user_cap_stays_at_or_below_the_global_one(
    key: str, value: int, stored_user: Any, stored_global: Any, refused: bool
) -> None:
    def check() -> None:
        caps.check_against_other(key, value, ENV_USER, ENV_GLOBAL, stored_user, stored_global)

    if refused:
        with pytest.raises(FlagRefused) as caught:
            check()
        assert caught.value.reason == "user_above_global"
    else:
        check()


@pytest.mark.parametrize("key", [USER_CAP, GLOBAL_CAP])
def test_an_invalid_stored_cap_is_logged_by_key_only(
    key: str, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "synthetic-not-for-the-log"
    with caplog.at_level(logging.WARNING, logger="ownvitals.flags.core"):
        assert caps.stored_cap(key, secret) == 0
    (record,) = caplog.records
    assert key in record.getMessage()
    assert secret not in caplog.text
