"""Each refused OAuth outcome names its own reason for the log line (#282, from the #261 review).

The refusals here are the race windows of the H2 locks (#248): an identity or account gone by the
time the Google sign-in holds the user row, and a session revoked while a link was in flight.
They are reached by replacing the store calls, so no database is needed. Synthetic values only.
"""

from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from ownvitals.auth import oauth_outcomes
from ownvitals.auth.oauth_client import ProviderIdentity
from ownvitals.auth.oauth_store import Flow
from ownvitals.auth.sessions import SessionGone

USER = uuid4()
IDENTITY = ProviderIdentity(
    subject="synthetic-subject", email="user@example.com", email_verified=True, auth_time=None
)


def _request(purpose: str) -> oauth_outcomes.Request:
    flow = Flow(
        provider="google",
        purpose=purpose,
        state_hash=b"",
        nonce="n",
        verifier="v",
        user_id=USER if purpose != "sign_in" else None,
        invite_id=None,
        return_to=None,
        locale=None,
    )
    info = SimpleNamespace(id=uuid4(), user_id=USER, email="user@example.com")
    return oauth_outcomes.Request(
        settings=cast(Any, None),
        keys=cast(Any, None),
        flow=flow,
        identity=IDENTITY,
        info=cast(Any, info),
        session_token=None,
        device_token=None,
        pending_token=None,
        ip="192.0.2.1",
        user_agent=None,
    )


def _owners(*answers: object):
    queue = list(answers)

    async def identity_owner(*_args: object) -> object:
        return queue.pop(0)

    return identity_owner


async def _locked(_db: object, _user_id: object) -> object:
    return SimpleNamespace(disabled=False)


async def _gone(_db: object, _user_id: object) -> None:
    return None


async def test_a_sign_in_whose_identity_went_under_the_lock_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(oauth_outcomes, "identity_owner", _owners((USER,), None))
    monkeypatch.setattr(oauth_outcomes, "lock_user_row", _locked)
    outcome = await oauth_outcomes.apply(cast(Any, None), _request("sign_in"))
    assert (outcome.code, outcome.why) == ("invalid", "identity_gone")


async def test_a_sign_in_whose_identity_moved_under_the_lock_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(oauth_outcomes, "identity_owner", _owners((USER,), (uuid4(),)))
    monkeypatch.setattr(oauth_outcomes, "lock_user_row", _locked)
    outcome = await oauth_outcomes.apply(cast(Any, None), _request("sign_in"))
    assert (outcome.code, outcome.why) == ("invalid", "identity_gone")


async def test_a_sign_in_whose_account_went_under_the_lock_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(oauth_outcomes, "identity_owner", _owners((USER,), (USER,)))
    monkeypatch.setattr(oauth_outcomes, "lock_user_row", _gone)
    outcome = await oauth_outcomes.apply(cast(Any, None), _request("sign_in"))
    assert (outcome.code, outcome.why) == ("invalid", "account_gone")


async def test_a_link_whose_session_was_revoked_meanwhile_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def add_identity(*_args: object, **_kwargs: object) -> bool:
        raise SessionGone

    monkeypatch.setattr(oauth_outcomes, "add_identity", add_identity)
    outcome = await oauth_outcomes.apply(cast(Any, None), _request("link"))
    assert (outcome.code, outcome.why) == ("session_mismatch", "session_gone")
