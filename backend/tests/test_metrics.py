"""Registry, label safety and the internal endpoint. Synthetic data only."""

import asyncio
import uuid

import pytest

from ownvitals import metrics as m
from ownvitals.withings import metrics as w


@pytest.fixture(autouse=True)
def _clean():
    m.REGISTRY.reset()
    yield
    m.REGISTRY.reset()


def test_every_label_name_is_allowed_and_values_are_closed():
    for metric in m.REGISTRY.metrics:
        for label, values in metric.labels.items():
            assert label in m.ALLOWED_LABELS, (metric.name, label)
            assert values, (metric.name, label)
            for value in values:
                # no identifier-, email- or number-shaped value can be declared
                assert "@" not in value, (metric.name, value)
                assert not value.isdigit(), (metric.name, value)
                with pytest.raises(ValueError, match="UUID"):
                    uuid.UUID(value)


@pytest.mark.parametrize(
    "bad", [str(uuid.uuid4()), "synthetic@example.test", "72.5", "12345", "measures "]
)
def test_label_values_outside_the_set_are_refused(bad):
    with pytest.raises(m.LabelError):
        w.record_last_sync_age(bad, 1)
    with pytest.raises(m.LabelError):
        w.stream_completeness.set(1, stream=bad, state="complete")


@pytest.mark.parametrize("name", ["user_id", "owner_id", "email", "value", "connection", "ts"])
def test_label_names_that_could_hold_identifiers_are_refused(name):
    with pytest.raises(m.LabelError):
        m.Gauge("ownvitals_x", "x", {name: ["a"]})


def test_open_vocabulary_is_refused():
    with pytest.raises(m.LabelError):
        m.Gauge("ownvitals_x", "x", {"stream": []})


def test_rendered_output_has_no_identifier_text():
    w.record_last_sync_age("measures", 12)
    out = m.REGISTRY.render()
    assert "measures" in out
    assert "owner" not in out.replace("ownvitals", "")


def test_webhook_age_moves():
    w.record_webhook(1_000_100, 1_000_000)
    w.record_webhook(1_000_000, 1_000_050)  # skew: age 0
    assert w.webhook_delivery_age.count() == 2
    assert 'le="300"} 2' in m.REGISTRY.render()


def test_last_sync_and_completeness_move():
    w.record_last_sync_age("ecg", 90)
    assert w.last_sync_success.get(stream="ecg") == 90
    w.set_completeness("ecg", {"partial": 2})
    assert w.stream_completeness.get(stream="ecg", state="partial") == 2
    assert w.stream_completeness.get(stream="ecg", state="complete") == 0


def test_subscriptions_reconcile_queue_refresh_api_move():
    w.set_subscription_counts({"active": 3, "failed": 1})
    assert w.subscriptions.get(state="failed") == 1
    w.record_reconcile(changed=4, deleted=2)
    assert w.reconcile_rows.get(kind="changed") == 4
    assert w.reconcile_rows.get(kind="deleted") == 2
    w.set_queue({"queued": 5, "dead_letter": 2}, 42)
    assert w.queue_jobs.get(state="dead_letter") == 2
    assert w.queue_age.get() == 42
    w.record_refresh_failure()
    w.record_refresh_recovery()
    assert w.token_refresh.get(result="failure") == 1
    assert w.token_refresh.get(result="recovered") == 1
    w.record_api_call(status=0)
    w.record_api_call(status=601)
    assert w.api_calls.get() == 2
    assert w.api_rate_limited.get() == 1


def test_counter_cannot_go_down():
    with pytest.raises(ValueError, match="only goes up"):
        w.api_calls.inc(-1)


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "::", "8.8.8.8", "example.com", "", "2001:4860:4860::8888"],  # noqa: S104
)
def test_bind_refuses_non_internal(host):
    with pytest.raises(m.BindRefused):
        m.check_bind(host)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "10.1.2.3", "192.168.0.5", "172.20.0.2"])
def test_bind_accepts_internal(host):
    m.check_bind(host)


async def _get(port: int, request: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    await writer.drain()
    data = await reader.read()
    writer.close()
    return data


async def test_endpoint_serves_metrics_only():
    w.record_api_call()
    server = await m.serve_metrics("127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        ok = await _get(port, b"GET /metrics HTTP/1.1\r\n\r\n")
        assert ok.startswith(b"HTTP/1.1 200")
        assert b"ownvitals_withings_api_calls_total 1" in ok
        assert (await _get(port, b"GET / HTTP/1.1\r\n\r\n")).startswith(b"HTTP/1.1 404")
        assert (await _get(port, b"POST /metrics HTTP/1.1\r\n\r\n")).startswith(b"HTTP/1.1 405")
    finally:
        server.close()
        await server.wait_closed()
