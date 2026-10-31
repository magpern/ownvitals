"""Callback URL rules of the Withings fake (HTTPS, port 443, a domain name, at most 255 characters),
where the HEAD check sits in ``notify subscribe`` and ``update``, and the masking of the callback
URL in the request log. Each refusal is shown by a client that does the wrong thing and fails.
Fakes only, no network.
"""

import asyncio
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest

from withingscore import FakeWithings
from withingsfake import call, fake_client, head_check_with, make_fake
from withingsnotify import HeadCheck
from withingsreceiver import Receiver
from withingsrules import callback_url_problem
from withingswire import (
    ACCESS_TTL,
    API_HOST,
    STATUS_BAD_CALLBACK,
    STATUS_BAD_PARAMS,
    STATUS_INVALID_TOKEN,
    STATUS_NO_SCOPE,
    STATUS_NO_SUBSCRIPTION,
    STATUS_NOT_IMPLEMENTED,
    STATUS_UNKNOWN,
    Request,
)

HOOK = "https://app.example.test/api/webhooks/withings/synthetic-secret"
HOOK_PATH = "/api/webhooks/withings/synthetic-secret"
OTHER = "https://app.example.test/api/webhooks/withings/second-secret"


class Rig:
    def __init__(self) -> None:
        self.fake, self.app, self.user = make_fake(seed=5, days=3, ecg_count=0)
        self.receiver = Receiver(self.fake)
        self.client = fake_client(self.fake, head_check=head_check_with(self.receiver.client()))
        self.token = self.fake.tokens_for(self.app.client_id, self.user.userid)["access_token"]

    async def notify(self, action: str, token: str | None = None, **params: Any) -> dict[str, Any]:
        return await call(self.client, "/notify", action, token or self.token, **params)


@pytest.fixture
def r() -> Rig:
    return Rig()


# ------------------------------------------------------------------------- the URL rules

REFUSED = {
    "plain http on a loopback address": "http://127.0.0.1:8000/hook",
    "https on a loopback address": "https://127.0.0.1/hook",
    "an IPv6 literal": "https://[::1]/hook",
    "a LAN address": "https://192.168.1.222/hook",
    "localhost": "https://localhost/hook",
    "a .localhost name": "https://app.localhost/hook",
    "http on a domain": "http://app.example.test/hook",
    "port 8443": "https://app.example.test:8443/hook",
    "port 80": "https://app.example.test:80/hook",
    "a name with one label": "https://backend/hook",
    "an address as one number": "https://2130706433/hook",
    "a short IPv4 form": "https://127.1/hook",
    "another scheme": "ftp://app.example.test/hook",
    "no scheme": "app.example.test/hook",
    "no host": "https:///hook",
    "a port out of range": "https://app.example.test:99999/hook",
    "an empty label": "https://app..example.test/hook",
    "a trailing dot": "https://app.example.test./hook",
}
ACCEPTED = {
    "https on a domain": "https://app.example.test/hook",
    "an explicit port 443": "https://app.example.test:443/hook",
    "upper case": "HTTPS://App.Example.TEST/hook",
    "a punycode name": "https://xn--e1afmkfd.xn--p1ai/hook",
    "the shape of the real one": HOOK,
}


@pytest.mark.parametrize("url", REFUSED.values(), ids=REFUSED.keys())
async def test_a_callback_url_that_breaks_the_rules_is_refused_without_a_head(
    r: Rig, url: str
) -> None:
    assert callback_url_problem(url) is not None
    assert (await r.notify("subscribe", callbackurl=url, appli=1))["status"] == STATUS_BAD_CALLBACK
    assert r.fake.subscriptions == []
    assert r.receiver.heads == []  # refused before Withings would have looked at it
    await r.notify("subscribe", callbackurl=HOOK, appli=1)
    moved = await r.notify("update", callbackurl=HOOK, appli=1, new_callbackurl=url)
    assert moved["status"] == STATUS_BAD_CALLBACK
    assert [s.callbackurl for s in r.fake.subscriptions] == [HOOK]
    assert r.receiver.heads == [HOOK_PATH]  # the one HEAD of the valid subscribe


@pytest.mark.parametrize("url", ACCEPTED.values(), ids=ACCEPTED.keys())
async def test_a_callback_url_that_follows_the_rules_is_accepted(r: Rig, url: str) -> None:
    assert callback_url_problem(url) is None
    assert (await r.notify("subscribe", callbackurl=url, appli=1))["status"] == 0
    assert [s.callbackurl for s in r.fake.subscriptions] == [url]
    assert len(r.receiver.heads) == 1


