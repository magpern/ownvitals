"""The access token a sync job calls Withings with (``withings.fresh_token``): refreshed before
it expires, never refreshed while it is good, and every refresh outcome mapped to a job category.
Synthetic ids and token strings; no network, no database (the rotation itself is tested in
``test_withings_tokens.py``).
"""

import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from ownvitals.jobs import JobFailure
from ownvitals.withings import fresh_token
from ownvitals.withings.fresh_token import fresh_access_token, needs_refresh
from ownvitals.withings.tokens import RefreshResult

SYNTHETIC = True
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
_fresh: Any = fresh_access_token  # the stand-ins below are not the real job, app or ring


class _Job:
    def __init__(self) -> None:
        self.factory = object()
        self.ctx = object()
        self.lease = object()
        self.deadlines: list[float] = []

    @contextlib.asynccontextmanager
    async def provider_call(self, deadline: float) -> AsyncIterator[None]:
        self.deadlines.append(deadline)
        yield


class _Client:
    @classmethod
    @contextlib.asynccontextmanager
    async def open(cls, app: object, **_options: object) -> AsyncIterator[str]:
        yield f"client-for-{app}"


@pytest.mark.parametrize(
    ("expires_in", "expected"),
    [
        (timedelta(hours=2), False),
        (timedelta(minutes=11), False),
        (timedelta(minutes=10), True),
        (timedelta(minutes=1), True),
        (timedelta(minutes=-5), True),  # already expired
        (None, True),  # unknown: refresh once, the new pair stores its expiry
    ],
)
def test_a_token_is_refreshed_when_it_expires_within_ten_minutes(
    expires_in: timedelta | None, expected: bool
) -> None:
    expires_at = None if expires_in is None else NOW + expires_in
    assert needs_refresh(expires_at, NOW) is expected


async def test_a_good_token_is_returned_without_calling_withings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def refused(*_a: object, **_k: object) -> RefreshResult:
        raise AssertionError("no refresh while the token is good")

    monkeypatch.setattr(fresh_token, "refresh_tokens", refused)
    monkeypatch.setattr(fresh_token, "WithingsClient", _Client)
    soon = datetime.now(UTC) + timedelta(hours=2)
    job = _Job()
    got = await _fresh(
        job,
        "ring",
        uuid4(),
        "app",
        token="old",
        expires_at=soon,
    )
    assert got == "old"
    assert job.deadlines == []


@pytest.mark.parametrize("outcome", ["refreshed", "current"])
async def test_an_expiring_token_is_replaced_through_the_rotation(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    connection = uuid4()
    job = _Job()

    async def refresh(
        factory: Any, ring: Any, ctx: Any, connection_id: Any, client: Any, *, lease: Any
    ) -> RefreshResult:
        assert (factory, ring, ctx, lease) == (job.factory, "ring", job.ctx, job.lease)
        assert (connection_id, client) == (connection, "client-for-app")
        assert job.deadlines == [fresh_token.DEADLINE_SECONDS]  # inside the provider call
        return RefreshResult(outcome, 3, "new-token")

    monkeypatch.setattr(fresh_token, "refresh_tokens", refresh)
    monkeypatch.setattr(fresh_token, "WithingsClient", _Client)
    expired = datetime.now(UTC) - timedelta(minutes=1)
    got = await _fresh(
        job,
        "ring",
        connection,
        "app",
        token="old",
        expires_at=expired,
    )
    assert got == "new-token"


async def test_an_ambiguous_refresh_gets_the_one_recovery_try(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def refresh(*_a: object, **_k: object) -> RefreshResult:
        calls.append("refresh")
        return RefreshResult("recover", 2)

    async def recover(*_a: object, **_k: object) -> RefreshResult:
        calls.append("recover")
        return RefreshResult("refreshed", 3, "recovered-token")

    monkeypatch.setattr(fresh_token, "refresh_tokens", refresh)
    monkeypatch.setattr(fresh_token, "recover_refresh", recover)
    monkeypatch.setattr(fresh_token, "WithingsClient", _Client)
    got = await _fresh(
        _Job(),
        "ring",
        uuid4(),
        "app",
        token="old",
        expires_at=None,
    )
    assert (got, calls) == ("recovered-token", ["refresh", "recover"])


@pytest.mark.parametrize(
    ("outcome", "category"),
    [
        ("needs_reconnect", "needs_reconnect"),
        ("not_connected", "not_connected"),
        ("failed", "upstream_unavailable"),  # Withings did not answer: the next pass tries again
        ("recover", "upstream_unavailable"),  # recovery did not store a pair either
    ],
)
async def test_a_refresh_that_gives_no_token_fails_the_job_with_its_category(
    monkeypatch: pytest.MonkeyPatch, outcome: str, category: str
) -> None:
    async def result(*_a: object, **_k: object) -> RefreshResult:
        return RefreshResult(outcome, 2)

    monkeypatch.setattr(fresh_token, "refresh_tokens", result)
    monkeypatch.setattr(fresh_token, "recover_refresh", result)
    monkeypatch.setattr(fresh_token, "WithingsClient", _Client)
    with pytest.raises(JobFailure) as failed:
        await _fresh(
            _Job(),
            "ring",
            uuid4(),
            "app",
            token="old",
            expires_at=None,
        )
    assert failed.value.category == category
