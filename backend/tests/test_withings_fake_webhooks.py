"""The webhook sender of the Withings fake: only a 2xx answer within a few seconds is a success
(findings section 2), a redirect is not followed, and a late receiver fails the attempt (and gets
the retry schedule) whether it is slow in real time or on the fake clock. Each test plays a
receiver that does the wrong thing and shows the delivery fails. Fakes only, no network.
"""

from typing import Any

import pytest

from withingsfake import Delivery, call, deliver_webhook, fake_client, head_check_with, make_fake
from withingsreceiver import Receiver
from withingswire import WEBHOOK_ATTEMPTS_PER_CYCLE, WEBHOOK_RESPONSE_TIMEOUT

HOOK = "https://app.example.test/api/webhooks/withings/synthetic-secret"
HOOK_PATH = "/api/webhooks/withings/synthetic-secret"
ATTEMPTS = 2 * 5  # the whole schedule: 5 cycles of 2 attempts


class Rig:
    def __init__(self, *, follow_redirects: bool = False) -> None:
        self.fake, self.app, self.user = make_fake(seed=6, days=3, ecg_count=0)
        self.receiver = Receiver(self.fake)
        self.hook = self.receiver.client(follow_redirects=follow_redirects)
        self.client = fake_client(self.fake, head_check=head_check_with(self.receiver.client()))
        self.token = self.fake.tokens_for(self.app.client_id, self.user.userid)["access_token"]

    async def subscribe(self) -> None:
        reply = await call(
            self.client, "/notify", "subscribe", self.token, callbackurl=HOOK, appli=1
        )
        assert reply["status"] == 0

    async def deliver(self, **options: Any) -> list[Delivery]:
        return await deliver_webhook(
            self.fake, self.hook, self.user.userid, 1, startdate=1, enddate=2, **options
        )


@pytest.fixture
async def r() -> Rig:
    rig = Rig()
    await rig.subscribe()
    return rig


@pytest.mark.parametrize("status", [200, 201, 202, 204, 299])
async def test_a_2xx_answer_is_a_success_and_ends_the_retries(r: Rig, status: int) -> None:
    r.receiver.post_status = status
    sent = await r.deliver(retry=True)
    assert [d.status_code for d in sent] == [status]
    assert r.fake.subscriptions[0].failing_since is None


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308, 400, 401, 404, 410, 429, 500, 503])
async def test_any_other_status_is_a_failure_and_the_schedule_runs_out(r: Rig, status: int) -> None:
    """A receiver that redirects (a trailing slash, say) or refuses is not one that took it."""
    r.receiver.post_status = status
    sent = await r.deliver(retry=True)
    assert [d.status_code for d in sent] == [status] * ATTEMPTS
    assert r.fake.subscriptions[0].failing_since is not None
    assert len(r.receiver.posts) == ATTEMPTS


async def test_a_redirect_is_not_followed_even_by_a_client_that_would() -> None:
    rig = Rig(follow_redirects=True)
    await rig.subscribe()
    rig.receiver.post_status = 307
    rig.receiver.redirect_to = f"{HOOK}/"
    sent = await rig.deliver(retry=True)
    assert [d.status_code for d in sent] == [307] * ATTEMPTS
    assert rig.receiver.post_paths == [HOOK_PATH] * ATTEMPTS  # never the redirect target


def test_the_response_bound_is_a_few_seconds() -> None:
    assert WEBHOOK_RESPONSE_TIMEOUT == 3.0
    assert WEBHOOK_ATTEMPTS_PER_CYCLE == 2


@pytest.mark.parametrize(
    ("seconds", "on_time"),
    [(0, True), (1, True), (2.75, True), (3.0, True), (3.25, False), (10, False), (3_600, False)],
)
async def test_a_receiver_that_takes_longer_than_the_bound_fails_the_attempt(
    r: Rig, seconds: float, on_time: bool
) -> None:
    r.receiver.clock_seconds = seconds  # the handler is slow on the fake clock
    (sent,) = await r.deliver()
    assert sent.timed_out is (not on_time)
    assert sent.status_code == (200 if on_time else None)
    assert len(r.receiver.posts) == 1  # the receiver did get it: a late answer is not a lost one
    assert (r.fake.subscriptions[0].failing_since is None) is on_time


async def test_a_receiver_that_is_slow_in_real_time_fails_the_attempt(r: Rig) -> None:
    r.receiver.sleep_seconds = 30  # an awaited sleep stands for blocking on a database
    (sent,) = await r.deliver(response_timeout=0.05)
    assert (sent.status_code, sent.timed_out) == (None, True)
    assert len(r.receiver.posts) == 1  # it started, which is what makes a retry a duplicate
    r.receiver.sleep_seconds = 0.001
    (sent,) = await r.deliver(response_timeout=0.5)
    assert (sent.status_code, sent.timed_out) == (200, False)


async def test_a_late_receiver_gets_the_whole_retry_schedule_and_every_duplicate(r: Rig) -> None:
    r.receiver.clock_seconds = 4
    sent = await r.deliver(retry=True)
    assert [d.timed_out for d in sent] == [True] * ATTEMPTS
    assert [d.status_code for d in sent] == [None] * ATTEMPTS
    assert len(r.receiver.posts) == ATTEMPTS  # at least once: the same notification ten times
    assert len(r.fake.subscriptions) == 1  # kept, not redelivered either
