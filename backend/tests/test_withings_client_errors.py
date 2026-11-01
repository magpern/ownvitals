"""The Withings client's error taxonomy and retries (PLAN §4.2, ADR 0004 §8, #20).

Status-in-JSON errors (HTTP 200) map to the four classes by number (a table-driven test against
the fake); HTTP and transport failures too. ``Transient`` is retried only within the caller's
budget; ``TokenInvalid`` and ``RateLimited`` never are, and the token refresh is never retried
at all. Anything that is not a transport failure (a ``TypeError``) propagates unclassified.
Fakes only, no network.
"""

import urllib.request
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import httpx
import pytest

from ownvitals import egress
from ownvitals.egress import OutboundBlocked, core
from ownvitals.withings import client as client_module
from ownvitals.withings.client import (
    RetryBudget,
    WithingsApp,
    WithingsClient,
    interpret,
    paginate,
)
from ownvitals.withings.errors import (
    DEFAULT_RATE_LIMIT_WAIT,
    Permanent,
    RateLimited,
    TokenInvalid,
    Transient,
    WithingsError,
)
from ownvitals.withings.models import MeasureBody, TokenResponse
from ownvitals.withings.units import real_value
from withingscore import FakeWithings
from withingsfake import FakeWithingsTransport

GOALS = "v2/user:getgoals"
APP = WithingsApp("synthetic-client", "synthetic-secret")


@pytest.fixture(autouse=True)
def _worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urllib.request, "_opener", None)
    monkeypatch.setattr(core, "_worker_list", None)
    egress.install_worker_allowlist(ai_enabled=False)


class Setup:
    def __init__(self) -> None:
        self.fake = FakeWithings(seed=5)
        self.app = self.fake.add_app()
        self.user = self.fake.add_user(days=3, ecg_count=0)
        tokens = self.fake.tokens_for(self.app.client_id, self.user.userid)
        self.token: str = tokens["access_token"]
        self.refresh_token: str = tokens["refresh_token"]
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def requests(self, endpoint: str) -> int:
        return sum(1 for e in self.fake.log if e.endpoint == endpoint)


@pytest.fixture
async def s() -> AsyncIterator[tuple[Setup, WithingsClient]]:
    setup = Setup()
    async with WithingsClient.open(
        APP, inner=FakeWithingsTransport(setup.fake), sleep=setup.sleep
    ) as client:
        yield setup, client


def _plain(error: WithingsError) -> None:
    """ADR 0004 §8: args are the category only, no cause, no context."""
    assert error.args == (error.category,)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert str(error) == error.category


# ------------------------------------------------------------------- statuses in the JSON body

STATUS_TABLE: list[tuple[int, type[WithingsError], str]] = [
    (401, TokenInvalid, "token_invalid"),
    (2553, TokenInvalid, "token_invalid"),
    (2555, TokenInvalid, "token_invalid"),
    (601, RateLimited, "rate_limited"),
    (214, Permanent, "not_authorized"),
    (247, Permanent, "invalid_userid"),
    (286, Permanent, "no_such_subscription"),
    (293, Permanent, "invalid_callback"),
    (304, Permanent, "invalid_code"),
    (503, Permanent, "invalid_params"),
    (2554, Permanent, "not_implemented"),
    (100, Permanent, "unknown_status"),
    (522, Permanent, "unknown_status"),
    (5000, Permanent, "unknown_status"),
    (-1, Permanent, "unknown_status"),
]


@pytest.mark.parametrize(("status", "cls", "category"), STATUS_TABLE)
async def test_a_json_status_maps_to_its_class(
    s: tuple[Setup, WithingsClient], status: int, cls: type[WithingsError], category: str
) -> None:
    setup, client = s
    setup.fake.inject("status", at=1, endpoint=GOALS, status=status)
    with pytest.raises(cls) as caught:
        await client.getgoals(setup.token, budget=RetryBudget(retries=5))
    error = caught.value
    assert (type(error), error.category, error.api_status) == (cls, category, status)
    assert (error.endpoint, error.http_status) == (GOALS, 200)
    assert error.ambiguous is False
    assert setup.requests(GOALS) == 1  # none of these is retried, whatever the budget
    _plain(error)


