"""Small helpers shared by the sign-up, sign-in, reset and email routes (ADR 0003 sections 5, 9)."""

import re
from collections.abc import Sequence
from typing import Annotated

from fastapi import Request
from pydantic import AfterValidator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.auth.ratelimit import Attempt, Bucket, RateLimited, RateLimiter
from ownvitals.mail import Mailer

EMAIL_MAX = 254
_PART = r"[^@\s<>\"',;:()\[\]\\]+"
_EMAIL = re.compile(rf"{_PART}@{_PART}\.{_PART}")


def check_email(value: str) -> str:
    """A plausible single address; anything with whitespace, control characters or a second
    ``@`` is refused (it ends up in a mail header)."""
    value = value.strip()
    if len(value) > EMAIL_MAX or not _EMAIL.fullmatch(value) or not value.isprintable():
        raise ValueError("not a valid email address")
    return value


EmailAddress = Annotated[str, AfterValidator(check_email)]


def factory(request: Request) -> async_sessionmaker[AsyncSession]:
    return request.app.state.session_factory


def mailer(request: Request) -> Mailer:
    return request.app.state.mailer


def limiter(request: Request) -> RateLimiter:
    return request.app.state.limiter


def user_agent(request: Request) -> str | None:
    return request.headers.get("user-agent")


async def reserve(request: Request, buckets: Sequence[Bucket]) -> Attempt:
    """Reserve the slots; raise ``RateLimited`` (429 with Retry-After) above a limit."""
    attempt = await limiter(request).begin(buckets)
    if not attempt.allowed:
        raise RateLimited(attempt.retry_after)
    return attempt
