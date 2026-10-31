"""Failure injection of the Withings fake, per request number: dropped connections (before or
after the server processed the request), delays and timeouts, API error statuses (601 with
``wait_seconds``), HTTP errors, garbage bodies and mutation hooks. Fakes only, no network.
"""

import json
from typing import Any

import httpx
import pytest

from withingscore import FakeWithings
from withingsfake import call, fake_client, make_fake
from withingswire import STATUS_RATE_LIMITED, Request, Response


class Rig:
    def __init__(self, timeout: float | None = None) -> None:
        self.fake, self.app, self.user = make_fake(seed=4, days=8, ecg_count=1)
        self.client = fake_client(self.fake, timeout=timeout)
        self.tokens = self.fake.tokens_for(self.app.client_id, self.user.userid)

    async def refresh(self, refresh_token: str) -> dict[str, Any]:
        return await call(
            self.client, "/v2/oauth2", "requesttoken", client_id=self.app.client_id,
            client_secret=self.app.client_secret, grant_type="refresh_token",
            refresh_token=refresh_token,
        )  # fmt: skip

    async def meas(self) -> dict[str, Any]:
        return await call(
            self.client, "/measure", "getmeas", self.tokens["access_token"], lastupdate=0
        )

    async def goals(self) -> dict[str, Any]:
        return await call(self.client, "/v2/user", "getgoals", self.tokens["access_token"])


@pytest.fixture
def rig() -> Rig:
    return Rig()


async def test_a_dropped_request_that_never_arrived_changes_nothing(rig: Rig) -> None:
    code = rig.fake.authorize(rig.app.client_id, rig.user.userid)
    rig.fake.inject("drop", at=1)
    params = {
        "client_id": rig.app.client_id, "client_secret": rig.app.client_secret,
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": next(iter(rig.app.redirect_uris)),
    }  # fmt: skip
    with pytest.raises(httpx.ConnectError):
        await call(rig.client, "/v2/oauth2", "requesttoken", **params)
    assert rig.fake.log[0].outcome == "dropped"
    assert (await call(rig.client, "/v2/oauth2", "requesttoken", **params))["status"] == 0


async def test_a_response_lost_after_the_rotation_leaves_the_old_token_usable(rig: Rig) -> None:
    old = rig.tokens["refresh_token"]
    rig.fake.inject("drop", at=1, endpoint="v2/oauth2:requesttoken", when="after")
    with pytest.raises(httpx.ReadError):
        await rig.refresh(old)
    grant = rig.fake.grant(rig.app.client_id, rig.user.userid)
    assert grant.previous_refresh == old
    lost = grant.refresh
    assert lost != old  # the server rotated; the client never saw the new pair
    retried = await rig.refresh(old)
    assert retried["status"] == 0
    assert retried["body"]["refresh_token"] not in (old, lost)
    assert (await rig.refresh(lost))["status"] == 2553  # the pair it never saw is gone


async def test_an_error_replacing_an_answer_after_processing_is_ambiguous_too(rig: Rig) -> None:
    old = rig.tokens["refresh_token"]
    rig.fake.inject("status", at=1, when="after", status=2555)
    assert (await rig.refresh(old))["status"] == 2555
    assert rig.fake.grant(rig.app.client_id, rig.user.userid).previous_refresh == old
    assert (await rig.refresh(old))["status"] == 0


async def test_a_status_failure_before_processing_leaves_state_alone(rig: Rig) -> None:
    old = rig.tokens["refresh_token"]
    rig.fake.inject("status", at=1, status=503)
    assert (await rig.refresh(old))["status"] == 503
    assert rig.fake.grant(rig.app.client_id, rig.user.userid).previous_refresh is None
    assert (await rig.refresh(old))["status"] == 0


async def test_failures_fire_on_the_numbered_requests_only(rig: Rig) -> None:
    failure = rig.fake.inject("status", at=2, status=STATUS_RATE_LIMITED, wait_seconds=30)
    statuses = [(await rig.goals())["status"] for _ in range(3)]
    assert statuses == [0, 601, 0]
    assert failure.fired == 1


async def test_numbering_can_count_one_endpoint_only(rig: Rig) -> None:
    rig.fake.inject("status", at=2, endpoint="measure:getmeas", status=503)
    order = [
        await rig.goals(),
        await rig.meas(),
        await rig.goals(),
        await rig.meas(),
        await rig.meas(),
    ]
    assert [r["status"] for r in order] == [0, 0, 0, 503, 0]


