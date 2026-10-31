"""``notify`` of the Withings fake (subscribe, get, list, update, revoke, the HEAD check) and the
webhook sender helper with Withings' retry schedule. The receiver is a tiny in-process ASGI app;
nothing touches the network.
"""

from collections.abc import MutableMapping
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest

from withingscore import FakeWithings
from withingsfake import FakeWithingsTransport, call, deliver_webhook, head_check_with, make_fake
from withingswire import (
    STATUS_BAD_CALLBACK,
    STATUS_BAD_PARAMS,
    STATUS_NO_SCOPE,
    STATUS_NO_SUBSCRIPTION,
    SUBSCRIPTION_CANCEL_AFTER,
    WEBHOOK_ATTEMPTS_PER_CYCLE,
    WEBHOOK_RETRY_DELAYS,
    webhook_form,
)

HOOK = "https://app.example.test/api/webhooks/withings/synthetic-secret"
DAY = 86_400


class Receiver:
    """A stand-in for the webhook receiver: answers HEAD and POST, records what it got."""

    def __init__(self) -> None:
        self.heads: list[str] = []
        self.posts: list[dict[str, str]] = []
        self.head_status = 200
        self.failing_posts = 0  # the next N POSTs answer 500

    async def __call__(self, scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        if scope["method"] == "HEAD":
            self.heads.append(scope["path"])
            status = self.head_status
        else:
            self.posts.append(dict(parse_qsl(body.decode())))
            status = 500 if self.failing_posts > 0 else 200
            self.failing_posts = max(0, self.failing_posts - 1)
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b""})


class World:
    def __init__(self) -> None:
        self.fake, self.app, self.user = make_fake(seed=2, days=5, ecg_count=1)
        self.receiver = Receiver()
        self.hook_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.receiver), base_url="https://app.example.test"
        )
        transport = FakeWithingsTransport(self.fake, head_check=head_check_with(self.hook_client))
        self.client = httpx.AsyncClient(transport=transport)
        self.token = self.fake.tokens_for(self.app.client_id, self.user.userid)["access_token"]

    async def notify(self, action: str, **params: Any) -> dict[str, Any]:
        return await call(self.client, "/notify", action, self.token, **params)


@pytest.fixture
def w() -> World:
    return World()


async def test_subscribe_makes_withings_send_a_head_to_the_callback_url(w: World) -> None:
    reply = await w.notify("subscribe", callbackurl=HOOK, appli=1, comment="synthetic")
    assert reply == {"status": 0, "body": {}}
    assert w.receiver.heads == ["/api/webhooks/withings/synthetic-secret"]
    assert [(s.appli, s.callbackurl, s.comment) for s in w.fake.subscriptions] == [
        (1, HOOK, "synthetic")
    ]


@pytest.mark.parametrize("head_status", [301, 403, 404, 500])
async def test_a_callback_that_does_not_answer_head_with_2xx_is_refused(
    w: World, head_status: int
) -> None:
    w.receiver.head_status = head_status
    reply = await w.notify("subscribe", callbackurl=HOOK, appli=1)
    assert reply["status"] == STATUS_BAD_CALLBACK
    assert w.fake.subscriptions == []


async def test_the_callback_url_is_at_most_255_characters(w: World) -> None:
    longest = "https://app.example.test/" + "a" * (255 - len("https://app.example.test/"))
    assert len(longest) == 255
    assert (await w.notify("subscribe", callbackurl=longest, appli=1))["status"] == 0
    too_long = longest + "a"
    assert (await w.notify("subscribe", callbackurl=too_long, appli=4))[
        "status"
    ] == STATUS_BAD_CALLBACK
    assert (await w.notify("subscribe", callbackurl="", appli=4))["status"] == STATUS_BAD_CALLBACK


async def test_without_a_head_checker_the_default_status_decides() -> None:
    fake, app, user = make_fake(days=3, ecg_count=0)
    token = fake.tokens_for(app.client_id, user.userid)["access_token"]
    client = httpx.AsyncClient(transport=FakeWithingsTransport(fake))
    assert (await call(client, "/notify", "subscribe", token, callbackurl=HOOK, appli=1))[
        "status"
    ] == 0
    fake.default_head_status = 404
    reply = await call(client, "/notify", "subscribe", token, callbackurl=HOOK, appli=4)
    assert reply["status"] == STATUS_BAD_CALLBACK


