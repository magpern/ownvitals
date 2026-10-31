"""``egress.RequestBudget``: every request (and redirect hop) to a budgeted host takes one before
it is sent, on the worker's urllib opener (also from a thread) and on the httpx clients; the one
over the budget raises ``RequestBudgetExhausted`` and never reaches the network. Other hosts are
not counted. Fakes only: no network."""

import asyncio
import io
import urllib.request
import urllib.response
from email.message import Message
from typing import Any

import httpx
import pytest

from ownvitals import egress
from ownvitals.egress import (
    WITHINGS_HOSTS,
    RequestBudget,
    RequestBudgetExhausted,
    build_allowlist_opener,
    core,
    install_worker_allowlist,
    request_budget,
    withings_async_client,
)

USER = "https://wbsapi.withings.net/v2/user"


@pytest.fixture(autouse=True)
def _fresh_egress(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urllib.request, "_opener", None)
    monkeypatch.setattr(core, "_worker_list", None)


class FakeHTTPS(urllib.request.BaseHandler):
    """Answers ``https`` without a network and counts what reached it."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def https_open(self, req: urllib.request.Request) -> Any:
        self.seen.append(req.full_url)
        response = urllib.response.addinfourl(io.BytesIO(b"{}"), Message(), req.full_url, 200)
        response.msg = "fake"  # pyright: ignore[reportAttributeAccessIssue]
        return response


def worker_opener(fake: FakeHTTPS) -> urllib.request.OpenerDirector:
    return build_allowlist_opener(egress.worker_allowlist(ai_enabled=True), https_handler=fake)


def get(opener: urllib.request.OpenerDirector, url: str = USER) -> int:
    with opener.open(url, timeout=5) as answer:
        return answer.status


def test_the_budget_takes_one_per_request_and_refuses_the_next_before_it_is_sent() -> None:
    fake = FakeHTTPS()
    opener = worker_opener(fake)
    budget = RequestBudget(WITHINGS_HOSTS, limit=2)
    with request_budget(budget):
        assert get(opener) == 200
        assert get(opener) == 200
        with pytest.raises(RequestBudgetExhausted) as refused:
            get(opener)
    assert refused.value.args == ("request budget used up",)
    assert budget.used == 2
    assert len(fake.seen) == 2  # the third never left
    assert get(opener) == 200  # outside the block nothing is counted
    assert budget.used == 2


def test_a_used_budget_starts_where_the_job_left_off() -> None:
    fake = FakeHTTPS()
    with (
        request_budget(RequestBudget(WITHINGS_HOSTS, limit=3, used=3)),
        pytest.raises(RequestBudgetExhausted),
    ):
        get(worker_opener(fake))
    assert fake.seen == []


def test_other_hosts_are_not_counted() -> None:
    fake = FakeHTTPS()
    budget = RequestBudget(WITHINGS_HOSTS, limit=0)
    with request_budget(budget):
        assert get(worker_opener(fake), "https://api.anthropic.com/v1/x") == 200
    budget.take("API.ANTHROPIC.COM")
    assert budget.used == 0
    with pytest.raises(RequestBudgetExhausted):
        budget.take("WBSAPI.WITHINGS.NET")
    with pytest.raises(RequestBudgetExhausted):
        budget.take("account.withings.com")


@pytest.mark.parametrize(("limit", "used"), [(-1, 0), (1, -1)])
def test_a_budget_is_not_negative(limit: int, used: int) -> None:
    with pytest.raises(ValueError, match="not negative"):
        RequestBudget(WITHINGS_HOSTS, limit=limit, used=used)


async def test_a_call_in_a_thread_is_counted_and_refused() -> None:
    """``asyncio.to_thread`` carries the context, so a blocking library call is budgeted too."""
    fake = FakeHTTPS()
    opener = worker_opener(fake)
    budget = RequestBudget(WITHINGS_HOSTS, limit=1)
    with request_budget(budget):
        assert await asyncio.to_thread(get, opener) == 200
        with pytest.raises(RequestBudgetExhausted):
            await asyncio.to_thread(get, opener)
    assert budget.used == 1
    assert len(fake.seen) == 1


class Recorder:
    def __init__(self) -> None:
        self.seen: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(str(request.url))
        if request.url.path == "/v2/moved":
            return httpx.Response(307, headers={"Location": "/v2/user"})
        return httpx.Response(200, json={"status": 0})


async def test_the_withings_client_is_counted_too_each_redirect_hop_included() -> None:
    install_worker_allowlist()
    recorder = Recorder()
    async with withings_async_client(inner=httpx.MockTransport(recorder)) as client:
        budget = RequestBudget(WITHINGS_HOSTS, limit=3)
        with request_budget(budget):
            await client.get(USER)
            await client.get("https://wbsapi.withings.net/v2/moved", follow_redirects=True)
            with pytest.raises(RequestBudgetExhausted):
                await client.get(USER)
    assert budget.used == 3
    assert len(recorder.seen) == 3


async def test_concurrent_jobs_each_spend_their_own_budget() -> None:
    install_worker_allowlist()
    recorder = Recorder()
    async with withings_async_client(inner=httpx.MockTransport(recorder)) as client:

        async def job(limit: int) -> RequestBudget:
            budget = RequestBudget(WITHINGS_HOSTS, limit=limit)
            with request_budget(budget):
                for _ in range(limit):
                    await client.get(USER)
                    await asyncio.sleep(0)
            return budget

        one, two = await asyncio.gather(job(2), job(3))
    assert (one.used, two.used) == (2, 3)
    assert len(recorder.seen) == 5
