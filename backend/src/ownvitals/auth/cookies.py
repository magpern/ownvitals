"""The auth cookies and their attributes (ADR 0003 sections 3 and 4).

Every cookie is a ``__Host-`` cookie: ``Secure``, ``Path=/`` and no ``Domain``. There is no
setting to drop ``Secure``: Chromium and Firefox accept it on ``http://127.0.0.1``.
"""

from starlette.responses import Response

from ownvitals.auth.sessions import IssuedSession

SESSION_COOKIE = "__Host-ownvitals_session"
DEVICE_COOKIE = "__Host-ownvitals_device"
CSRF_COOKIE = "__Host-ownvitals_csrf"
OAUTH_COOKIE = "__Host-ownvitals_oauth"
PENDING_COOKIE = "__Host-ownvitals_pending"

DEVICE_MAX_AGE = 365 * 24 * 3600
CSRF_MAX_AGE = 30 * 24 * 3600
OAUTH_MAX_AGE = 10 * 60
PENDING_MAX_AGE = 24 * 3600


def set_session_cookie(response: Response, issued: IssuedSession) -> None:
    """``HttpOnly``, ``Secure``, ``SameSite=Lax``; ``Max-Age`` is the remaining lifetime."""
    response.set_cookie(
        SESSION_COOKIE,
        issued.token,
        max_age=issued.max_age,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax")


def set_device_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        DEVICE_COOKIE,
        token,
        max_age=DEVICE_MAX_AGE,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def set_csrf_cookie(response: Response, value: str) -> None:
    """The anonymous double-submit cookie: readable by JavaScript, never sent cross-site."""
    response.set_cookie(
        CSRF_COOKIE,
        value,
        max_age=CSRF_MAX_AGE,
        path="/",
        secure=True,
        httponly=False,
        samesite="strict",
    )


def set_pending_cookie(response: Response, value: str) -> None:
    response.set_cookie(
        PENDING_COOKIE,
        value,
        max_age=PENDING_MAX_AGE,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def set_oauth_cookie(response: Response, value: str) -> None:
    response.set_cookie(
        OAUTH_COOKIE,
        value,
        max_age=OAUTH_MAX_AGE,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def clear_oauth_cookie(response: Response) -> None:
    response.delete_cookie(OAUTH_COOKIE, path="/", secure=True, httponly=True, samesite="lax")


def clear_pending_cookie(response: Response) -> None:
    response.delete_cookie(PENDING_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
