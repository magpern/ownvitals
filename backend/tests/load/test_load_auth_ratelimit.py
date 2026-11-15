"""Load test of the auth rate limits (#462; ADR 0003 section 9).

Many concurrent attempts, each a real ``RateLimiter.begin`` on its own pooled app-role
connection, against the Postgres ``rate_limits`` table: sign-in (and password re-auth, which
reserves the same buckets), password reset, and the TOTP step-up. Each is run from many IPs
on one account, from one IP across many accounts, and from one account on one IP (the pair).

The admission bound is exact. An attempt is admitted while the bucket that binds the scenario
is within its limit, so the admitted count is, per fixed window of that bucket, the smaller of
its reservations and its limit: the limit itself unless the run crosses a window boundary, which
the per-window sum makes exact too. The limits are pinned here as numbers: a loosened constant
fails as surely as a loosened upsert.

Synthetic addresses only (``example.test``, RFC 5737 and RFC 3849 documentation ranges).
"""

import asyncio
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

import pytest
from loadtools import Profile, Report, profiles, record, resolve
from sqlalchemy import create_engine, text

from authtools import (  # noqa: F401 - fixtures
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
)
from ownvitals.auth.clientip import bucket_ip
from ownvitals.auth.ratelimit import (
    MAIL_ACCOUNT,
    MAIL_IP,
    MAIL_PAIR,
    MFA_ACCOUNT,
    MFA_IP,
    MFA_PAIR,
    SIGN_IN_ACCOUNT,
    SIGN_IN_IP,
    SIGN_IN_PAIR,
    Bucket,
    Limit,
    RateLimiter,
    mfa_buckets,
    password_buckets,
)
from ownvitals.db.engine import create_engine as create_app_engine
from ownvitals.db.engine import create_session_factory


def one_ip(tag: "Tag") -> str:
    """The round's single client (RFC 5737): a round never inherits another's reservations."""
    return f"203.0.113.{tag.round + 1}"


def many_ip(tag: "Tag", index: int) -> str:
    """A distinct client per round and index: one IPv6 documentation /64 each (RFC 3849;
    buckets are per /64)."""
    return f"2001:db8:{tag.round:x}:{index:x}::1"


def reset_buckets(email: str, ip: str) -> list[Bucket]:
    """The buckets ``reset_routes.request_reset`` reserves, in its order."""
    address = bucket_ip(ip)
    return [
        Bucket(MAIL_PAIR, f"{email}|{address}"),
        Bucket(MAIL_ACCOUNT, email),
        Bucket(MAIL_IP, address),
    ]


@dataclass(frozen=True)
class Tag:
    """One round's names: a fresh ``salt`` for the accounts and the round for the IPs."""

    salt: str
    round: int


@dataclass(frozen=True)
class Scenario:
    """``attempts`` concurrent attempts; attempt ``i`` reserves ``buckets(i)``. ``binding`` is
    the bucket that trips, with its limit pinned as ``max_count`` (ADR 0003 section 9)."""

    name: str
    attempts: Callable[[int], int]  # from the number of callers
    buckets: Callable[[int, Tag], list[Bucket]]  # (attempt, the round's tag)
    binding: Callable[[Tag], tuple[Limit, str]]
    max_count: int


def _email(tag: Tag, index: int | None = None) -> str:
    return f"load-{tag.salt}-{tag.round}-{'one' if index is None else index}@example.test"


def _user(tag: Tag, index: int | None = None) -> str:
    return f"user-{tag.salt}-{tag.round}-{'one' if index is None else index}"