async def test_a_rate_limit_carries_its_wait(s: tuple[Setup, WithingsClient]) -> None:
    setup, client = s
    setup.fake.inject("status", at=1, endpoint=GOALS, status=601, wait_seconds=17)
    with pytest.raises(RateLimited) as caught:
        await client.getgoals(setup.token)
    assert caught.value.wait_seconds == 17.0
    setup.fake.inject("status", at=2, endpoint=GOALS, status=601)
    with pytest.raises(RateLimited) as caught:
        await client.getgoals(setup.token)
    assert caught.value.wait_seconds == DEFAULT_RATE_LIMIT_WAIT


async def test_the_real_rate_limit_is_a_rate_limited_error() -> None:
    setup = Setup()
    setup.fake.limiter.per_minute = 2
    async with WithingsClient.open(APP, inner=FakeWithingsTransport(setup.fake)) as client:
        await client.getgoals(setup.token)
        await client.getgoals(setup.token)
        with pytest.raises(RateLimited):
            await client.getgoals(setup.token, budget=RetryBudget(retries=3))
    assert setup.requests(GOALS) == 3


# --------------------------------------------------------------------- HTTP and transport

HTTP_TABLE: list[tuple[int, type[WithingsError]]] = [
    (408, Transient), (429, Transient), (500, Transient), (502, Transient), (503, Transient),
    (599, Transient), (401, TokenInvalid), (400, Permanent), (403, Permanent), (404, Permanent),
    (302, Permanent), (204, Permanent),
]  # fmt: skip


@pytest.mark.parametrize(("http_status", "cls"), HTTP_TABLE)
async def test_an_http_status_maps_to_its_class(
    s: tuple[Setup, WithingsClient], http_status: int, cls: type[WithingsError]
) -> None:
    setup, client = s
    setup.fake.inject("http", at=1, endpoint=GOALS, http_status=http_status)
    with pytest.raises(cls) as caught:
        await client.getgoals(setup.token)
    assert caught.value.http_status == http_status
    assert caught.value.api_status is None
    _plain(caught.value)


@pytest.mark.parametrize(
    ("kind", "category", "opts"),
    [
        ("drop", "network", {"when": "before"}),
        ("drop", "network", {"when": "after"}),
        ("delay", "timeout", {"seconds": 120}),
        ("garbage", "malformed_response", {}),
    ],
)
async def test_transport_failures_are_transient(
    s: tuple[Setup, WithingsClient], kind: Any, category: str, opts: dict[str, Any]
) -> None:
    setup, client = s
    setup.fake.inject(kind, at=1, endpoint=GOALS, **opts)
    with pytest.raises(Transient) as caught:
        await client.getgoals(setup.token)
    assert caught.value.category == category
    assert caught.value.ambiguous
    _plain(caught.value)


def _mock(content: bytes | None = None, *, raises: BaseException | None = None) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if raises is not None:
            raise raises
        return httpx.Response(200, content=content)

    return httpx.MockTransport(handler)


async def test_a_programming_error_is_not_classified() -> None:
    async with WithingsClient.open(APP, inner=_mock(raises=TypeError("a bug"))) as client:
        with pytest.raises(TypeError):
            await client.getgoals("synthetic-token", budget=RetryBudget(retries=3))


@pytest.mark.parametrize(
    ("content", "cls", "category"),
    [
        (b'{"status":0,"body":{"goals":"not an object"}}', Permanent, "unexpected_body"),
        (b'{"status":0}', Permanent, "unexpected_body"),
        (b'{"status":0,"body":{"goals":{"steps":NaN}}}', Transient, "malformed_response"),
        (b'{"status":true,"body":{}}', Transient, "malformed_response"),
        (b'{"status":"0","body":{}}', Transient, "malformed_response"),
        (b"[0]", Transient, "malformed_response"),
        (b"\xff\xfe", Transient, "malformed_response"),
        (b"[" * 100_000, Transient, "malformed_response"),
    ],
)
async def test_an_unusable_answer_is_categorized(
    content: bytes, cls: type[WithingsError], category: str
) -> None:
    async with WithingsClient.open(APP, inner=_mock(content)) as client:
        with pytest.raises(cls) as caught:
            await client.getgoals("synthetic-token")
    assert caught.value.category == category
    assert caught.value.ambiguous  # Withings may have acted on it
    _plain(caught.value)


async def test_an_oversized_answer_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "MAX_RESPONSE_BYTES", 64)
    content = b'{"status":0,"body":{"goals":{}},"padding":"' + b"x" * 100 + b'"}'
    async with WithingsClient.open(APP, inner=_mock(content)) as client:
        with pytest.raises(Permanent) as caught:
            await client.getgoals("synthetic-token")
    assert caught.value.category == "response_too_large"


