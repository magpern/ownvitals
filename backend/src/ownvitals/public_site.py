"""Serving the prerendered public site and keeping everything else out of search (ADR 0016).

The pure part is the fixed set of public paths and the exact-match rule. The side effects are
the contained file lookup and the ``X-Robots-Tag`` / cache middleware.

- ``public_relative_file`` maps a request path to a file name under the public root, or ``None``
  for any path not in the fixed set (no prefix, no glob, no trailing-slash variant). An unlisted
  or traversal path never builds a filesystem path.
- ``find_public_file`` resolves the candidate and requires ``is_relative_to(root)`` and
  ``is_file()`` after resolution (symlinks included); any failure is ``None``.
- A response is exempt from ``noindex`` only when a prerendered file was actually served, so a
  listed path that falls through to the SPA shell (a legal page omitted while the legal flag is
  off) still gets ``noindex``. Only that response carries ``Cache-Control: public``.
"""

from pathlib import Path, PurePosixPath
from typing import Final

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

PAGE_ROUTES: Final = ("", "features", "security", "privacy", "terms", "cookies", "contact")
# Exact files of the public build, by their path from its root. The image names are the six
# mock-ups of frontend/src/public/images.ts (ADR 0016 item 4), listed one by one, never as a
# prefix or glob; a frontend test holds this tuple equal to what the build emits.
PUBLIC_FILES: Final = (
    "sitemap.xml",
    "robots.txt",
    "public.css",
    "img/Dashboard.png",
    "img/Dashboard.dark.png",
    "img/Findings.png",
    "img/Findings.dark.png",
    "img/Connections.png",
    "img/Connections.dark.png",
)

_PAGE_PATHS: Final = frozenset(
    "/".join(part for part in (locale, route) if part)
    for locale in ("", "sv")
    for route in PAGE_ROUTES
)
PUBLIC_PATHS: Final = _PAGE_PATHS | frozenset(PUBLIC_FILES)

NOINDEX: Final = "noindex, nofollow"
PUBLIC_CACHE_CONTROL: Final = "public, max-age=300"
SERVED_KEY: Final = "ownvitals.public_file_served"


def public_relative_file(path: str) -> PurePosixPath | None:
    """The file under the public root for the exact request path ``path`` (no leading slash)."""
    if path not in PUBLIC_PATHS:
        return None
    return PurePosixPath(path, "index.html") if path in _PAGE_PATHS else PurePosixPath(path)


def find_public_file(root: Path, path: str) -> Path | None:
    """The prerendered file for ``path``, or ``None``. ``root`` is already resolved."""
    relative = public_relative_file(path)
    if relative is None:
        return None
    try:
        candidate = root.joinpath(*relative.parts).resolve()
        if candidate.is_relative_to(root) and candidate.is_file():
            return candidate
    except (OSError, ValueError):  # for example a symlink loop
        pass
    return None


def mark_public_served(scope: Scope) -> None:
    scope[SERVED_KEY] = True


class RobotsHeaders:
    """``X-Robots-Tag: noindex, nofollow`` on every response that is not a public file."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def with_robots(message: Message) -> None:
            if message["type"] == "http.response.start" and not scope.get(SERVED_KEY):
                MutableHeaders(scope=message)["X-Robots-Tag"] = NOINDEX
            await send(message)

        await self.app(scope, receive, with_robots)
