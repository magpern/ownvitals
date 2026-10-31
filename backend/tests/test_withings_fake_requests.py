"""What the Withings fake refuses in the shape of a request, so that a regressed client fails
against it (PLAN 4.2): the API takes POST with a form body only and no secret in the query string,
and holds an application to 120 requests a minute (a token bucket on the fake clock). Each test
plays the client that does the wrong thing and shows it fails; the refusals change no state. Fakes
only, no network.
"""

from typing import Any

import httpx
import pytest

from withingscore import FakeWithings
from withingsfake import fake_client
from withingsrules import RATE_LIMIT_PER_MINUTE, SECRET_PARAMS, RateLimiter
from withingswire import (
    API_HOST,
    STATUS_BAD_PARAMS,
    STATUS_INVALID_TOKEN,
    STATUS_NOT_IMPLEMENTED,
    STATUS_RATE_LIMITED,
    sign,
)

HOOK = "https://app.example.test/api/webhooks/withings/synthetic-secret"


class World:
    def __init__(self, **options: Any) -> None:
        self.fake = FakeWithings(seed=9, **options)
        self.app = self.fake.add_app()
        self.user = self.fake.add_user(days=4, ecg_count=0)
        self.client: httpx.AsyncClient = fake_client(self.fake)
        self.tokens = self.fake.tokens_for(self.app.client_id, self.user.userid)
        self.token: str = self.tokens["access_token"]
        self.redirect = next(iter(self.app.redirect_uris))

    async def send(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        form: dict[str, Any] | None = None,
        token: str | None = None,
    ) -> dict[str, Any]:
        """Any request to the API host, shaped as the test says (the clients under test don't)."""
        headers = {"authorization": f"Bearer {token}"} if token else {}
        reply = await self.client.request(
            method, f"https://{API_HOST}{path}", params=query, data=form, headers=headers
        )
        return reply.json()

    async def goals(self) -> dict[str, Any]:
        return await self.send("POST", "/v2/user", form={"action": "getgoals"}, token=self.token)

    def exchange_form(self, code: str) -> dict[str, str]:
        return {
            "action": "requesttoken",
            "grant_type": "authorization_code",
            "client_id": self.app.client_id,
            "client_secret": self.app.client_secret,
            "code": code,
            "redirect_uri": self.redirect,
        }


@pytest.fixture
def w() -> World:
    return World()


# ------------------------------------------------------------ secrets only in POST bodies


async def test_a_client_that_exchanges_the_code_through_the_url_fails(w: World) -> None:
    code = w.fake.authorize(w.app.client_id, w.user.userid)
    form = w.exchange_form(code)
    by_get = await w.send("GET", "/v2/oauth2", query=form)
    assert by_get["status"] == STATUS_NOT_IMPLEMENTED
    for refused in (
        await w.send("POST", "/v2/oauth2", query=form),  # a POST, but the secrets are in the URL
        await w.send("POST", "/v2/oauth2", query=form, form=form),  # and the body repeats them
    ):
        assert refused["status"] == STATUS_BAD_PARAMS
        assert "client_secret" in refused["error"]
        assert w.app.client_secret not in refused["error"]  # names, never values
        assert code not in refused["error"]
    assert w.fake.grant(w.app.client_id, w.user.userid).generation == 1  # nothing was issued
    assert (await w.send("POST", "/v2/oauth2", form=form))["status"] == 0  # the code was untouched


async def test_a_client_that_refreshes_through_the_url_fails_and_rotates_nothing(
    w: World,
) -> None:
    form = {
        "action": "requesttoken",
        "grant_type": "refresh_token",
        "client_id": w.app.client_id,
        "client_secret": w.app.client_secret,
        "refresh_token": w.tokens["refresh_token"],
    }
    assert (await w.send("GET", "/v2/oauth2", query=form))["status"] == STATUS_NOT_IMPLEMENTED
    assert (await w.send("POST", "/v2/oauth2", query=form))["status"] == STATUS_BAD_PARAMS
    only_the_token = {"refresh_token": w.tokens["refresh_token"]}
    mixed = await w.send("POST", "/v2/oauth2", query=only_the_token, form=form)
    assert mixed["status"] == STATUS_BAD_PARAMS  # one leaked secret is enough
    assert w.fake.grant(w.app.client_id, w.user.userid).generation == 1  # nothing rotated
    assert (await w.send("POST", "/v2/oauth2", form=form))["status"] == 0


