"""The jobs API's authentication seams, fail-closed until the accounts issue (#10).

``POST /api/jobs``, ``GET /api/jobs/{id}``, its cancel and its event stream need a **full
session** (ADR 0003 section 2: signed in, second factor passed) and, for the writes, the CSRF
check. Neither exists before #10, so (Architect decision, 2026-10-31, #77):

- ``current_user`` always answers 401: no route of the jobs API serves anyone yet. #10 replaces
  it with ``ownvitals.auth``'s dependency (the only web issuer of a ``UserContext``, ADR 0002
  section 1), with CSRF on the writes;
- ``stream_session_probe`` gives the event stream a check that the session it was opened with is
  still valid for the same user (every 15 s); the default says no, so a stream ends at its first
  check. #10 replaces it with a check of the session cookie.

Tests use ``app.dependency_overrides`` with a test-only issuer (``tests/ctx.py``). No context is
built here.
"""

from collections.abc import Awaitable, Callable
from uuid import UUID

from fastapi import HTTPException, Request

from ownvitals.context import UserContext

Probe = Callable[[], Awaitable[bool]]


async def current_user() -> UserContext:
    """Fail closed until #10: nobody is signed in."""
    raise HTTPException(status_code=401, detail={"reason": "not_signed_in"})


def stream_session_probe(request: Request) -> Callable[[UUID], Probe]:
    """For a stream of ``user_id``: whether its session is still valid. Fail closed until #10."""

    def probe_for(user_id: UUID) -> Probe:
        async def still_signed_in() -> bool:
            return False

        return still_signed_in

    return probe_for
