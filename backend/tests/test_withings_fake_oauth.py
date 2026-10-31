"""Token semantics of the Withings fake, tested on the fake itself (findings section 2): the
authorization code (30 s, single use), the access token (3 h), the refresh token (rotation, the
old one valid up to 8 h or until the new access token is first used, 1 year), scopes, revoke and
the demo account. A manual clock drives time; nothing touches the network.
"""

from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from withingsfake import call, fake_client, make_fake
from withingswire import (
    ACCESS_TTL,
    CODE_TTL,
    NONCE_TTL,
    REFRESH_GRACE,
    REFRESH_TTL,
    STATUS_BAD_CODE,
    STATUS_BAD_PARAMS,
    STATUS_BAD_REFRESH_TOKEN,
    STATUS_BAD_USERID,
    STATUS_INVALID_TOKEN,
    App,
    sign,
)

HOUR = 3_600


class Session:
    """A fake with one app and one user, and the calls the tests repeat."""

    def __init__(self) -> None:
        self.fake, self.app, self.user = make_fake(seed=11, days=10, ecg_count=1)
        self.client = fake_client(self.fake)

    async def exchange(self, userid: int | None = None, scope: str | None = None) -> dict[str, Any]:
        code = self.fake.authorize(
            self.app.client_id,
            userid or self.user.userid,
            scope=scope or "user.metrics,user.activity",
        )
        return await self.exchange_code(code)

    async def exchange_code(self, code: str, **override: str) -> dict[str, Any]:
        params = {
            "client_id": self.app.client_id,
            "client_secret": self.app.client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": next(iter(self.app.redirect_uris)),
        } | override
        return await call(self.client, "/v2/oauth2", "requesttoken", **params)

    async def refresh(self, refresh_token: str, app: App | None = None) -> dict[str, Any]:
        app = app or self.app
        return await call(
            self.client,
            "/v2/oauth2",
            "requesttoken",
            client_id=app.client_id,
            client_secret=app.client_secret,
            grant_type="refresh_token",
            refresh_token=refresh_token,
        )

    async def meas(self, access_token: str) -> int:
        return (await call(self.client, "/measure", "getmeas", access_token))["status"]

    async def nonce(self) -> str:
        stamp = int(self.fake.clock.now())
        reply = await call(
            self.client,
            "/v2/signature",
            "getnonce",
            client_id=self.app.client_id,
            timestamp=stamp,
            signature=sign(self.app.client_secret, "getnonce", self.app.client_id, stamp),
        )
        return reply["body"]["nonce"]

    async def revoke(self, nonce: str, signature: str | None = None) -> dict[str, Any]:
        signature = signature or sign(self.app.client_secret, "revoke", self.app.client_id, nonce)
        return await call(
            self.client,
            "/v2/oauth2",
            "revoke",
            client_id=self.app.client_id,
            userid=self.user.userid,
            nonce=nonce,
            signature=signature,
        )


@pytest.fixture
async def s() -> Session:
    return Session()


async def test_the_authorization_code_is_valid_for_30_seconds(s: Session) -> None:
    assert CODE_TTL == 30
    on_time = s.fake.authorize(s.app.client_id, s.user.userid)
    late = s.fake.authorize(s.app.client_id, s.user.userid)
    s.fake.advance(CODE_TTL - 1)
    assert (await s.exchange_code(on_time))["status"] == 0
    s.fake.advance(2)
    assert (await s.exchange_code(late))["status"] == STATUS_BAD_CODE


async def test_the_authorization_code_is_single_use_and_bound_to_app_and_redirect(
    s: Session,
) -> None:
    code = s.fake.authorize(s.app.client_id, s.user.userid)
    assert (await s.exchange_code(code, redirect_uri="https://elsewhere.example.test/cb"))[
        "status"
    ] == STATUS_BAD_CODE
    assert (await s.exchange_code(code, client_secret="wrong"))["status"] == STATUS_BAD_PARAMS
    assert (await s.exchange_code(code))["status"] == 0
    assert (await s.exchange_code(code))["status"] == STATUS_BAD_CODE
    other = s.fake.add_app("other-client", "other-secret", ["https://other.example.test/cb"])
    stolen = s.fake.authorize(s.app.client_id, s.user.userid)
    params = {"client_id": other.client_id, "client_secret": other.client_secret, "code": stolen}
    reply = await call(
        s.client,
        "/v2/oauth2",
        "requesttoken",
        grant_type="authorization_code",
        redirect_uri="https://other.example.test/cb",
        **params,
    )
    assert reply["status"] == STATUS_BAD_CODE


