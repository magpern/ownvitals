"""The sign-up mode with the stricter-wins rule (ADR 0003 section 6; classbeat ADR 0011 §7).

Pure functions: no database, no environment. ``ownvitals.flags.effective`` joins the environment
with the stored flag. #10 ports the sign-up mode only; the stored flags and the admin writes
arrive with #12.

The database can tighten what the environment allows, never loosen it: the effective mode is the
stricter of ``OWNVITALS_SIGNUP_MODE`` and the stored ``signup_mode`` (``closed`` above
``invite``). ``open`` is refused until the M8 gate: the setting refuses it, and a stored ``open``
is invalid. A stored value that does not validate falls back to ``invite``, and is logged by key
only.
"""

import logging
from typing import Any, Final, Literal

logger = logging.getLogger(__name__)

SignupMode = Literal["invite", "closed"]

_STRICTNESS: Final[dict[str, int]] = {"invite": 1, "closed": 2}


def stricter_signup(environment: SignupMode, stored: Any) -> SignupMode:
    """``stored`` is the stored value, or ``None`` when there is none."""
    if environment not in _STRICTNESS:
        raise ValueError("the environment's sign-up mode is invite or closed")
    if stored is None:
        return environment
    if not isinstance(stored, str) or stored not in _STRICTNESS:
        logger.warning("flag signup_mode holds an invalid value; its stricter default applies")
        stored = "invite"
    return "closed" if "closed" in (stored, environment) else "invite"