async def test_a_client_that_sends_the_access_token_in_the_url_fails(w: World) -> None:
    body = {"action": "getmeas", "lastupdate": 0}
    in_query = await w.send("POST", "/measure", query={"access_token": w.token}, form=body)
    assert in_query["status"] == STATUS_BAD_PARAMS
    both = await w.send(
        "POST", "/measure", query={"access_token": w.token}, form=body, token=w.token
    )
    assert both["status"] == STATUS_BAD_PARAMS  # the header does not excuse the URL
    by_get = await w.send("GET", "/measure", query={"action": "getmeas"}, token=w.token)
    assert by_get["status"] == STATUS_NOT_IMPLEMENTED
    assert (await w.send("POST", "/measure", form=body, token=w.token))["status"] == 0


async def test_a_client_that_signs_through_the_url_fails_and_burns_no_nonce(w: World) -> None:
    stamp = int(w.fake.clock.now())
    signed = sign(w.app.client_secret, "getnonce", w.app.client_id, stamp)
    form = {
        "action": "getnonce",
        "client_id": w.app.client_id,
        "timestamp": stamp,
        "signature": signed,
    }
    assert (await w.send("GET", "/v2/signature", query=form))["status"] == STATUS_NOT_IMPLEMENTED
    leaked = await w.send("POST", "/v2/signature", query={"signature": signed}, form=form)
    assert leaked["status"] == STATUS_BAD_PARAMS
    nonce = (await w.send("POST", "/v2/signature", form=form))["body"]["nonce"]
    revoke = {
        "action": "revoke",
        "client_id": w.app.client_id,
        "userid": w.user.userid,
        "nonce": nonce,
        "signature": sign(w.app.client_secret, "revoke", w.app.client_id, nonce),
    }
    leaked = await w.send(
        "POST", "/v2/oauth2", query={"signature": revoke["signature"]}, form=revoke
    )
    assert leaked["status"] == STATUS_BAD_PARAMS
    assert (await w.goals())["status"] == 0  # still connected
    assert (await w.send("POST", "/v2/oauth2", form=revoke))["status"] == 0  # the nonce survived


async def test_a_client_that_manages_subscriptions_through_the_url_fails(w: World) -> None:
    query = {"action": "subscribe", "callbackurl": HOOK, "appli": 1}
    assert (await w.send("GET", "/notify", query=query, token=w.token))[
        "status"
    ] == STATUS_NOT_IMPLEMENTED
    leaked = await w.send(
        "POST", "/notify", query={"access_token": w.token}, form=query, token=w.token
    )
    assert leaked["status"] == STATUS_BAD_PARAMS
    assert w.fake.subscriptions == []
    assert (await w.send("POST", "/notify", form=query, token=w.token))["status"] == 0


ENDPOINTS = [
    ("/v2/oauth2", "requesttoken"),
    ("/v2/oauth2", "revoke"),
    ("/v2/oauth2", "getdemoaccess"),
    ("/v2/signature", "getnonce"),
    ("/notify", "list"),
    ("/measure", "getmeas"),
    ("/v2/user", "getgoals"),
]


@pytest.mark.parametrize("name", sorted(SECRET_PARAMS))
@pytest.mark.parametrize(("path", "action"), ENDPOINTS)
async def test_no_secret_travels_in_the_query_string_of_any_endpoint(
    w: World, name: str, path: str, action: str
) -> None:
    reply = await w.send(
        "POST", path, query={name: "synthetic-value"}, form={"action": action}, token=w.token
    )
    assert reply["status"] == STATUS_BAD_PARAMS
    assert name in reply["error"]
    assert "synthetic-value" not in reply["error"]


