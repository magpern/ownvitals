"""The admin area's request-body limit (``ownvitals.admin.routes``): ``AdminBodyLimit`` refuses
an ``/api/admin`` body over ``MAX_BODY_BYTES`` with 413, and ``_body_refusal`` tells the two body
refusals (that one and FastAPI's own unreadable body) from any other HTTP error. The admin
area's handler (``install``) gates and audits them."""

from typing import Any, Final

from fastapi import HTTPException

from ownvitals.admin.routes import ADMIN_PREFIX

# The largest admin request body. The largest valid one is a banner (280 characters, at most 12
# bytes each once JSON-escaped); 16 KiB leaves room without letting a body matter.
MAX_BODY_BYTES: Final = 16 * 1024
BODY_TOO_LARGE: Final = "body_too_large"
BODY_UNREADABLE: Final = "body_unreadable"
# The two body refusals by exactly what raises them, so a 400 or 413 a route raises itself is
# never taken for one: FastAPI's own body-parse failure and ``AdminBodyLimit``'s.
_FASTAPI_BODY_ERROR = "There was an error parsing the body"
_BODY_REFUSALS: dict[tuple[int, object], str] = {
    (400, _FASTAPI_BODY_ERROR): BODY_UNREADABLE,
    (413, repr({"reason": BODY_TOO_LARGE})): BODY_TOO_LARGE,
}


def _body_refusal(exc: object) -> str | None:
    status, detail = getattr(exc, "status_code", None), getattr(exc, "detail", None)
    key = detail if isinstance(detail, str) else repr(detail)
    return _BODY_REFUSALS.get((status, key)) if isinstance(status, int) else None


class AdminBodyLimit:
    """ASGI middleware: an ``/api/admin`` request body over ``MAX_BODY_BYTES``, by its
    ``Content-Length`` or as it streams in, is refused 413 when the route reads it. The refusal
    is raised from ``receive``, so FastAPI's body parsing re-raises it before any dependency,
    and the admin area's handler runs the gate first."""

    def __init__(self, app: Any, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or not str(scope.get("path", "")).startswith(ADMIN_PREFIX):
            await self.app(scope, receive, send)
            return
        declared = next(
            (v for k, v in scope.get("headers", []) if k.lower() == b"content-length"), None
        )
        too_large = declared is not None and (
            not declared.isdigit() or int(declared) > self.max_bytes
        )
        seen = 0

        async def limited() -> Any:
            nonlocal seen
            if too_large:
                raise HTTPException(status_code=413, detail={"reason": BODY_TOO_LARGE})
            message = await receive()
            if message.get("type") == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    raise HTTPException(status_code=413, detail={"reason": BODY_TOO_LARGE})
            return message

        await self.app(scope, limited, send)