async def test_a_601_storm_is_a_run_of_request_numbers(rig: Rig) -> None:
    rig.fake.inject("status", at=range(2, 6), status=STATUS_RATE_LIMITED, wait_seconds=60)
    replies = [await rig.goals() for _ in range(6)]
    assert [r["status"] for r in replies] == [0, 601, 601, 601, 601, 0]
    assert replies[1]["body"] == {"wait_seconds": 60}


async def test_a_601_without_wait_seconds_has_no_body(rig: Rig) -> None:
    rig.fake.inject("status", at=1, status=STATUS_RATE_LIMITED)
    reply = await rig.goals()
    assert reply["status"] == 601
    assert "body" not in reply


async def test_a_delay_holds_the_response_back_and_moves_the_clock(rig: Rig) -> None:
    rig.fake.inject("delay", at=1, seconds=45)
    started = rig.fake.clock.now()
    assert (await rig.goals())["status"] == 0
    assert rig.fake.clock.now() - started == 45


async def test_a_delay_past_the_read_timeout_is_a_timeout_after_processing() -> None:
    rig = Rig(timeout=10)
    rig.fake.inject("delay", at=1, seconds=45)
    started = rig.fake.clock.now()
    with pytest.raises(httpx.ReadTimeout):
        await rig.refresh(rig.tokens["refresh_token"])
    assert rig.fake.clock.now() - started == 10  # the client waited its timeout, no longer
    assert (
        rig.fake.grant(rig.app.client_id, rig.user.userid).generation == 2
    )  # but it was processed


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503])
async def test_an_http_error_replaces_the_answer(rig: Rig, status: int) -> None:
    rig.fake.inject("http", at=1, http_status=status)
    reply = await rig.client.post(
        "https://wbsapi.withings.net/v2/user",
        data={"action": "getgoals"},
        headers={"authorization": f"Bearer {rig.tokens['access_token']}"},
    )
    assert reply.status_code == status
    assert (await rig.goals())["status"] == 0


async def test_a_garbage_body_is_http_200_without_json(rig: Rig) -> None:
    rig.fake.inject("garbage", at=1)
    reply = await rig.client.post(
        "https://wbsapi.withings.net/v2/user",
        data={"action": "getgoals"},
        headers={"authorization": f"Bearer {rig.tokens['access_token']}"},
    )
    assert reply.status_code == 200
    with pytest.raises(json.JSONDecodeError):
        reply.json()


async def test_a_mutation_hook_runs_before_the_request(rig: Rig) -> None:
    before = await rig.meas()
    assert before["body"]["measuregrps"]
    rig.fake.inject(
        "mutate", at=2, action=lambda fake: fake.users[rig.user.userid].data.measuregrps.clear()
    )
    assert (await rig.meas())["body"]["measuregrps"] == []


async def test_the_request_log_masks_secrets(rig: Rig) -> None:
    code = rig.fake.authorize(rig.app.client_id, rig.user.userid)
    issued = await call(
        rig.client,
        "/v2/oauth2",
        "requesttoken",
        client_id=rig.app.client_id,
        client_secret=rig.app.client_secret,
        grant_type="authorization_code",
        code=code,
        redirect_uri=next(iter(rig.app.redirect_uris)),
    )
    token = issued["body"]["access_token"]
    await call(rig.client, "/v2/user", "getgoals", token)
    first = rig.fake.log[0]
    assert first.endpoint == "v2/oauth2:requesttoken"
    assert first.params["client_secret"] == first.params["code"] == "***"
    assert first.params["grant_type"] == "authorization_code"
    dump = repr(rig.fake.log)
    assert rig.app.client_secret not in dump
    assert token not in dump  # the Authorization header is never logged
    assert [e.number for e in rig.fake.log] == [1, 2]
    assert [e.api_status for e in rig.fake.log] == [0, 0]


def test_without_strict_hosts_the_fake_routes_by_path_alone() -> None:
    """How the e2e service mounts it behind a single host."""
    fake = FakeWithings(strict_hosts=False)
    app = fake.add_app()
    user = fake.add_user(days=3, ecg_count=0)
    token = fake.tokens_for(app.client_id, user.userid)["access_token"]
    request = Request(
        "POST", "http://fake-withings:8080/v2/user", {"authorization": f"Bearer {token}"},
        b"action=getgoals",
    )  # fmt: skip
    reply = fake.handle(request)
    assert isinstance(reply, Response)
    assert reply.json()["status"] == 0
    page = fake.handle(Request("GET", "http://fake-withings:8080/oauth2_user/authorize2"))
    assert page.status_code == 400  # reached the authorize page, which wants parameters
