"""Reserve-first rate limits, release on success, client IP and the hashing pool (section 9)."""

import asyncio
import dataclasses
import ipaddress
import threading
import time
from datetime import timedelta

import pytest

from authtools import (  # noqa: F401 - fixtures
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
)
from ownvitals.auth.clientip import bucket_ip, client_ip
from ownvitals.auth.deps import request_ip
from ownvitals.auth.passwords import PasswordHashBusy, PasswordHasher
from ownvitals.auth.ratelimit import Bucket, Limit, RateLimited, RateLimiter, password_buckets

TEST_LIMIT = Limit("test", "pair", 5, 3600)


def limiter(env: Env) -> RateLimiter:
    return env.app.state.limiter


async def counts(env: Env) -> list[int]:
    async with env.db() as db:
        from sqlalchemy import text

        rows = await db.execute(text("SELECT count FROM rate_limits ORDER BY count"))
        return [row[0] for row in rows.all()]


async def test_parallel_attempts_do_not_overshoot_the_limit(env: Env) -> None:
    attempts = await asyncio.gather(
        *(limiter(env).begin([Bucket(TEST_LIMIT, "victim|1.2.3.4")]) for _ in range(40))
    )
    assert sum(a.allowed for a in attempts) == TEST_LIMIT.max_count
    assert all(a.retry_after >= 1 for a in attempts if not a.allowed)
    assert all(a.retry_after <= 3600 for a in attempts if not a.allowed)
    assert await counts(env) == [40]  # a refused attempt keeps its slot


async def test_the_limit_trips_and_a_new_window_resets_it(env: Env) -> None:
    bucket = Bucket(TEST_LIMIT, "someone")
    for _ in range(5):
        assert (await limiter(env).begin([bucket])).allowed
    assert not (await limiter(env).begin([bucket])).allowed
    await env.run("UPDATE rate_limits SET window_start = window_start - interval '1 hour'")
    assert (await limiter(env).begin([bucket])).allowed  # a fresh window starts at 1


async def test_success_deletes_the_pair_bucket_and_decrements_the_others(env: Env) -> None:
    buckets = password_buckets("a@example.com", "203.0.113.7", known_device=False)
    for _ in range(3):  # three earlier failures from the pair, the account and the IP
        await limiter(env).begin(buckets)
    attempt = await limiter(env).begin(buckets)
    assert attempt.allowed
    assert await counts(env) == [4, 4, 4]
    await attempt.succeed()
    # The (account, IP) bucket is gone; the account and IP ones lost exactly this reservation.
    assert await counts(env) == [3, 3]


async def test_release_never_touches_a_newer_window(env: Env) -> None:
    bucket = Bucket(TEST_LIMIT, "straddler")
    old = await limiter(env).begin([bucket])  # reserved in a window that then ends
    # Time passes: the old reservation now belongs to the previous window.
    await env.run("UPDATE rate_limits SET window_start = window_start - interval '1 hour'")
    old.slots = [
        dataclasses.replace(slot, window_start=slot.window_start - timedelta(hours=1))
        for slot in old.slots
    ]
    newer = await limiter(env).begin([bucket])  # the current window holds one failure
    assert newer.allowed
    assert await counts(env) == [1, 1]
    await old.succeed()
    # Only the old window's row was decremented; the newer window's failure still counts.
    assert await counts(env) == [0, 1]


async def test_decrement_never_goes_below_zero(env: Env) -> None:
    attempt = await limiter(env).begin([Bucket(TEST_LIMIT, "x")])
    await env.run("UPDATE rate_limits SET count = 0")
    await attempt.succeed()
    assert await counts(env) == [0]


async def test_a_known_device_reserves_no_per_account_slot(env: Env) -> None:
    unknown = password_buckets("a@example.com", "203.0.113.7", known_device=False)
    known = password_buckets("a@example.com", "203.0.113.7", known_device=True)
    assert [b.limit.scope for b in unknown] == ["pair", "account", "ip"]
    assert [b.limit.scope for b in known] == ["pair", "ip"]
    # 60 attempts from known devices never trip the 50/hour per-account cap.
    for ip in range(60):
        buckets = password_buckets("a@example.com", f"10.0.{ip}.1", known_device=True)
        assert (await limiter(env).begin(buckets)).allowed