async def test_an_oversized_answer_is_not_read_to_the_end(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "MAX_RESPONSE_BYTES", 64)
    sent: list[int] = []

    async def endless() -> AsyncIterator[bytes]:
        for _ in range(1_000):
            sent.append(1)
            yield b"x" * 32

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=endless())

    async with WithingsClient.open(APP, inner=httpx.MockTransport(handler)) as client:
        with pytest.raises(Permanent):
            await client.getgoals("synthetic-token")
    assert len(sent) <= 4  # reading stops at the cap


async def test_numbers_are_never_floats() -> None:
    content = b'{"status":0,"body":{"activities":[{"date":"2026-10-30","distance":1.1}]}}'
    async with WithingsClient.open(APP, inner=_mock(content)) as client:
        body = await client.getactivity("synthetic-token", lastupdate=0)
    assert body.activities[0].distance == Decimal("1.1")


async def test_a_malformed_token_is_never_sent() -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, content=b'{"status":0,"body":{"goals":{}}}')

    async with WithingsClient.open(APP, inner=httpx.MockTransport(handler)) as client:
        for bad in ("", "a b", "x\r\nX-Injected: 1", "té"):
            with pytest.raises(ValueError, match="malformed") as caught:
                await client.getgoals(bad)
            assert bad not in str(caught.value) or not bad
    assert sent == []


async def test_outside_the_worker_the_client_cannot_be_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(core, "_worker_list", None)
    with pytest.raises(OutboundBlocked):
        async with WithingsClient.open(APP, inner=_mock(b"{}")):
            pass


# ------------------------------------------------------------------------------- retries


async def test_transient_failures_retry_within_the_budget(s: tuple[Setup, WithingsClient]) -> None:
    setup, client = s
    setup.fake.inject("http", at={1, 2}, endpoint=GOALS, http_status=503)
    budget = RetryBudget(retries=3, max_delay=10)
    body = await client.getgoals(setup.token, budget=budget)
    assert body.goals.steps == 10_000
    assert setup.requests(GOALS) == 3
    assert budget.retries == 1
    assert budget.used == 2
    assert len(setup.sleeps) == 2
    assert 0.8 <= setup.sleeps[0] <= 1.2
    assert 1.6 <= setup.sleeps[1] <= 2.4


async def test_a_spent_budget_raises(s: tuple[Setup, WithingsClient]) -> None:
    setup, client = s
    setup.fake.inject("drop", at={1, 2, 3, 4}, endpoint=GOALS)
    budget = RetryBudget(retries=2)
    with pytest.raises(Transient):
        await client.getgoals(setup.token, budget=budget)
    assert setup.requests(GOALS) == 3
    assert budget.retries == 0
    with pytest.raises(Transient):  # the budget is shared: the next call has none left
        await client.getgoals(setup.token, budget=budget)
    assert setup.requests(GOALS) == 4


async def test_without_a_budget_nothing_is_retried(s: tuple[Setup, WithingsClient]) -> None:
    setup, client = s
    setup.fake.inject("http", at=1, endpoint=GOALS, http_status=500)
    with pytest.raises(Transient):
        await client.getgoals(setup.token)
    assert setup.requests(GOALS) == 1
    assert setup.sleeps == []


async def test_no_retry_waits_past_the_deadline(s: tuple[Setup, WithingsClient]) -> None:
    setup, client = s
    setup.fake.inject("http", at=1, endpoint=GOALS, http_status=500)
    with pytest.raises(Transient):
        await client.getgoals(setup.token, budget=RetryBudget(retries=3, deadline=0.0))
    assert setup.requests(GOALS) == 1


async def test_a_retry_after_hint_is_honoured_up_to_the_cap(
    s: tuple[Setup, WithingsClient],
) -> None:
    setup, client = s  # the fake's HTTP failures send Retry-After: 1
    setup.fake.inject("http", at=1, endpoint=GOALS, http_status=429)
    await client.getgoals(setup.token, budget=RetryBudget(retries=1, max_delay=0.5))
    assert setup.sleeps
    assert setup.sleeps[0] <= 0.5 * 1.2


@pytest.mark.parametrize("status", [401, 2553, 2555, 601, 503])
async def test_only_transient_failures_are_retried(
    s: tuple[Setup, WithingsClient], status: int
) -> None:
    setup, client = s
    setup.fake.inject("status", at=1, endpoint=GOALS, status=status)
    with pytest.raises(WithingsError):
        await client.getgoals(setup.token, budget=RetryBudget(retries=5))
    assert setup.requests(GOALS) == 1
    assert setup.sleeps == []


