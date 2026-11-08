"""The Google re-authentication rule, table-driven (``oauth_reauth_policy``, ADR 0003 section 1,
amendment of 2026-11-08, #247). Pure: no database."""

from dataclasses import replace

import pytest

from ownvitals.auth import oauth_providers
from ownvitals.auth.oauth_reauth_policy import (
    AUTH_TIME_SKEW,
    GOOGLE,
    REAUTH_MAX_AGE,
    ReauthAnswer,
    refusal,
)

NOW = 1_800_000_000.0
OK = ReauthAnswer(
    provider="google",
    has_password=False,
    flow_age=10.0,
    email_verified=True,
    auth_time=None,
    now=NOW,
)


def test_the_constant_names_the_provider_module_s_google() -> None:
    assert GOOGLE == oauth_providers.GOOGLE


def test_the_numbers_are_not_widened() -> None:
    assert (REAUTH_MAX_AGE, AUTH_TIME_SKEW) == (300, 60)


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({}, None),
        ({"auth_time": None}, None),  # Google sends none: bound to this flow instead
        ({"auth_time": int(NOW) - 86_400}, None),  # an old sign-in at Google
        ({"auth_time": int(NOW)}, None),
        ({"auth_time": int(NOW) + AUTH_TIME_SKEW}, None),
        ({"auth_time": int(NOW) + AUTH_TIME_SKEW + 1}, ("reauth_stale", "auth_time_future")),
        ({"flow_age": 0.0}, None),
        ({"flow_age": float(REAUTH_MAX_AGE)}, None),
        ({"flow_age": REAUTH_MAX_AGE + 0.001}, ("reauth_expired", "flow_old")),
        ({"flow_age": -1.0}, ("reauth_expired", "flow_old")),
        ({"flow_age": None}, ("reauth_expired", "flow_old")),
        ({"email_verified": False}, ("invalid", "email_unverified")),
        ({"has_password": True}, ("reauth_use_password", "has_password")),
        # A password wins over everything else, even a fresh auth_time.
        ({"has_password": True, "auth_time": int(NOW)}, ("reauth_use_password", "has_password")),
        ({"provider": "github"}, ("invalid", "provider_unsupported")),
        ({"provider": "Google"}, ("invalid", "provider_unsupported")),
    ],
)
def test_refusal(change: dict[str, object], expected: tuple[str, str] | None) -> None:
    assert refusal(replace(OK, **change)) == expected  # pyright: ignore[reportArgumentType]


def test_the_module_imports_no_io() -> None:
    from pathlib import Path

    import ownvitals.auth.oauth_reauth_policy as policy

    source = Path(policy.__file__).read_text(encoding="utf-8")
    imports = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    assert imports == ["from dataclasses import dataclass"]