SCENARIOS = [
    Scenario(
        "sign-in: one account, many IPs",
        lambda callers: 2 * callers,  # two per IP: the pair and IP buckets never trip
        lambda i, tag: password_buckets(_email(tag), many_ip(tag, i // 2), known_device=False),
        lambda tag: (SIGN_IN_ACCOUNT, _email(tag)),
        50,
    ),
    Scenario(
        "sign-in: one IP, many accounts",
        lambda callers: callers,
        lambda i, tag: password_buckets(_email(tag, i), one_ip(tag), known_device=False),
        lambda tag: (SIGN_IN_IP, bucket_ip(one_ip(tag))),
        30,
    ),
    Scenario(
        "sign-in: one account, one IP",
        # Below the IP's 30, so only the pair's 10 binds (a known device has no account slot).
        lambda callers: min(callers, 29),
        lambda i, tag: password_buckets(_email(tag), one_ip(tag), known_device=True),
        lambda tag: (SIGN_IN_PAIR, f"{_email(tag)}|{bucket_ip(one_ip(tag))}"),
        10,
    ),
    Scenario(
        "reset: one account, many IPs",
        lambda callers: callers,
        lambda i, tag: reset_buckets(_email(tag), many_ip(tag, i)),
        lambda tag: (MAIL_ACCOUNT, _email(tag)),
        10,
    ),
    Scenario(
        "reset: one IP, many accounts",
        lambda callers: callers,
        lambda i, tag: reset_buckets(_email(tag, i), one_ip(tag)),
        lambda tag: (MAIL_IP, bucket_ip(one_ip(tag))),
        10,
    ),
    Scenario(
        "step-up: one account, many IPs",
        lambda callers: callers,
        lambda i, tag: mfa_buckets(_user(tag), many_ip(tag, i)),
        lambda tag: (MFA_ACCOUNT, _user(tag)),
        20,
    ),
    Scenario(
        "step-up: one IP, many accounts",
        lambda callers: callers,
        lambda i, tag: mfa_buckets(_user(tag, i), one_ip(tag)),
        lambda tag: (MFA_IP, bucket_ip(one_ip(tag))),
        20,
    ),
    Scenario(
        "step-up: one account, one IP",
        # Below the account's and the IP's 20, so only the pair's 5 binds.
        lambda callers: min(callers, 19),
        lambda i, tag: mfa_buckets(_user(tag), one_ip(tag)),
        lambda tag: (MFA_PAIR, f"{_user(tag)}|{bucket_ip(one_ip(tag))}"),
        5,
    ),
    Scenario(
        "reset: one account, one IP",
        # Below the account's and the IP's 10, so only the pair's 3 binds.
        lambda callers: min(callers, 9),
        lambda i, tag: reset_buckets(_email(tag), one_ip(tag)),
        lambda tag: (MAIL_PAIR, f"{_email(tag)}|{bucket_ip(one_ip(tag))}"),
        3,
    ),
]


def _windows(database_url: str, bucket_hash: bytes) -> list[int]:
    """The reservation count of every window of one bucket (superuser read)."""
    engine = create_engine(database_url)
    try:
        with engine.connect() as conn:
            return [
                int(count)
                for count in conn.execute(
                    text("SELECT count FROM rate_limits WHERE bucket_hash = :h"),
                    {"h": bucket_hash},
                ).scalars()
            ]
    finally:
        engine.dispose()


async def _round(
    limiter: RateLimiter,
    scenario: Scenario,
    callers: int,
    database_url: str,
    salt: str,
    index: int,
) -> tuple[int, int, list[float]]:
    tag = Tag(salt, index)  # fresh buckets for every round
    attempts = scenario.attempts(callers)
    assert attempts > scenario.max_count  # the limit must be contended to be tested

    async def one(attempt_index: int) -> tuple[bool, float]:
        began = time.monotonic()
        attempt = await limiter.begin(scenario.buckets(attempt_index, tag))
        return attempt.allowed, time.monotonic() - began

    results = await asyncio.gather(*(one(i) for i in range(attempts)))
    allowed = sum(ok for ok, _ in results)
    limit, value = scenario.binding(tag)
    windows = _windows(database_url, limiter.bucket_hash(limit, value))
    # Every attempt reserved exactly one slot of the binding bucket, refused ones included.
    assert sum(windows) == attempts, (scenario.name, windows)
    # The exact bound: per window, the smaller of its reservations and the pinned limit.
    assert allowed == sum(min(count, scenario.max_count) for count in windows), (
        scenario.name,
        allowed,
        windows,
    )
    return allowed, attempts - allowed, [seconds for _, seconds in results]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
@pytest.mark.parametrize("given", profiles())
async def test_the_auth_limits_hold_under_concurrent_attempts(
    given: Profile | None,
    scenario: Scenario,
    env: Env,
    database_url: str,
    request: pytest.FixtureRequest,
) -> None:
    profile = resolve(given)
    limit, _ = scenario.binding(Tag("pin", 0))
    assert limit.max_count == scenario.max_count  # the ADR's number, not a loosened one
    # One pooled app-role connection per caller, so ``workers`` attempts really run at once.
    engine = create_app_engine(env.settings, pool_size=profile.workers, max_overflow=0)
    try:
        limiter = RateLimiter(create_session_factory(engine), env.app.state.keys)
        report = Report(f"auth {scenario.name}", profile.name, profile.workers)
        salt = uuid.uuid4().hex[:12]
        for index in range(profile.rounds):
            allowed, refused, waits = await asyncio.wait_for(
                _round(limiter, scenario, profile.workers, database_url, salt, index),
                profile.deadline,
            )
            report.admitted += allowed
            report.refused += refused
            report.waits += waits
    finally:
        await engine.dispose()
    record(request.config, report)