def test_the_secrets_are_the_five_the_issue_names() -> None:
    names = ["access_token", "client_secret", "code", "refresh_token", "signature"]
    assert sorted(SECRET_PARAMS) == names


async def test_the_names_are_compared_without_regard_to_case(w: World) -> None:
    reply = await w.send(
        "POST", "/v2/user", query={"Client_Secret": "x"}, form={"action": "getgoals"}
    )
    assert reply["status"] == STATUS_BAD_PARAMS


@pytest.mark.parametrize("method", ["GET", "PUT", "PATCH", "DELETE"])
async def test_the_api_takes_post_only_even_with_a_form_body(w: World, method: str) -> None:
    reply = await w.send(method, "/v2/user", form={"action": "getgoals"}, token=w.token)
    assert reply["status"] == STATUS_NOT_IMPLEMENTED
    assert "POST" in reply["error"]


async def test_the_api_reads_the_form_body_only(w: World) -> None:
    only_in_url = await w.send("POST", "/v2/user", query={"action": "getgoals"}, token=w.token)
    assert only_in_url["status"] == STATUS_NOT_IMPLEMENTED  # no action in the body
    body = {"action": "getmeas"}
    future = 4_000_000_000
    ignored = await w.send(
        "POST", "/measure", query={"lastupdate": future}, form=body, token=w.token
    )
    assert ignored["body"]["measuregrps"]  # a filter in the URL is not read
    read = await w.send("POST", "/measure", form=body | {"lastupdate": future}, token=w.token)
    assert read["body"]["measuregrps"] == []


async def test_the_log_records_the_method_and_what_came_in_the_url(w: World) -> None:
    await w.goals()
    code = w.fake.authorize(w.app.client_id, w.user.userid)
    form = w.exchange_form(code)
    await w.send("GET", "/v2/oauth2", query=form)
    good, bad = w.fake.log[-2:]
    assert (good.method, good.query_names, good.api_status) == ("POST", (), 0)
    assert bad.method == "GET"
    assert set(bad.query_names) == set(form)
    assert bad.api_status == STATUS_NOT_IMPLEMENTED
    assert bad.params["client_secret"] == bad.params["code"] == "***"
    assert w.app.client_secret not in repr(w.fake.log)
    assert (bad.number, bad.endpoint) == (good.number + 1, "v2/oauth2:requesttoken")


async def test_the_authorize_page_stays_a_get_with_a_query() -> None:
    fake = FakeWithings()
    app = fake.add_app()
    fake.add_user(days=3, ecg_count=0)
    client = fake_client(fake)
    query = {
        "response_type": "code",
        "client_id": app.client_id,
        "redirect_uri": next(iter(app.redirect_uris)),
        "scope": "user.metrics",
        "state": "abc123",
    }
    page = await client.get("https://account.withings.com/oauth2_user/authorize2", params=query)
    assert page.status_code == 302


# ---------------------------------------------------------------------------- 120 a minute


async def test_an_application_gets_120_requests_in_a_minute() -> None:
    w = World()
    assert RATE_LIMIT_PER_MINUTE == 120
    statuses = [(await w.goals())["status"] for _ in range(RATE_LIMIT_PER_MINUTE + 1)]
    assert statuses == [0] * RATE_LIMIT_PER_MINUTE + [STATUS_RATE_LIMITED]
    refused = await w.goals()
    assert refused["status"] == STATUS_RATE_LIMITED
    assert "body" not in refused  # no wait_seconds: a client must back off on its own