async def test_list_and_get_return_what_was_subscribed(w: World) -> None:
    await w.notify("subscribe", callbackurl=HOOK, appli=1, comment="weight")
    await w.notify("subscribe", callbackurl=HOOK, appli=44)
    everything = (await w.notify("list"))["body"]["profiles"]
    assert {(p["appli"], p["comment"]) for p in everything} == {(1, "weight"), (44, "")}
    only = (await w.notify("list", appli=44))["body"]["profiles"]
    assert [p["appli"] for p in only] == [44]
    got = (await w.notify("get", callbackurl=HOOK, appli=1))["body"]
    assert got == {"appli": 1, "callbackurl": HOOK, "comment": "weight"}
    assert (await w.notify("get", callbackurl=HOOK))["body"]["appli"] == 1  # appli 1 by default
    assert (await w.notify("get", callbackurl=HOOK, appli=4))["status"] == STATUS_NO_SUBSCRIPTION


async def test_update_moves_a_subscription_and_checks_the_new_url(w: World) -> None:
    await w.notify("subscribe", callbackurl=HOOK, appli=1)
    other = "https://app.example.test/api/webhooks/withings/second-secret"
    assert (await w.notify("update", callbackurl=HOOK, appli=1, new_callbackurl=other))[
        "status"
    ] == 0
    assert w.receiver.heads[-1] == "/api/webhooks/withings/second-secret"
    assert [s.callbackurl for s in w.fake.subscriptions] == [other]
    assert (await w.notify("update", callbackurl=other, appli=1, new_appli=4, comment="bp"))[
        "status"
    ] == 0
    assert [(s.appli, s.comment) for s in w.fake.subscriptions] == [(4, "bp")]
    w.receiver.head_status = 404
    refused = await w.notify("update", callbackurl=other, appli=4, new_callbackurl=HOOK)
    assert refused["status"] == STATUS_BAD_CALLBACK
    assert [s.callbackurl for s in w.fake.subscriptions] == [other]
    missing = await w.notify("update", callbackurl=HOOK, appli=4, new_appli=1)
    assert missing["status"] == STATUS_NO_SUBSCRIPTION


async def test_revoke_removes_the_subscription(w: World) -> None:
    await w.notify("subscribe", callbackurl=HOOK, appli=1)
    await w.notify("subscribe", callbackurl=HOOK, appli=4)
    assert (await w.notify("revoke", callbackurl=HOOK, appli=1))["status"] == 0
    assert [s.appli for s in w.fake.subscriptions] == [4]
    assert (await w.notify("revoke", callbackurl=HOOK, appli=1))["status"] == STATUS_NO_SUBSCRIPTION
    assert (await w.notify("revoke", callbackurl=HOOK))["status"] == 0  # every appli of that URL
    assert w.fake.subscriptions == []


async def test_an_unknown_appli_or_a_missing_scope_is_refused(w: World) -> None:
    assert (await w.notify("subscribe", callbackurl=HOOK, appli=999))["status"] == STATUS_BAD_PARAMS
    narrow = w.fake.tokens_for(w.app.client_id, w.fake.add_user().userid, "user.activity")
    reply = await call(
        w.client, "/notify", "subscribe", narrow["access_token"], callbackurl=HOOK, appli=1
    )
    assert reply["status"] == STATUS_NO_SCOPE  # appli 1 is user.metrics
    ok = await call(
        w.client, "/notify", "subscribe", narrow["access_token"], callbackurl=HOOK, appli=16
    )
    assert ok["status"] == 0


async def test_subscriptions_belong_to_one_user_and_app(w: World) -> None:
    await w.notify("subscribe", callbackurl=HOOK, appli=1)
    second = w.fake.add_user()
    token = w.fake.tokens_for(w.app.client_id, second.userid)["access_token"]
    assert (await call(w.client, "/notify", "list", token))["body"]["profiles"] == []
    reply = await call(w.client, "/notify", "revoke", token, callbackurl=HOOK, appli=1)
    assert reply["status"] == STATUS_NO_SUBSCRIPTION
    assert len(w.fake.subscriptions) == 1


@pytest.mark.parametrize(
    ("appli", "fields", "expected"),
    [
        (1, {"startdate": 10, "enddate": 20}, {"startdate": "10", "enddate": "20"}),
        (4, {"startdate": 10, "enddate": 20}, {"startdate": "10", "enddate": "20"}),
        (54, {"startdate": 10, "enddate": 20}, {"startdate": "10", "enddate": "20"}),
        (44, {"startdate": 10, "enddate": 20}, {"startdate": "10", "enddate": "20"}),  # no date
        (16, {"date": "2026-10-30"}, {"date": "2026-10-30"}),
        (46, {}, {"action": "update"}),
        (50, {"date": 1793300000, "deviceid": "d"}, {"date": "1793300000", "deviceid": "d"}),
    ],
)
def test_the_webhook_form_follows_the_notification_content_page(
    appli: int, fields: dict[str, Any], expected: dict[str, str]
) -> None:
    form = webhook_form(appli, 42, **fields)
    assert form == {"userid": "42", "appli": str(appli), **expected}


