"""The jobs API's authentication, from ``ownvitals.auth`` (#10).

``POST /api/jobs``, ``GET /api/jobs/{id}``, its cancel and its event stream need a signed-in
session (ADR 0003 section 2); the writes pass the Origin and CSRF checks of ``ownvitals.auth``'s
middleware, which no route can skip. #11 narrows "signed in" to a full session (second factor
passed) in ``ownvitals.auth.deps``, so nothing here changes then.

- ``current_user`` is ``ownvitals.auth``'s dependency, the only web issuer of a ``UserContext``
  (ADR 0002 section 1): 401 when anonymous.
- ``stream_session_probe`` gives the event stream a check that the session it was opened with is
  still valid for the same user (every 15 s): the request's session cookie, resolved again. A
  revoked, expired or rotated-away session, or one of another user, ends the stream.

Tests use ``app.dependency_overrides`` with a test-only issuer (``tests/ctx.py``). No context is
built here.
"""

from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request

from ownvitals.auth.deps import Probe, require_user, session_probe
from ownvitals.context import UserContext


async def current_user(ctx: Annotated[UserContext, Depends(require_user)]) -> UserContext:
    """The signed-in user's context; 401 ``not_authenticated`` when anonymous."""
    return ctx


def stream_session_probe(request: Request) -> Callable[[UUID], Probe]:
    """For a stream of ``user_id``: whether the session cookie the stream was opened with still
    resolves to a valid session of that user (``ownvitals.auth.deps.session_probe``)."""
    return session_probe(request)