async def test_the_adr_limits(env: Env) -> None:
    from ownvitals.auth import ratelimit as r

    table = {
        r.SIGN_IN_PAIR: (10, 900),
        r.SIGN_IN_ACCOUNT: (50, 3600),
        r.SIGN_IN_IP: (30, 900),
        r.MFA_PAIR: (5, 300),
        r.MFA_ACCOUNT: (20, 3600),
        r.MFA_IP: (20, 300),
        r.SIGNUP_ADDRESS: (3, 3600),
        r.SIGNUP_IP: (5, 3600),
        r.MAIL_PAIR: (3, 3600),
        r.MAIL_ACCOUNT: (10, 3600),
        r.MAIL_IP: (10, 3600),
        r.OAUTH_IP: (30, 900),
    }
    for limit, (count, window) in table.items():
        assert (limit.max_count, limit.window_seconds) == (count, window)


async def test_ipv6_addresses_share_a_bucket_per_64(env: Env) -> None:
    same = [
        "2001:db8:1:2::1",
        "2001:db8:1:2:ffff:ffff:ffff:ffff",
        "2001:0db8:0001:0002:abcd::1",
    ]
    assert len({bucket_ip(a) for a in same}) == 1
    assert bucket_ip("2001:db8:1:3::1") != bucket_ip(same[0])
    assert bucket_ip(same[0]) == "2001:db8:1:2::/64"
    assert bucket_ip("192.0.2.9") == "192.0.2.9"
    # End to end: rotating addresses inside one /64 does not escape the per-IP limit.
    results = []
    for i in range(35):
        buckets = password_buckets(
            f"user{i}@example.com", f"2001:db8:1:2::{i:x}", known_device=False
        )
        results.append((await limiter(env).begin(buckets)).allowed)
    assert results.count(True) == 30  # SIGN_IN_IP: 30 per 15 minutes


def test_an_ipv6_scope_id_is_dropped() -> None:
    assert bucket_ip("fe80::1%eth0") == bucket_ip("fe80::1")
    assert bucket_ip("fe80::1%eth0") == "fe80::/64"
    assert client_ip("fe80::2%12", [], []) == "fe80::2"


def test_an_ipv4_mapped_address_is_unmapped_before_bucketing() -> None:
    assert bucket_ip("::ffff:192.0.2.9") == "192.0.2.9"
    assert client_ip("::ffff:192.0.2.9", [], []) == "192.0.2.9"
    assert bucket_ip("::ffff:192.0.2.9") == bucket_ip("192.0.2.9")


PROXY = [ipaddress.ip_network("10.0.0.0/8")]


def test_the_peer_is_the_client_unless_it_is_a_trusted_proxy() -> None:
    assert client_ip("203.0.113.9", ["1.1.1.1"], PROXY) == "203.0.113.9"  # header ignored
    assert client_ip("203.0.113.9", ["1.1.1.1"], []) == "203.0.113.9"
    assert client_ip(None, [], PROXY) == "unknown"


def test_behind_a_trusted_proxy_the_rightmost_untrusted_entry_wins() -> None:
    spoofed = "6.6.6.6, 198.51.100.4, 10.1.1.1"
    assert client_ip("10.0.0.5", [spoofed], PROXY) == "198.51.100.4"
    assert client_ip("10.0.0.5", ["6.6.6.6", "198.51.100.4"], PROXY) == "198.51.100.4"
    assert client_ip("10.0.0.5", ["10.2.2.2"], PROXY) == "10.0.0.5"
    assert client_ip("10.0.0.5", [], PROXY) == "10.0.0.5"
    assert client_ip("10.0.0.5", ["not-an-ip"], PROXY) == "10.0.0.5"
    assert client_ip("10.0.0.5", ["::ffff:198.51.100.4"], PROXY) == "198.51.100.4"


async def test_trusted_proxies_are_read_from_the_settings(app_database_url: str) -> None:
    from types import SimpleNamespace

    from authtools import make_settings

    settings = make_settings(app_database_url, trusted_proxies="172.16.0.0/12, 192.0.2.1")
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(settings=settings)),
        client=SimpleNamespace(host="172.17.0.2"),
        headers=SimpleNamespace(getlist=lambda _name: ["198.51.100.77, 172.17.0.9"]),
    )
    assert request_ip(request) == "198.51.100.77"  # pyright: ignore[reportArgumentType]


