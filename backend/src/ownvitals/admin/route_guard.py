"""The admin route guard's helpers (moved out of ``ownvitals.admin.routes`` unchanged, #461).

``tests/test_admin_routes_guard.py`` enumerates the app's routes with ``admin_area_routes`` and
fails for one under ``/api/admin`` that ``gate_problems`` names: a route without ``admin_gate``
(``whoami`` of ``ownvitals.auth`` needs ``require_admin``), or one that is not a plain API route.
"""

from typing import Any

from fastapi.routing import APIRoute, iter_route_contexts

from ownvitals.admin.routes import ADMIN_PREFIX, admin_gate, calls


def admin_area_routes(routes: list[Any]) -> list[tuple[str, Any]]:
    """Every route of ``routes`` under ``/api/admin`` with its full path, through included
    routers (FastAPI keeps them as nested entries of ``app.routes``)."""
    found: list[tuple[str, Any]] = []
    for context in iter_route_contexts(routes):
        path = context.path or getattr(context.original_route, "path", "") or ""
        if path.startswith(ADMIN_PREFIX):
            found.append((path, context.original_route))
    return found


def gate_problems(routes: list[Any], whoami: Any) -> list[str]:
    """For the router guard: routes under ``/api/admin`` without ``admin_gate`` (or, for the
    auth package's ``whoami``, without ``whoami``'s dependency, passed in as ``whoami``), or that
    are not plain API routes (a mount or a websocket would bypass the check)."""
    problems: list[str] = []
    for path, route in admin_area_routes(routes):
        if not isinstance(route, APIRoute):
            problems.append(f"{path}: not an API route ({type(route).__name__})")
            continue
        is_whoami = path == f"{ADMIN_PREFIX}/whoami"
        needed = whoami if is_whoami else admin_gate
        if needed not in calls(route.dependant):
            name = "require_admin" if is_whoami else "admin_gate"
            problems.append(f"{sorted(route.methods or set())} {path}: no {name}")
    return problems
