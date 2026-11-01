"""Fixed-window rate limits in Postgres (ADR 0003 section 9). There is no Redis.

Every attempt first takes a slot in each of its buckets with one atomic upsert, each in its own
committed transaction, and is refused above the limit before any password check runs, so
parallel attempts cannot overshoot. Sign-in, re-auth and MFA limits count failures only: the
success releases what it reserved (``Attempt.succeed``) and never touches a newer window.
Bucket keys are an HMAC of ``(action, scope, value, window)``: no email or IP in the clear.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import Float, and_, delete, func, literal, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.auth.clientip import bucket_ip
from ownvitals.auth.keys import Keys, hmac_digest
from ownvitals.auth.tables import rate_limits
from ownvitals.db import system_session


@dataclass(frozen=True, slots=True)
class Limit:
    """``max_count`` slots per fixed window of ``window_seconds``."""

    action: str
    scope: str  # "pair" (account and IP), "account" or "ip"
    max_count: int
    window_seconds: int


# The table of ADR 0003 section 9.
SIGN_IN_PAIR = Limit("sign-in", "pair", 10, 15 * 60)
SIGN_IN_ACCOUNT = Limit("sign-in", "account", 50, 3600)
SIGN_IN_IP = Limit("sign-in", "ip", 30, 15 * 60)
MFA_PAIR = Limit("mfa", "pair", 5, 5 * 60)
MFA_ACCOUNT = Limit("mfa", "account", 20, 3600)
MFA_IP = Limit("mfa", "ip", 20, 5 * 60)
SIGNUP_ADDRESS = Limit("sign-up", "account", 3, 3600)
SIGNUP_IP = Limit("sign-up", "ip", 5, 3600)
MAIL_PAIR = Limit("mail", "pair", 3, 3600)
MAIL_ACCOUNT = Limit("mail", "account", 10, 3600)
MAIL_IP = Limit("mail", "ip", 10, 3600)
OAUTH_IP = Limit("oauth", "ip", 30, 15 * 60)
PASSWORD_CHANGE = Limit("password-change", "account", 10, 3600)
# ADR 0004 section 4: provider connects (MOWL sign-in with a password) count requests, per
# ownvitals user, per IP and per normalized provider email, so the route can't be used to test
# passwords against MOWL. A success releases its slots.
CONNECT_USER = Limit("connect", "account", 5, 15 * 60)
CONNECT_IP = Limit("connect", "ip", 20, 15 * 60)
CONNECT_PROVIDER_EMAIL = Limit("connect", "provider-email", 5, 15 * 60)
# ADR 0009 section 4: Spotify connect starts, per user and per IP and hour. A completed
# connection releases its slot (the callback, ``RateLimiter.release``).
SPOTIFY_START_USER = Limit("spotify-start", "account", 10, 3600)
SPOTIFY_START_IP = Limit("spotify-start", "ip", 30, 3600)
# ADR 0011 section 6: every /api/admin request, per admin and minute.
ADMIN_USER = Limit("admin", "account", 60, 60)
# ADR 0011 section 2: redeem-code attempts, per user and per IP and hour. Every attempt counts,
# a success too, so the route can't be used to try codes faster than this.
REDEEM_USER = Limit("redeem", "account", 10, 3600)
REDEEM_IP = Limit("redeem", "ip", 30, 3600)

OnSuccess = Literal["decrement", "delete"]


@dataclass(frozen=True, slots=True)
class Bucket:
    """One counter an attempt reserves: a limit and the value it is counted for."""

    limit: Limit
    value: str
    on_success: OnSuccess = "decrement"


@dataclass(frozen=True, slots=True)
class Slot:
    bucket_hash: bytes
    window_start: datetime
    on_success: OnSuccess


@dataclass(slots=True)
class Attempt:
    """The result of reserving every bucket of one attempt."""

    allowed: bool
    retry_after: int  # seconds until the longest tripped window ends; 0 when allowed
    slots: list[Slot] = field(default_factory=lambda: list[Slot]())
    _factory: async_sessionmaker[AsyncSession] | None = None

    async def succeed(self) -> None:
        """Release a successful attempt: delete the buckets marked ``delete`` (all their
        windows) and decrement, by one, the exact rows reserved (the same ``window_start``)."""
        if self._factory is None or not self.slots:
            return
        async with system_session(self._factory) as session:
            for slot in self.slots:
                if slot.on_success == "delete":
                    await session.execute(
                        delete(rate_limits).where(rate_limits.c.bucket_hash == slot.bucket_hash)
                    )
                else:
                    await session.execute(
                        update(rate_limits)
                        .where(
                            and_(
                                rate_limits.c.bucket_hash == slot.bucket_hash,
                                rate_limits.c.window_start == slot.window_start,
                            )
                        )
                        .values(count=func.greatest(rate_limits.c.count - 1, 0))
                    )
        self.slots = []


def _window_start(window: int) -> Any:
    return func.to_timestamp(
        func.floor(func.extract("epoch", func.clock_timestamp()) / window) * window
    )


class RateLimiter:
    def __init__(self, factory: async_sessionmaker[AsyncSession], keys: Keys) -> None:
        self._factory = factory
        self._keys = keys

    async def release(self, buckets: Sequence[Bucket]) -> None:
        """Give back one slot of each bucket in its current window (never below zero): a
        success that completes in another request than the one that reserved (the Spotify
        callback releases its start's slot)."""
        async with system_session(self._factory) as session:
            for bucket in buckets:
                bucket_hash = self.bucket_hash(bucket.limit, bucket.value)
                await session.execute(
                    update(rate_limits)
                    .where(
                        and_(
                            rate_limits.c.bucket_hash == bucket_hash,
                            rate_limits.c.window_start
                            == _window_start(bucket.limit.window_seconds),
                        )
                    )
                    .values(count=func.greatest(rate_limits.c.count - 1, 0))
                )

    def bucket_hash(self, limit: Limit, value: str) -> bytes:
        return hmac_digest(
            self._keys.rate_limit, limit.action, limit.scope, str(limit.window_seconds), value
        )

    async def _reserve_one(self, bucket: Bucket) -> tuple[Slot, bool, int]:
        window = bucket.limit.window_seconds
        bucket_hash = self.bucket_hash(bucket.limit, bucket.value)
        started = _window_start(window)
        statement = (
            insert(rate_limits)
            .values(bucket_hash=bucket_hash, window_start=started, count=1)
            .on_conflict_do_update(
                index_elements=[rate_limits.c.bucket_hash, rate_limits.c.window_start],
                set_={"count": rate_limits.c.count + 1},
            )
            .returning(
                rate_limits.c.count.label("hits"),
                rate_limits.c.window_start,
                func.ceil(
                    func.extract(
                        "epoch",
                        rate_limits.c.window_start
                        + func.make_interval(0, 0, 0, 0, 0, 0, literal(float(window), Float))
                        - func.clock_timestamp(),
                    )
                ).label("retry_after"),
            )
        )
        # Its own transaction, committed at once: the next attempt sees this slot taken.
        async with system_session(self._factory) as session:
            row = (await session.execute(statement)).one()
        slot = Slot(bucket_hash, row.window_start, bucket.on_success)
        return slot, row.hits <= bucket.limit.max_count, max(int(row.retry_after), 1)

    async def begin(self, buckets: Sequence[Bucket]) -> Attempt:
        """Reserve one slot in each bucket. Refused if any bucket is over its limit; the slots
        stay taken then (a refused attempt counts), as ADR 0003 section 9 reserves first."""
        attempt = Attempt(allowed=True, retry_after=0, _factory=self._factory)
        for bucket in buckets:
            slot, ok, retry_after = await self._reserve_one(bucket)
            attempt.slots.append(slot)
            if not ok:
                attempt.allowed = False
                attempt.retry_after = max(attempt.retry_after, retry_after)
        return attempt


class RateLimited(Exception):
    """A limit tripped: the response is 429 with ``Retry-After``."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("rate limited")
        self.retry_after = retry_after


def password_buckets(email_normalized: str, ip: str, *, known_device: bool) -> list[Bucket]:
    """Sign-in and re-auth: per (account, IP), per account (not from a known device), per IP."""
    address = bucket_ip(ip)
    buckets = [Bucket(SIGN_IN_PAIR, f"{email_normalized}|{address}", "delete")]
    if not known_device:
        buckets.append(Bucket(SIGN_IN_ACCOUNT, email_normalized))
    buckets.append(Bucket(SIGN_IN_IP, address))
    return buckets


def mfa_buckets(user_id: str, ip: str) -> list[Bucket]:
    """TOTP and recovery code attempts."""
    address = bucket_ip(ip)
    return [
        Bucket(MFA_PAIR, f"{user_id}|{address}", "delete"),
        Bucket(MFA_ACCOUNT, user_id),
        Bucket(MFA_IP, address),
    ]


def connect_buckets(provider: str, user_id: str, ip: str, email_normalized: str) -> list[Bucket]:
    """A provider connect with email and password (MOWL): per user, per IP, per provider email.
    The values are HMAC-keyed like every bucket; the email is never stored in the clear."""
    return [
        Bucket(CONNECT_USER, f"{provider}|{user_id}"),
        Bucket(CONNECT_IP, bucket_ip(ip)),
        Bucket(CONNECT_PROVIDER_EMAIL, f"{provider}|{email_normalized}"),
    ]


def spotify_start_buckets(user_id: str, ip: str) -> list[Bucket]:
    """A Spotify connect start (ADR 0009 section 4): per user and per IP."""
    return [Bucket(SPOTIFY_START_USER, user_id), Bucket(SPOTIFY_START_IP, bucket_ip(ip))]


def admin_buckets(user_id: str) -> list[Bucket]:
    """An admin area request (ADR 0011 section 6): 60 per admin and minute."""
    return [Bucket(ADMIN_USER, user_id)]


def redeem_buckets(user_id: str, ip: str) -> list[Bucket]:
    """A redeem-code attempt (ADR 0011 section 2): per user and per IP."""
    return [Bucket(REDEEM_USER, user_id), Bucket(REDEEM_IP, bucket_ip(ip))]