def test_appli_53_has_no_user_and_an_unknown_appli_is_an_error() -> None:
    assert webhook_form(53, mac="00:00:00:00:00:00") == {"appli": "53", "mac": "00:00:00:00:00:00"}
    with pytest.raises(ValueError, match="unknown appli"):
        webhook_form(999, 42)


async def test_a_webhook_is_posted_as_a_form_to_the_subscribed_urls_only(w: World) -> None:
    await w.notify("subscribe", callbackurl=HOOK, appli=1)
    await w.notify("subscribe", callbackurl=HOOK, appli=44)
    sent = await deliver_webhook(
        w.fake, w.hook_client, w.user.userid, 1, startdate=1793300000, enddate=1793300600
    )
    assert [d.status_code for d in sent] == [200]
    assert w.receiver.posts == [
        {
            "userid": str(w.user.userid),
            "appli": "1",
            "startdate": "1793300000",
            "enddate": "1793300600",
        }
    ]
    assert (
        await deliver_webhook(w.fake, w.hook_client, w.user.userid, 4, startdate=1, enddate=2) == []
    )
    forged = await deliver_webhook(w.fake, w.hook_client, w.user.userid, 1, form={"userid": "x"})
    assert [d.status_code for d in forged] == [200]
    assert w.receiver.posts[-1] == {"userid": "x"}


async def test_the_retry_schedule_is_five_cycles_of_two_attempts(w: World) -> None:
    assert WEBHOOK_RETRY_DELAYS == (10, 60, 3_600, 14_400)
    assert WEBHOOK_ATTEMPTS_PER_CYCLE == 2
    await w.notify("subscribe", callbackurl=HOOK, appli=1)
    w.receiver.failing_posts = 999
    start = w.fake.clock.now()
    sent = await deliver_webhook(
        w.fake, w.hook_client, w.user.userid, 1, retry=True, startdate=1, enddate=2
    )
    assert len(sent) == 10
    offsets = [d.at - start for d in sent]
    assert offsets == [0, 0, 10, 10, 70, 70, 3_670, 3_670, 18_070, 18_070]  # about 5 h in all
    assert {d.status_code for d in sent} == {500}
    assert len(w.fake.subscriptions) == 1  # kept; not redelivered either


async def test_a_retry_stops_at_the_first_success(w: World) -> None:
    await w.notify("subscribe", callbackurl=HOOK, appli=1)
    for failing, expected in ((2, [500, 500, 200]), (3, [500, 500, 500, 200]), (0, [200])):
        w.receiver.failing_posts = failing
        sent = await deliver_webhook(
            w.fake, w.hook_client, w.user.userid, 1, retry=True, startdate=1, enddate=2
        )
        assert [d.status_code for d in sent] == expected


async def test_twenty_days_of_failures_cancel_the_subscription(w: World) -> None:
    await w.notify("subscribe", callbackurl=HOOK, appli=1)
    w.receiver.failing_posts = 10_000
    assert SUBSCRIPTION_CANCEL_AFTER == 20 * DAY
    for _ in range(20):  # deliveries on days 0 to 19
        await deliver_webhook(w.fake, w.hook_client, w.user.userid, 1, startdate=1, enddate=2)
        w.fake.advance(DAY)
    assert len(w.fake.subscriptions) == 1
    await deliver_webhook(w.fake, w.hook_client, w.user.userid, 1, startdate=1, enddate=2)
    assert w.fake.subscriptions == []


async def test_a_success_resets_the_failure_clock(w: World) -> None:
    await w.notify("subscribe", callbackurl=HOOK, appli=1)
    w.receiver.failing_posts = 1
    await deliver_webhook(w.fake, w.hook_client, w.user.userid, 1, startdate=1, enddate=2)
    assert w.fake.subscriptions[0].failing_since is not None
    w.fake.advance(15 * DAY)
    await deliver_webhook(w.fake, w.hook_client, w.user.userid, 1, startdate=1, enddate=2)
    assert w.fake.subscriptions[0].failing_since is None


def test_the_fake_keeps_no_state_between_instances() -> None:
    first, second = FakeWithings(seed=1), FakeWithings(seed=1)
    first.add_app()
    assert second.apps == {}
    assert second.subscriptions == []
