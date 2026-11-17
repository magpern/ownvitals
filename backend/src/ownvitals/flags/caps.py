"""The AI monthly caps as stored flags (#528; ADR 0014 section 7 and its amendment of 2026-11-17).

Pure functions: no database, no environment. ``ownvitals.flags.effective.ai_caps`` reads the two
stored rows; ``ownvitals.ai.budget`` passes the result to every ``ai_reserve``.

The environment's ``OWNVITALS_AI_USER_MONTHLY_CAP_MICROUSD`` and
``OWNVITALS_AI_GLOBAL_MONTHLY_CAP_MICROUSD`` are the ceiling and the default. A stored cap can only
lower its own, never raise it, so the server settings stay the most any month can cost:

- effective global = the environment's global cap, or the lower of it and the stored one;
- effective per user = the environment's per-user cap, or the lower of it and the stored one, and
  never above the effective global cap. So the per-user cap is at most the global one however the
  two rows were written (two admins at once, a cleared per-user cap under a lowered global one).
- An environment cap of 0 means no request can be reserved; a stored cap can only be 0 then.
- A stored value that is not an integer from 0 to ``MAX_CAP_MICROUSD`` counts as **0** (the lower
  bound: nothing is reserved) and is logged by key only.

The write side (the admin area) refuses instead of falling back: a stored cap above the
environment's (``above_environment``), a per-user cap above the effective global cap, or a global
cap below the effective per-user cap (``user_above_global``).
"""

from dataclasses import dataclass
from typing import Any

from ownvitals.flags import core
from ownvitals.flags.core import GLOBAL_CAP, USER_CAP, FlagRefused


@dataclass(frozen=True, slots=True)
class Caps:
    """The caps in force, in micro-USD."""

    user: int
    global_: int


def _environment(value: int, name: str) -> int:
    if type(value) is not int or not 0 <= value <= core.MAX_CAP_MICROUSD:
        raise ValueError(f"the environment's {name} cap is an integer in range")
    return value


def stored_cap(key: str, stored: Any) -> int | None:
    """The stored cap of ``key`` (``None``: no row or a cleared one). Invalid: 0, logged by key."""
    if key not in core.CAP_KEYS:
        raise ValueError(f"{key!r} is not a cap flag")
    if stored is None:
        return None
    if type(stored) is not int or not 0 <= stored <= core.MAX_CAP_MICROUSD:
        core.invalid(key)
        return 0
    return stored


def effective_caps(env_user: int, env_global: int, stored_user: Any, stored_global: Any) -> Caps:
    """The caps in force: each the lower of the environment's and the stored one, and the per-user
    cap never above the global one. ``stored_*`` are the raw stored values (``None``: none)."""
    env_user = _environment(env_user, "per-user")
    env_global = _environment(env_global, "global")
    user, global_ = stored_cap(USER_CAP, stored_user), stored_cap(GLOBAL_CAP, stored_global)
    effective_global = env_global if global_ is None else min(env_global, global_)
    effective_user = env_user if user is None else min(env_user, user)
    return Caps(user=min(effective_user, effective_global), global_=effective_global)


def check_environment(key: str, value: int, env_user: int, env_global: int) -> None:
    """``FlagRefused("above_environment")`` when ``value`` (validated) is above the
    environment's cap of ``key``."""
    ceiling = _environment(env_user if key == USER_CAP else env_global, key)
    if value > ceiling:
        raise FlagRefused("above_environment")


def check_against_other(
    key: str, value: int, env_user: int, env_global: int, stored_user: Any, stored_global: Any
) -> None:
    """``FlagRefused("user_above_global")`` when storing ``value`` for ``key`` would leave the
    per-user cap above the global cap (effective values, the other cap as stored now)."""
    if key == USER_CAP:
        caps = effective_caps(env_user, env_global, None, stored_global)
        if min(value, env_user) > caps.global_:
            raise FlagRefused("user_above_global")
    else:
        current = effective_caps(env_user, env_global, stored_user, None)
        if current.user > min(value, env_global):
            raise FlagRefused("user_above_global")