async def test_a_tripped_limit_is_a_429_with_retry_after(env: Env) -> None:
    @env.app.get("/api/test/limited")
    async def limited() -> None:  # pyright: ignore[reportUnusedFunction]
        raise RateLimited(retry_after=42)

    response = await env.client.get("/api/test/limited")
    assert response.status_code == 429
    assert response.headers["retry-after"] == "42"


async def test_bucket_keys_hold_no_email_or_ip_in_the_clear(env: Env) -> None:
    await limiter(env).begin(
        password_buckets("victim@example.com", "203.0.113.7", known_device=False)
    )
    async with env.db() as db:
        from sqlalchemy import text

        rows = (
            await db.execute(text("SELECT encode(bucket_hash, 'escape') FROM rate_limits"))
        ).all()
    blob = "".join(row[0] for row in rows)
    assert "victim" not in blob
    assert "203.0.113" not in blob


async def test_a_busy_hashing_pool_answers_503_after_the_wait() -> None:
    """The first hash holds the only worker until the test releases it, so the slot is busy for
    exactly as long as the test needs (no race between a real Argon2id run and a fixed delay).
    The pool, the semaphore and the wait are the production ones."""
    hasher = PasswordHasher(workers=1, wait_seconds=0.01)
    inside = threading.Event()
    release = threading.Event()

    class Blocking:
        def hash(self, password: str) -> str:
            inside.set()
            assert release.wait(30)
            return "$argon2id$stub"

    hasher._argon2 = Blocking()  # type: ignore[assignment]  # pyright: ignore[reportPrivateUsage]
    try:
        slow = asyncio.create_task(hasher.hash("a long enough password"))
        assert await asyncio.to_thread(inside.wait, 30)  # the worker is now held
        started = time.monotonic()
        with pytest.raises(PasswordHashBusy):
            await hasher.hash("another long password")
        assert time.monotonic() - started < 1
        assert not slow.done()
        release.set()
        assert await slow == "$argon2id$stub"
        assert await hasher.hash("a third long password") == "$argon2id$stub"  # the slot came back
    finally:
        release.set()
        hasher.close()


async def test_the_pool_runs_at_most_workers_hashes_at_once() -> None:
    hasher = PasswordHasher(workers=2, wait_seconds=30)
    running = 0
    peak = 0
    inner = hasher._argon2  # pyright: ignore[reportPrivateUsage]

    class Watched:
        def hash(self, password: str) -> str:
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            try:
                return inner.hash(password)
            finally:
                running -= 1

    hasher._argon2 = Watched()  # type: ignore[assignment]  # pyright: ignore[reportPrivateUsage]
    try:
        await asyncio.gather(*(hasher.hash(f"password number {i} ok") for i in range(6)))
    finally:
        hasher.close()
    assert peak == 2


async def test_argon2id_parameters_verify_and_rehash() -> None:
    hasher = PasswordHasher(workers=2)
    try:
        hashed = await hasher.hash("correct horse battery")
        assert hashed.startswith("$argon2id$v=19$m=19456,t=2,p=1$")
        assert await hasher.verify(hashed, "correct horse battery")
        assert not await hasher.verify(hashed, "wrong password here")
        assert not await hasher.verify("not a hash", "correct horse battery")
        assert not hasher.needs_rehash(hashed)
        assert hasher.needs_rehash(
            "$argon2id$v=19$m=8,t=1,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
        )
        await hasher.verify_dummy("anything")
    finally:
        hasher.close()


def test_password_rules() -> None:
    from ownvitals.auth.passwords import PasswordRuleError, normalize_password

    with pytest.raises(PasswordRuleError):
        normalize_password("short")
    with pytest.raises(PasswordRuleError):
        normalize_password("x" * 129)
    assert normalize_password("x" * 12) == "x" * 12
    assert normalize_password("x" * 128) == "x" * 128
    fullwidth = "".join(chr(ord(c) + 0xFEE0) for c in "fullwidth1234")
    assert normalize_password(fullwidth) == "fullwidth1234"  # NFKC
