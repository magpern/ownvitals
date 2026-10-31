"""``UserContext``: who is acting, carried explicitly (ADR 0002 section 1).

Only ``ownvitals.auth`` (from the authenticated server-side session, after the second factor) and
``ownvitals.jobs`` (from the job row a worker claimed) may build one. Two layers say so:

- a runtime barrier: construction needs the issuer token (``issuer_token()``), so
  ``UserContext(...)``, ``type(ctx)(...)``, ``ctx.__class__(...)`` and ``dataclasses.replace``
  without it raise, re-calling ``ctx.__init__`` on an existing context raises before it changes
  anything, contexts cannot be pickled, and ``require_context`` accepts only the exact class;
- an AST scan (``tests/test_user_context_guard.py``) that refuses any reference to the class,
  the token, ``dataclasses.replace``, ``object.__setattr__`` and similar outside the issuers.

Both stop accidental misuse and make misuse visible in review. Deliberately malicious code
inside our own process (``object.__setattr__`` on a frozen instance, ``object.__new__`` plus
slot descriptors, ctypes) is out of scope for runtime checks; the review gate catches it.
Everything else receives the context as the ``ctx`` parameter.
"""

from dataclasses import dataclass
from typing import Any, Literal, NoReturn
from uuid import UUID

ROLES = ("user", "admin")
_ISSUER = object()  # module-private; handed out only by issuer_token()


def issuer_token() -> object:
    """The token that lets the caller build a ``UserContext``.

    Only the issuers in ``ownvitals.auth`` and ``ownvitals.jobs`` may call it (the AST scan
    refuses any other module under ``src/ownvitals``); tests use ``tests/ctx.py``.
    """
    return _ISSUER


@dataclass(frozen=True, slots=True, init=False)
class UserContext:
    """Immutable and hashable. Request- or job-scoped; never stored globally.

    ``connections`` holds ``(provider, connection row id)`` pairs: references to the user's
    connection rows, never credentials. Use ``connection(provider)`` to look one up.
    """

    user_id: UUID
    role: Literal["user", "admin"]
    connections: frozenset[tuple[str, UUID]]

    def __init__(
        self,
        user_id: UUID,
        role: Literal["user", "admin"],
        connections: frozenset[tuple[str, UUID]] = frozenset(),
        *,
        issuer: object = None,
    ) -> None:
        # Both checks come before any field is assigned, so a refused call (including a
        # repeated ``ctx.__init__(...)``) changes nothing. ``dataclasses.replace`` does not
        # pass the token, so it is refused too.
        if issuer is not _ISSUER:
            raise PermissionError("a UserContext can only be issued by ownvitals.auth or jobs")
        try:
            object.__getattribute__(self, "user_id")
        except AttributeError:
            pass  # a fresh instance: its slots are still empty
        else:
            raise PermissionError(
                "a UserContext is already initialised and cannot be re-initialised"
            )
        if not isinstance(user_id, UUID):
            raise TypeError("user_id must be a UUID")
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        if not isinstance(connections, frozenset):
            raise TypeError("connections must be a frozenset of (provider, connection id)")
        providers = [pair[0] for pair in connections]
        if len(providers) != len(set(providers)):
            raise ValueError("connections has more than one connection for a provider")
        object.__setattr__(self, "user_id", user_id)
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "connections", connections)

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> NoReturn:
        # Pydantic (and so FastAPI) would otherwise build a real UserContext from request data
        # without calling __init__, so a handler parameter ``ctx: UserContext`` would take the
        # user from the body. Any model, TypeAdapter or route that tries fails right here.
        raise TypeError("UserContext can't be validated from data; use the auth dependency")

    def __reduce__(self) -> NoReturn:
        raise TypeError("a UserContext cannot be pickled or copied by value")

    def __setstate__(self, state: object) -> NoReturn:
        raise TypeError("a UserContext cannot be restored from a state")

    def connection(self, provider: str) -> UUID | None:
        """The connection row id for ``provider``, or ``None``."""
        for name, connection_id in self.connections:
            if name == provider:
                return connection_id
        return None


def require_context(ctx: object) -> UserContext:
    """Return ``ctx`` if it is exactly a ``UserContext``; anything else is refused.

    No look-alike with a ``user_id`` attribute, no subclass instance, no UUID, no dict.
    """
    if type(ctx) is not UserContext:
        raise TypeError("a UserContext is required")
    return ctx