async def test_the_token_response_carries_what_the_docs_promise(s: Session) -> None:
    body = (await s.exchange(scope="user.metrics"))["body"]
    assert body["expires_in"] == ACCESS_TTL == 10_800
    assert body["userid"] == s.user.userid
    assert body["scope"] == "user.metrics"
    assert body["token_type"] == "Bearer"
    assert len(body["access_token"]) == len(body["refresh_token"]) == 40


async def test_the_access_token_lives_3_hours(s: Session) -> None:
    token = (await s.exchange())["body"]["access_token"]
    s.fake.advance(ACCESS_TTL - 1)
    assert await s.meas(token) == 0
    s.fake.advance(2)
    assert await s.meas(token) == STATUS_INVALID_TOKEN


async def test_a_refresh_rotates_the_pair(s: Session) -> None:
    first = (await s.exchange())["body"]
    second = (await s.refresh(first["refresh_token"]))["body"]
    assert second["refresh_token"] != first["refresh_token"]
    assert second["access_token"] != first["access_token"]
    assert second["userid"] == first["userid"]
    assert second["expires_in"] == ACCESS_TTL
    assert (await s.refresh(second["refresh_token"]))["status"] == 0


async def test_the_old_refresh_token_dies_when_the_new_access_token_is_first_used(
    s: Session,
) -> None:
    first = (await s.exchange())["body"]
    await s.refresh(first["refresh_token"])
    s.fake.advance(60)
    retried = await s.refresh(first["refresh_token"])  # nothing used yet: a retry works
    assert retried["status"] == 0
    assert await s.meas(retried["body"]["access_token"]) == 0  # the first use of the newest one
    assert (await s.refresh(first["refresh_token"]))["status"] == STATUS_BAD_REFRESH_TOKEN
    assert (await s.refresh(retried["body"]["refresh_token"]))["status"] == 0


async def test_a_retry_with_the_old_token_discards_the_pair_it_never_saw(s: Session) -> None:
    first = (await s.exchange())["body"]
    lost = (await s.refresh(first["refresh_token"]))["body"]  # the response is "lost"
    retried = (await s.refresh(first["refresh_token"]))["body"]
    assert retried["refresh_token"] not in (first["refresh_token"], lost["refresh_token"])
    assert await s.meas(lost["access_token"]) == STATUS_INVALID_TOKEN
    assert (await s.refresh(lost["refresh_token"]))["status"] == STATUS_BAD_REFRESH_TOKEN
    assert await s.meas(retried["access_token"]) == 0


async def test_the_old_refresh_token_stays_valid_up_to_8_hours(s: Session) -> None:
    assert REFRESH_GRACE == 8 * HOUR
    first = (await s.exchange())["body"]
    await s.refresh(first["refresh_token"])
    s.fake.advance(REFRESH_GRACE - 1)
    assert (await s.refresh(first["refresh_token"]))["status"] == 0
    s.fake.advance(2)  # the retry did not extend the window, which began at the first renewal
    assert (await s.refresh(first["refresh_token"]))["status"] == STATUS_BAD_REFRESH_TOKEN


async def test_using_an_older_access_token_does_not_end_the_grace_period(s: Session) -> None:
    first = (await s.exchange())["body"]
    assert await s.meas(first["access_token"]) == 0
    second = (await s.refresh(first["refresh_token"]))["body"]
    assert await s.meas(first["access_token"]) == 0  # valid until its own 3 h are over
    assert (await s.refresh(first["refresh_token"]))["status"] == 0
    assert second["access_token"] != first["access_token"]


async def test_the_refresh_token_lives_one_year(s: Session) -> None:
    assert REFRESH_TTL == 365 * 86_400
    first = (await s.exchange())["body"]
    s.fake.advance(REFRESH_TTL - 1)
    kept = (await s.refresh(first["refresh_token"]))["body"]
    assert kept["refresh_token"]
    old = (await s.exchange())["body"]
    s.fake.advance(REFRESH_TTL + 1)
    assert (await s.refresh(old["refresh_token"]))["status"] == STATUS_BAD_REFRESH_TOKEN


async def test_a_new_authorization_replaces_every_earlier_token(s: Session) -> None:
    first = (await s.exchange())["body"]
    second = (await s.exchange())["body"]
    assert await s.meas(first["access_token"]) == STATUS_INVALID_TOKEN
    assert (await s.refresh(first["refresh_token"]))["status"] == STATUS_BAD_REFRESH_TOKEN
    assert await s.meas(second["access_token"]) == 0