async def test_the_bucket_refills_at_two_requests_a_second(w: World) -> None:
    for _ in range(RATE_LIMIT_PER_MINUTE):
        assert (await w.goals())["status"] == 0
    assert (await w.goals())["status"] == STATUS_RATE_LIMITED
    w.fake.advance(0.25)
    assert (await w.goals())["status"] == STATUS_RATE_LIMITED
    w.fake.advance(0.25)  # half a second in all: one token
    assert (await w.goals())["status"] == 0
    assert (await w.goals())["status"] == STATUS_RATE_LIMITED
    for _ in range(100):  # the sustained rate is exactly 120 a minute
        w.fake.advance(0.5)
        assert (await w.goals())["status"] == 0


async def test_a_minute_of_quiet_refills_the_whole_bucket(w: World) -> None:
    for _ in range(RATE_LIMIT_PER_MINUTE):
        await w.goals()
    w.fake.advance(60)
    statuses = [(await w.goals())["status"] for _ in range(RATE_LIMIT_PER_MINUTE + 1)]
    assert statuses == [0] * RATE_LIMIT_PER_MINUTE + [STATUS_RATE_LIMITED]
    w.fake.advance(3_600)  # an hour of quiet does not bank more than one bucket
    statuses = [(await w.goals())["status"] for _ in range(RATE_LIMIT_PER_MINUTE + 1)]
    assert statuses.count(0) == RATE_LIMIT_PER_MINUTE


async def test_the_limit_is_per_application_not_per_user(w: World) -> None:
    second = w.fake.add_user(days=3, ecg_count=0)
    other_tokens = w.fake.tokens_for(w.app.client_id, second.userid)
    other_app = w.fake.add_app("other-client", "other-secret", ["https://other.example.test/cb"])
    foreign = w.fake.tokens_for(other_app.client_id, second.userid)
    for _ in range(RATE_LIMIT_PER_MINUTE):
        await w.goals()
    shared = await w.send(
        "POST", "/v2/user", form={"action": "getgoals"}, token=other_tokens["access_token"]
    )
    assert shared["status"] == STATUS_RATE_LIMITED  # same app, another user
    own = await w.send(
        "POST", "/v2/user", form={"action": "getgoals"}, token=foreign["access_token"]
    )
    assert own["status"] == 0  # another app has its own bucket


async def test_every_call_that_names_the_application_counts(w: World) -> None:
    form = w.exchange_form("synthetic-wrong-code")  # a failed token request still counts
    for _ in range(60):
        assert (await w.send("POST", "/v2/oauth2", form=form))["status"] != STATUS_RATE_LIMITED
        assert (await w.goals())["status"] == 0
    assert (await w.send("POST", "/v2/oauth2", form=form))["status"] == STATUS_RATE_LIMITED
    assert (await w.goals())["status"] == STATUS_RATE_LIMITED
    assert w.fake.log[-1].api_status == STATUS_RATE_LIMITED


async def test_a_request_that_names_no_known_application_is_not_limited(w: World) -> None:
    for _ in range(RATE_LIMIT_PER_MINUTE + 10):
        reply = await w.send("POST", "/v2/user", form={"action": "getgoals"}, token="nobody")
        assert reply["status"] == STATUS_INVALID_TOKEN


async def test_the_limit_can_be_changed_or_switched_off() -> None:
    three = World(rate_limit=3)
    assert [(await three.goals())["status"] for _ in range(4)] == [0, 0, 0, STATUS_RATE_LIMITED]
    three.fake.limiter.per_minute = None
    assert (await three.goals())["status"] == 0
    unlimited = World(rate_limit=None)
    for _ in range(300):
        assert (await unlimited.goals())["status"] == 0


def test_the_bucket_starts_full_and_refills_evenly() -> None:
    limiter = RateLimiter(60)  # one a second
    assert [limiter.admit("app", 100.0) for _ in range(61)] == [True] * 60 + [False]
    assert limiter.admit("app", 100.5) is False
    assert limiter.admit("app", 101.0) is True
    assert limiter.admit("other", 100.0) is True  # buckets are per application
    assert RateLimiter(None).admit("app", 0.0) is True