# --------------------------------------------------------------------------- token refresh

REFRESH = "v2/oauth2:requesttoken"


@pytest.mark.parametrize(
    ("kind", "opts"),
    [("drop", {"when": "after"}), ("http", {"http_status": 503}), ("garbage", {})],
)
async def test_a_refresh_is_never_retried_and_its_failure_is_ambiguous(
    s: tuple[Setup, WithingsClient], kind: Any, opts: dict[str, Any]
) -> None:
    setup, client = s
    setup.fake.inject(kind, at=1, endpoint=REFRESH, **opts)
    with pytest.raises(Transient) as caught:
        await client.refresh(setup.refresh_token)
    assert caught.value.ambiguous
    assert setup.requests(REFRESH) == 1
    assert setup.sleeps == []


async def test_a_refused_refresh_token_is_token_invalid(s: tuple[Setup, WithingsClient]) -> None:
    setup, client = s
    with pytest.raises(TokenInvalid) as caught:
        await client.refresh("synthetic-unknown-refresh-token")
    assert caught.value.api_status == 2553
    assert not caught.value.ambiguous
    assert setup.requests(REFRESH) == 1


async def test_the_lost_refresh_answer_can_be_recovered_once(
    s: tuple[Setup, WithingsClient],
) -> None:
    """PLAN §4.3 step 4: the answer was lost after Withings rotated; the stored refresh token
    still works once (its grace), because the new access token was never used."""
    setup, client = s
    setup.fake.inject("drop", at=1, endpoint=REFRESH, when="after")
    with pytest.raises(Transient):
        await client.refresh(setup.refresh_token)
    tokens = await client.refresh(setup.refresh_token)
    assert tokens.refresh_token.get_secret_value() != setup.refresh_token


def test_the_code_exchange_answer_reads_with_the_same_taxonomy() -> None:
    endpoint = "v2/oauth2:requesttoken"
    with pytest.raises(Permanent) as caught:
        interpret(endpoint, 200, b'{"status":304,"error":"synthetic text"}', TokenResponse)
    assert caught.value.category == "invalid_code"
    with pytest.raises(Permanent):  # status 0 without the tokens: never stored
        interpret(endpoint, 200, b'{"status":0,"body":{"userid":1}}', TokenResponse)
    ok = (
        b'{"status":0,"body":{"userid":"42","access_token":"a","refresh_token":"r","expires_in":1}}'
    )
    tokens = interpret(endpoint, 200, ok, TokenResponse)
    assert tokens.userid == 42
    extra = ok.replace(b'"expires_in":1', b'"expires_in":1,"new_secret":"synthetic-s"')
    assert interpret(endpoint, 200, extra, TokenResponse).raw == {"new_secret": None}


# ---------------------------------------------------------------------- pagination, units


async def test_a_stuck_offset_stops_the_pagination() -> None:
    page = MeasureBody(measuregrps=[], more=True, offset=3)
    calls: list[int | None] = []

    async def fetch(offset: int | None) -> MeasureBody:
        calls.append(offset)
        return page

    with pytest.raises(Permanent) as caught:
        _ = [p async for p in paginate(fetch)]
    assert caught.value.category == "pagination_stuck"
    assert calls == [None, 3]  # the second answer did not advance: stop at once

    async def endless(offset: int | None) -> MeasureBody:
        return MeasureBody(measuregrps=[], more=True, offset=(offset or 0) + 1)

    with pytest.raises(Permanent):
        _ = [p async for p in paginate(endless, max_pages=5)]


def test_real_value_is_exact() -> None:
    assert real_value(70_512, -3) == Decimal("70.512")
    assert real_value(1, -1) == Decimal("0.1")
    assert real_value(-5, 2) == Decimal("-500")
    assert real_value(123456789012345678901234567890123, -3) == Decimal(
        "123456789012345678901234567890.123"
    )
    for bad_value, bad_unit in ((True, 0), (1, False), (1.5, 0), (1, 1.0), ("1", 0)):
        with pytest.raises(TypeError):
            real_value(bad_value, bad_unit)  # type: ignore[arg-type]
    for unit in (-31, 31, 10**6):
        with pytest.raises(ValueError, match="range"):
            real_value(1, unit)
