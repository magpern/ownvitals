"""The Cloudflare Turnstile settings of ``ownvitals.config`` (roadmap Amendment 12, ADR 0018).

Off by default. On, it needs both keys, or the settings refuse to load and so no process starts
(the install check): the public site key (``OWNVITALS_TURNSTILE_SITE_KEY``) and the secret, which
is read only from the secret file ``turnstile_secret_key`` (``config_secrets``), never from the
environment. The refusal names the missing field, never a value.
"""

import re

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings

# The site key goes to the browser and into the widget: a short token of letters, digits, ``_``
# and ``-`` (Cloudflare's keys look like ``0x4AAAAAAA...``), nothing else.
SITE_KEY = re.compile(r"[A-Za-z0-9_-]{1,128}")
# The secret as ``egress.turnstile`` sends it: printable ASCII, no space, ``;``, ``&``, ``=`` or
# ``#``, at most 256 characters. Another one would fail every check, so it is refused here.
SECRET = re.compile(r"[!-~]{1,256}")
SECRET_FORBIDDEN = frozenset(";&=#")


class TurnstileSettings(BaseSettings):
    """Turnstile on sign-up, sign-in and the reset request; its validators refuse a half setup."""

    # The owner's decision (roadmap Amendment 7, P1): never on without the owner.
    turnstile_enabled: bool = False
    turnstile_site_key: str | None = None
    # From the file ``turnstile_secret_key`` (never the environment).
    turnstile_secret_key: SecretStr | None = None

    @field_validator("turnstile_site_key")
    @classmethod
    def _check_site_key(cls, value: str | None) -> str | None:
        if value is not None and not SITE_KEY.fullmatch(value):
            raise ValueError("turnstile_site_key is letters, digits, '_' and '-' (at most 128)")
        return value

    @model_validator(mode="after")
    def _turnstile_needs_both_keys(self) -> "TurnstileSettings":
        if not self.turnstile_enabled:
            return self
        if not self.turnstile_site_key:
            raise ValueError("turnstile_enabled needs turnstile_site_key")
        secret = self.turnstile_secret_key
        if secret is None or not secret.get_secret_value():
            raise ValueError("turnstile_enabled needs the secret file turnstile_secret_key")
        value = secret.get_secret_value()
        if not SECRET.fullmatch(value) or SECRET_FORBIDDEN & set(value):
            raise ValueError("the secret file turnstile_secret_key has an unusable value")
        return self