async def test_a_client_that_builds_a_local_http_subscription_url_fails(r: Rig) -> None:
    """The mistake #24 and #25 must not make: a development URL on the wire."""
    reply = await r.notify("subscribe", callbackurl="http://127.0.0.1:8000/hook", appli=1)
    assert reply["status"] == STATUS_BAD_CALLBACK
    assert "callback" in reply["error"]
    assert (r.fake.subscriptions, r.receiver.heads) == ([], [])


def test_the_reasons_name_the_rule_that_failed() -> None:
    assert callback_url_problem("") == "empty or longer than 255 characters"
    assert callback_url_problem("http://app.example.test/") == "not HTTPS"
    assert callback_url_problem("https://app.example.test:8443/") == "port is not 443"
    assert callback_url_problem("https://10.0.0.1/") == "an IP address, not a domain name"
    assert callback_url_problem("https://localhost/") == "localhost"
    assert callback_url_problem("https://backend/") == "not a domain name"


# ----------------------------------------------------- where the HEAD check sits in the call


async def test_no_head_is_sent_for_a_request_that_fails_anyway(r: Rig) -> None:
    """Withings HEADs the URL as part of creating the subscription, not for a bad request."""
    narrow = r.fake.tokens_for(r.app.client_id, r.fake.add_user().userid, "user.activity")
    await r.notify("subscribe", callbackurl=HOOK, appli=1)  # one HEAD, for later updates
    heads = list(r.receiver.heads)
    failing = [
        (await r.notify("subscribe", "nobody", callbackurl=HOOK, appli=1), STATUS_INVALID_TOKEN),
        (await r.notify("subscribe", callbackurl=HOOK, appli=999), STATUS_BAD_PARAMS),
        (await r.notify("subscribe", callbackurl=HOOK), STATUS_BAD_PARAMS),
        (
            await r.notify("subscribe", narrow["access_token"], callbackurl=HOOK, appli=1),
            STATUS_NO_SCOPE,
        ),
        (await r.notify("subscribe", callbackurl="a" * 300, appli=1), STATUS_BAD_CALLBACK),
        (
            await r.notify("update", callbackurl=OTHER, appli=1, new_callbackurl=OTHER),
            STATUS_NO_SUBSCRIPTION,
        ),
        (
            await r.notify("update", callbackurl=HOOK, appli=1, new_callbackurl=OTHER, new_appli=0),
            STATUS_BAD_PARAMS,
        ),
    ]
    assert [reply["status"] for reply, _ in failing] == [status for _, status in failing]
    r.fake.inject("status", at=r.fake.request_count + 1, status=STATUS_UNKNOWN)
    assert (await r.notify("subscribe", callbackurl=OTHER, appli=4))["status"] == STATUS_UNKNOWN
    get = await r.client.get(f"https://{API_HOST}/notify", params={"action": "subscribe"})
    assert get.json()["status"] == STATUS_NOT_IMPLEMENTED
    r.fake.advance(ACCESS_TTL + 1)  # the token expires
    expired = await r.notify("subscribe", callbackurl=OTHER, appli=4)
    assert expired["status"] == STATUS_INVALID_TOKEN
    assert r.receiver.heads == heads  # not one HEAD for any of them
    assert len(r.fake.subscriptions) == 1


async def test_the_head_comes_last_just_before_the_subscription_exists(r: Rig) -> None:
    assert (await r.notify("subscribe", callbackurl=HOOK, appli=1))["status"] == 0
    assert (r.receiver.heads, r.receiver.subscriptions_at_head) == ([HOOK_PATH], [0])
    assert len(r.fake.subscriptions) == 1
    moved = await r.notify("update", callbackurl=HOOK, appli=1, new_callbackurl=OTHER)
    assert moved["status"] == 0
    assert r.receiver.heads[-1] == "/api/webhooks/withings/second-secret"
    assert r.receiver.subscriptions_at_head[-1] == 1  # the old URL was still in place
    heads = len(r.receiver.heads)
    comment = await r.notify("update", callbackurl=OTHER, appli=1, comment="only the comment")
    assert comment["status"] == 0
    assert len(r.receiver.heads) == heads  # no new URL, nothing to HEAD


async def test_a_head_that_fails_refuses_the_subscription_after_one_head(r: Rig) -> None:
    r.receiver.head_status = 503
    assert (await r.notify("subscribe", callbackurl=HOOK, appli=1))["status"] == STATUS_BAD_CALLBACK
    assert (r.receiver.heads, r.fake.subscriptions) == ([HOOK_PATH], [])


