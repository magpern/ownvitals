"""The flag keys, their validation and the stricter-wins rule (ADR 0003 section 6; classbeat ADR
0011 section 7; ownvitals #12).

Pure functions: no database, no environment. ``ownvitals.flags.effective`` reads the stored rows
(``app_settings``) and joins them with the environment.

The database can tighten what the environment allows, never loosen it:

- ``signup_mode``: stored ``invite`` or ``closed``; the effective mode is the stricter of the
  environment's and the stored one (``closed`` above ``invite``). ``open`` is refused until the
  M8 gate: the setting refuses it, and a stored ``open`` is invalid.
- ``experimental_findings`` (D9, owner-only) and ``ai_insights`` (D11): stored ``off`` or ``on``.
  Each is on only when the environment's switch (``OWNVITALS_EXPERIMENTAL_FINDINGS``,
  ``OWNVITALS_AI_INSIGHTS``, both off by default) is on **and** the stored value is not ``off``;
  no row means the environment's value. A stored ``on`` never turns on what the environment has
  off. Who may see experimental findings (the owner's household accounts only, D9) is decided
  where they are shown; this flag is the switch above that rule.
- ``banner``: ``{text, level}``; absent or invalid is none. Its text is plain text: no control
  character (``Cc``), no format character (``Cf``: bidi controls, zero-width characters, the
  soft hyphen, the BOM) and no line or paragraph separator (``Zl``, ``Zp``), on the write side
  and again when it is read.

A stored value that does not validate falls back to the key's stricter default (``invite``,
``off``, no banner) and is logged by key only: the value is never logged, since it may hold an
admin's free text. Unknown keys are refused on write (``unknown_key``); the table's own check
refuses them too.
"""

import logging
import unicodedata
from dataclasses import dataclass
from typing import Any, Final, Literal

logger = logging.getLogger(__name__)

SignupMode = Literal["invite", "closed"]
Switch = Literal["off", "on"]
BannerLevel = Literal["info", "warning"]

BANNER_MAX_CHARS: Final = 280
KEYS: Final = ("signup_mode", "experimental_findings", "ai_insights", "banner")
SWITCH_KEYS: Final = ("experimental_findings", "ai_insights")

_STRICTNESS: Final[dict[str, int]] = {"invite": 1, "closed": 2}
_SWITCHES: Final = ("off", "on")
_LEVELS: Final = ("info", "warning")
_NOT_PLAIN_TEXT: Final = frozenset({"Cc", "Cf", "Zl", "Zp"})


def _plain_text(text: str) -> bool:
    return not any(unicodedata.category(c) in _NOT_PLAIN_TEXT for c in text)


def _invalid(key: str) -> None:
    logger.warning("flag %s holds an invalid value; its stricter default applies", key)


@dataclass(frozen=True, slots=True)
class Banner:
    text: str
    level: BannerLevel


def stricter_signup(environment: SignupMode, stored: Any) -> SignupMode:
    """``stored`` is the stored value, or ``None`` when there is none."""
    if environment not in _STRICTNESS:
        raise ValueError("the environment's sign-up mode is invite or closed")
    if stored is None:
        return environment
    if not isinstance(stored, str) or stored not in _STRICTNESS:
        _invalid("signup_mode")
        stored = "invite"
    return "closed" if "closed" in (stored, environment) else "invite"


def stricter_switch(key: str, environment_on: bool, stored: Any) -> bool:
    """``experimental_findings`` or ``ai_insights``: on only when the environment has it on and
    the stored value (``None``: no row) is not ``off``. An invalid stored value counts as
    ``off``."""
    if key not in SWITCH_KEYS:
        raise ValueError(f"{key!r} is not a switch flag")
    if type(environment_on) is not bool:
        raise ValueError("the environment's switch is a bool")
    if stored is None:
        return environment_on
    if not isinstance(stored, str) or stored not in _SWITCHES:
        _invalid(key)
        return False
    return environment_on and stored == "on"


def banner_setting(stored: Any) -> Banner | None:
    """The stored ``{text, level}``; ``None`` when absent, empty, too long, not plain text or
    malformed."""
    if stored is None:
        return None
    if isinstance(stored, dict):
        text, level = stored.get("text"), stored.get("level")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if (
            isinstance(text, str)
            and level in _LEVELS
            and len(text) <= BANNER_MAX_CHARS
            and _plain_text(text)
            and len(stored) == 2  # pyright: ignore[reportUnknownArgumentType]
        ):
            return Banner(text, level) if text.strip() else None  # pyright: ignore[reportArgumentType]
    _invalid("banner")
    return None


# --- validation at write time (the admin area) -------------------------------------------------


class FlagRefused(Exception):
    """A value the admin may not store: ``reason`` is the audit and response code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def validate_for_write(key: str, value: Any) -> Any:
    """The canonical value to store for ``key``, or ``FlagRefused``. Stricter than the read side:
    the read side falls back, the write side refuses.

    - ``signup_mode``: ``invite`` or ``closed``; ``open`` is refused until M8
      (``environment_only``).
    - ``experimental_findings``, ``ai_insights``: ``off`` or ``on``.
    - ``banner``: exactly ``{text, level}``; plain text of 1 to 280 characters, level ``info`` or
      ``warning``."""
    if key == "signup_mode":
        if value == "open":
            raise FlagRefused("environment_only")
        if not isinstance(value, str) or value not in _STRICTNESS:
            raise FlagRefused("invalid_value")
        return value
    if key in SWITCH_KEYS:
        if not isinstance(value, str) or value not in _SWITCHES:
            raise FlagRefused("invalid_value")
        return value
    if key == "banner":
        if not isinstance(value, dict) or set(value) != {"text", "level"}:  # pyright: ignore[reportUnknownArgumentType]
            raise FlagRefused("invalid_value")
        text, level = value["text"], value["level"]
        if not isinstance(text, str) or level not in _LEVELS or not text.strip():
            raise FlagRefused("invalid_value")
        if len(text) > BANNER_MAX_CHARS or not _plain_text(text):
            raise FlagRefused("invalid_value")
        return {"text": text, "level": level}
    raise FlagRefused("unknown_key")