async def test_a_refresh_token_of_another_app_or_nobody_is_refused(s: Session) -> None:
    first = (await s.exchange())["body"]
    other = s.fake.add_app("other-client", "other-secret", ["https://other.example.test/cb"])
    assert (await s.refresh(first["refresh_token"], other))["status"] == STATUS_BAD_REFRESH_TOKEN
    assert (await s.refresh("0" * 40))["status"] == STATUS_BAD_REFRESH_TOKEN


async def test_scopes_limit_the_endpoints(s: Session) -> None:
    token = (await s.exchange(scope="user.info"))["body"]["access_token"]
    for path, action in (
        ("/measure", "getmeas"),
        ("/v2/sleep", "getsummary"),
        ("/v2/heart", "list"),
    ):
        assert (await call(s.client, path, action, token, lastupdate=0))["status"] == 214
    assert (await call(s.client, "/v2/user", "getdevice", token))["status"] == 0


async def test_revoke_needs_a_fresh_nonce_and_a_valid_signature_and_ends_the_grant(
    s: Session,
) -> None:
    tokens = (await s.exchange())["body"]
    nonce = await s.nonce()
    assert (await s.revoke(nonce, signature="0" * 64))["status"] == STATUS_BAD_PARAMS
    assert (await s.revoke(nonce))["status"] == STATUS_BAD_PARAMS  # the nonce is single use
    nonce = await s.nonce()
    assert (await s.revoke(nonce))["status"] == 0
    assert await s.meas(tokens["access_token"]) == STATUS_INVALID_TOKEN
    assert (await s.refresh(tokens["refresh_token"]))["status"] == STATUS_BAD_REFRESH_TOKEN
    assert (await s.revoke(await s.nonce()))[
        "status"
    ] == STATUS_BAD_USERID  # nothing left to revoke


async def test_a_nonce_expires_after_30_minutes(s: Session) -> None:
    await s.exchange()
    nonce = await s.nonce()
    s.fake.advance(NONCE_TTL + 1)
    assert (await s.revoke(nonce))["status"] == STATUS_BAD_PARAMS


async def test_the_nonce_needs_the_right_signature(s: Session) -> None:
    reply = await call(
        s.client,
        "/v2/signature",
        "getnonce",
        client_id=s.app.client_id,
        timestamp=1,
        signature="bad",
    )
    assert reply["status"] == STATUS_BAD_PARAMS


async def test_the_demo_account_answers_with_synthetic_data(s: Session) -> None:
    nonce = await s.nonce()
    reply = await call(
        s.client,
        "/v2/oauth2",
        "getdemoaccess",
        client_id=s.app.client_id,
        nonce=nonce,
        signature=sign(s.app.client_secret, "getdemoaccess", s.app.client_id, nonce),
        scope_oauth2="user.metrics",
    )
    assert reply["status"] == 0
    assert set(reply["body"]) == {"access_token", "refresh_token", "expires_in"}
    meas = await call(s.client, "/measure", "getmeas", reply["body"]["access_token"], lastupdate=0)
    assert meas["body"]["measuregrps"]


async def test_the_authorize_page_redirects_with_code_and_state(s: Session) -> None:
    redirect = next(iter(s.app.redirect_uris))
    query = {
        "response_type": "code",
        "client_id": s.app.client_id,
        "redirect_uri": redirect,
        "scope": "user.metrics,user.activity",
        "state": "abc123",
    }
    page = await s.client.get("https://account.withings.com/oauth2_user/authorize2", params=query)
    assert page.status_code == 302
    target = urlsplit(page.headers["location"])
    assert f"{target.scheme}://{target.netloc}{target.path}" == redirect
    got = parse_qs(target.query)
    assert got["state"] == ["abc123"]
    assert (await s.exchange_code(got["code"][0]))["status"] == 0
    s.fake.deny_next_authorization = True
    denied = await s.client.get("https://account.withings.com/oauth2_user/authorize2", params=query)
    assert parse_qs(urlsplit(denied.headers["location"]).query) == {
        "error": ["access_denied"],
        "state": ["abc123"],
    }
    bad = await s.client.get(
        "https://account.withings.com/oauth2_user/authorize2",
        params=query | {"redirect_uri": "https://elsewhere.example.test/cb"},
    )
    assert bad.status_code == 400


async def test_only_the_two_withings_hosts_over_https_are_reachable(s: Session) -> None:
    for url in (
        "https://scalews.withings.net/cgi-bin/measure",  # the private API (findings section 1)
        "https://example.com/v2/user",
        "http://wbsapi.withings.net/v2/user",
    ):
        with pytest.raises(httpx.ConnectError):
            await s.client.post(url, data={"action": "getgoals"})
    wrong_host = await s.client.get("https://wbsapi.withings.net/oauth2_user/authorize2")
    assert wrong_host.status_code == 404