async def test_a_response_lost_after_the_head_leaves_the_subscription_in_place(r: Rig) -> None:
    r.fake.inject("drop", at=r.fake.request_count + 1, when="after")
    with pytest.raises(httpx.ReadError):
        await r.notify("subscribe", callbackurl=HOOK, appli=1)
    assert r.receiver.heads == [HOOK_PATH]  # Withings did HEAD it
    assert [s.callbackurl for s in r.fake.subscriptions] == [HOOK]  # and created it
    r.fake.inject("drop", at=r.fake.request_count + 1)
    with pytest.raises(httpx.ConnectError):
        await r.notify("subscribe", callbackurl=OTHER, appli=1)
    assert len(r.receiver.heads) == 1  # the request never arrived: no HEAD, no subscription
    assert len(r.fake.subscriptions) == 1


async def test_an_error_replacing_the_answer_after_the_head_is_ambiguous_too(r: Rig) -> None:
    r.fake.inject("status", at=r.fake.request_count + 1, when="after", status=STATUS_UNKNOWN)
    assert (await r.notify("subscribe", callbackurl=HOOK, appli=1))["status"] == STATUS_UNKNOWN
    assert (len(r.receiver.heads), len(r.fake.subscriptions)) == (1, 1)


async def test_requests_that_overlap_while_withings_heads_do_not_trample_each_other(r: Rig) -> None:
    async def slow_head(url: str) -> int:
        await asyncio.sleep(0.01)  # both requests are past their checks when the HEAD returns
        return 200

    client = fake_client(r.fake, head_check=slow_head)

    async def notify(action: str, **params: Any) -> dict[str, Any]:
        return await call(client, "/notify", action, r.token, **params)

    both = await asyncio.gather(
        notify("subscribe", callbackurl=HOOK, appli=1, comment="a"),
        notify("subscribe", callbackurl=HOOK, appli=1, comment="b"),
    )
    assert [reply["status"] for reply in both] == [0, 0]
    assert len(r.fake.subscriptions) == 1  # the second one found the first when it finished
    moved, revoked = await asyncio.gather(
        notify("update", callbackurl=HOOK, appli=1, new_callbackurl=OTHER),
        notify("revoke", callbackurl=HOOK, appli=1),
    )
    assert (moved["status"], revoked["status"]) == (STATUS_NO_SUBSCRIPTION, 0)
    assert r.fake.subscriptions == []  # nothing resurrected, nothing left half-moved


def test_a_synchronous_caller_resolves_the_head_check_itself() -> None:
    """How the e2e service (#36) drives the core: ``handle`` takes the HEAD status or a default."""
    fake = FakeWithings()
    app = fake.add_app()
    user = fake.add_user(days=3, ecg_count=0)
    token = fake.tokens_for(app.client_id, user.userid)["access_token"]
    form = urlencode({"action": "subscribe", "callbackurl": HOOK, "appli": 1}).encode()
    request = Request(
        "POST", f"https://{API_HOST}/notify", {"authorization": f"Bearer {token}"}, form
    )
    assert fake.handle(request, head_status=404).json()["status"] == STATUS_BAD_CALLBACK
    assert fake.subscriptions == []
    step = fake.start(request)
    assert isinstance(step, HeadCheck)
    assert (step.url, fake.subscriptions) == (HOOK, [])  # nothing yet: Withings is HEADing
    assert step.finish(204).json()["status"] == 0
    assert len(fake.subscriptions) == 1
    fake.default_head_status = 500
    other = Request(
        "POST",
        f"https://{API_HOST}/notify",
        {"authorization": f"Bearer {token}"},
        urlencode({"action": "subscribe", "callbackurl": OTHER, "appli": 4}).encode(),
    )
    assert fake.handle(other).json()["status"] == STATUS_BAD_CALLBACK


# ------------------------------------------------------------------- the request log


async def test_the_log_masks_the_callback_url_that_carries_the_webhook_secret(r: Rig) -> None:
    await r.notify("subscribe", callbackurl=HOOK, appli=1)
    await r.notify("update", callbackurl=HOOK, appli=1, new_callbackurl=OTHER)
    subscribe, update = r.fake.log[-2:]
    assert subscribe.params["callbackurl"] == "***"
    assert update.params["callbackurl"] == update.params["new_callbackurl"] == "***"
    assert (subscribe.params["appli"], update.params["appli"]) == ("1", "1")
    dump = repr(r.fake.log)
    assert "synthetic-secret" not in dump
    assert "second-secret" not in dump
    assert [s.callbackurl for s in r.fake.subscriptions] == [OTHER]  # the state keeps the URL
