"""The Withings client's logs and exceptions never hold a token, a code, a secret, a nonce, a
callback URL, a measurement value or the provider's error text (AGENTS.md "Health data", PLAN
§4.2, ADR 0004 §9; #20's third acceptance criterion).

Every endpoint and every error path runs against the fake with every logger at DEBUG; then each
record (its message, arguments and every attribute) and each raised exception (``str``, ``repr``
and attributes) is searched for the secrets and values of the run. Synthetic data only.
"""

import logging
import urllib.request
from typing import Any

import pytest

from ownvitals import egress
from ownvitals.egress import core
from ownvitals.withings.client import RetryBudget, WithingsApp, WithingsClient
from ownvitals.withings.errors import WithingsError
from withingscore import FakeWithings
from withingsfake import FakeWithingsTransport
from withingsstore import add_group
from withingsstreams import START
from withingswire import FailureKind

HOOK_SECRET = "synthetic-hook-secret-0d1f"
HOOK = f"https://app.example.test/api/webhooks/withings/{HOOK_SECRET}"
PLANTED_VALUE = 9_876_543  # a measurement value that appears nowhere else
PROVIDER_TEXTS = (
    "injected failure",
    "invalid refresh token",
    "invalid_token",
    "unknown access token",
    "too many requests",
    "no such subscription",
    "callback URL",
    "scope not authorized",
    "upstream error",
    "invalid_params:",  # the fake's text; "invalid_params" alone is our category
    "wrong action",
)


@pytest.fixture(autouse=True)
def _worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urllib.request, "_opener", None)
    monkeypatch.setattr(core, "_worker_list", None)
    egress.install_worker_allowlist(ai_enabled=False)


def _text(value: Any) -> str:
    if isinstance(value, BaseException):
        return f"{value!s} {value!r} {vars(value)!r}"
    return repr(value)


async def test_no_secret_value_or_provider_text_reaches_a_log_or_an_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    fake = FakeWithings(seed=11)
    app = fake.add_app(client_secret="synthetic-client-secret-77aa")
    user = fake.add_user(days=3, ecg_count=1)
    add_group(
        user.data,
        {"date": START - 3600, "measures": [{"value": PLANTED_VALUE, "type": 1, "unit": -5}]},
        now=START - 60,
    )
    tokens = fake.tokens_for(app.client_id, user.userid)
    token, refresh = tokens["access_token"], tokens["refresh_token"]
    secrets = {token, refresh, app.client_secret, HOOK_SECRET}
    raised: list[BaseException] = []

    async def attempt(call: Any) -> Any:
        try:
            return await call
        except WithingsError as error:
            raised.append(error)
            return None

    withings = WithingsApp(app.client_id, app.client_secret)

    async def no_sleep(seconds: float) -> None:
        return None

    async with WithingsClient.open(
        withings, inner=FakeWithingsTransport(fake), sleep=no_sleep
    ) as client:
        # every endpoint, successfully
        body = await client.getmeas(token)
        assert any(x.value == PLANTED_VALUE for g in body.measuregrps for x in g.measures)
        listed = await client.heart_list(token)
        assert listed.series
        assert listed.series[0].ecg is not None
        await client.heart_get(token, listed.series[0].ecg.signalid)
        await client.getactivity(token, lastupdate=0)
        await client.getworkouts(token, lastupdate=0)
        await client.getintradayactivity(token)
        await client.sleep_getsummary(token, lastupdate=0)
        await client.getdevice(token)
        await client.getgoals(token)
        await client.notify_subscribe(token, callbackurl=HOOK, appli=1)
        await client.notify_list(token)
        await client.notify_update(token, callbackurl=HOOK, appli=1, comment="synthetic")
        # every error path
        for status in (401, 2553, 2555, 601, 214, 503, 286, 9999):
            fake.inject("status", at=fake.request_count + 1, status=status, wait_seconds=5)
            await attempt(client.getmeas(token))
        failures: list[tuple[FailureKind, dict[str, Any]]] = [
            ("http", {"http_status": 503}),
            ("http", {"http_status": 404}),
            ("garbage", {}),
            ("drop", {"when": "after"}),
            ("delay", {"seconds": 99}),
        ]
        for kind, opts in failures:
            fake.inject(kind, at=fake.request_count + 1, **opts)
            await attempt(client.getmeas(token, budget=RetryBudget(retries=1)))
        await attempt(client.notify_revoke(token, callbackurl=HOOK + "-unknown", appli=1))
        await attempt(client.notify_subscribe(token, callbackurl="http://bad.test/x", appli=1))
        await attempt(client.getmeas("synthetic-unknown-access-token"))
        await attempt(client.refresh("synthetic-unknown-refresh-token"))
        fake.inject("drop", at=fake.request_count + 1, when="after")
        await attempt(client.refresh(refresh))
        renewed = await client.refresh(refresh)  # the recovery within the grace
        secrets |= {
            renewed.access_token.get_secret_value(),
            renewed.refresh_token.get_secret_value(),
        }
        await client.notify_revoke(token, callbackurl=HOOK, appli=1)
        await client.revoke(user.userid)
        await attempt(client.revoke(user.userid))  # already revoked: 247

    assert len(raised) >= 15
    assert caplog.records, "the failures were logged"
    texts = [_text(r.getMessage()) + _text(vars(r)) for r in caplog.records]
    texts += [_text(e) for e in raised]
    forbidden = {*secrets, str(PLANTED_VALUE), *PROVIDER_TEXTS}
    leaks = {f for f in forbidden for t in texts if f in t}
    assert leaks == set()
    # the failure lines carry the category and numbers, nothing more
    failed = [r for r in caplog.records if r.getMessage() == "withings call failed"]
    assert failed
    for record in failed:
        assert {"endpoint", "category", "http_status", "api_status"} <= set(vars(record))
