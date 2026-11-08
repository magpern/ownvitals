"""The account, session, passkey, QR-login and language settings of ``ownvitals.config`` (fields
and derived properties; the refusing validators stay in ``Settings``)."""

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings


class AuthSettings(BaseSettings):
    """Sessions, proxies, passkeys and QR login. Every refusing validator stays in ``Settings``."""

    # The one public origin, for the Origin check and for links in mail.
    public_base_url: str = "http://127.0.0.1:8000"
    # Comma-separated addresses that become admin at their next sign-in.
    admin_emails: str = ""
    # Comma-separated addresses or CIDR ranges of reverse proxies whose X-Forwarded-For is
    # believed (ADR 0003 section 8): on the home server, SWAG. Default: loopback only. Empty: the
    # peer address is always the client.
    trusted_proxies: str = "127.0.0.0/8,::1"
    # Those of the trusted proxies whose ``CF-Connecting-IP`` header is believed: SWAG, which sets
    # or overwrites it only for connections from Cloudflare's ranges (the owner's SWAG
    # configuration). The header counts only on a request whose peer is in both lists. Default:
    # none, so the header is never believed.
    cf_connecting_ip_proxies: str = ""
    # A session ends after this long without a request, and after ``session_absolute_days`` at
    # most (ADR 0003 section 3: 72 hours idle, 30 days absolute).
    session_idle_hours: int = 72
    session_absolute_days: int = 30
    password_hash_workers: int = 4
    # Who may create an account: ``invite`` (default) or ``closed``. ``open`` is refused until
    # the M8 gate (D7, ADR 0003 section 6).
    signup_mode: Literal["invite", "closed"] = "invite"
    # How long a fresh TOTP check authorises a sensitive action (step-up, PLAN §6: within 10
    # minutes). The environment can shorten the window, never lengthen it.
    stepup_window_minutes: int = Field(default=10, ge=1, le=10)
    # Passkeys (ADR 0011 sections 1 to 3). The WebAuthn relying-party id: unset means the host
    # of ``public_base_url``; set, it must be that host or a parent domain of it. The only
    # accepted origin is ``public_base_url``. An IP-address host has no relying-party id (browsers
    # refuse one), so passkeys are off there; local development uses ``http://localhost:<port>``.
    webauthn_rp_id: str | None = None
    # At most this many passkeys per account (ADR 0011 section 2: 10).
    passkeys_per_account: int = Field(default=10, ge=1, le=10)
    # QR approve-login (ADR 0011 sections 5 to 7). ``qr_login_enabled`` is open question 3's
    # safe-off: false hides the start route and refuses scans. ``geo_source`` is where the
    # desktop's place comes from (open question 2): ``none`` (default, "Place unknown") or
    # ``cloudflare`` (the visitor-location headers, from a ``cf_connecting_ip_proxies`` peer
    # only); ``geoip_file`` is refused until the owner answers and a reviewed PR adds a reader.
    # The SSE streams of the desktop: at most this many per IP (IPv6 by /64) and per web process.
    qr_login_enabled: bool = True
    geo_source: Literal["none", "cloudflare", "geoip_file"] = "none"
    qr_streams_per_ip: int = Field(default=5, ge=1, le=5)
    qr_streams_per_process: int = Field(default=100, ge=1, le=100)
    # How often the worker purges expired auth rows.
    purge_interval_seconds: float = 3600.0
    # The languages the operator enables (ADR 0015 section 1), comma-separated bare codes. ``en``
    # is always on. A language is offered when it is enabled and in the manifest
    # (``locales.SUPPORTED_LOCALES``): this can switch a supported language off, never add one.
    locales: str = "en,sv"

    @property
    def enabled_locales(self) -> frozenset[str]:
        """``locales`` as a set of codes, with ``en`` always in. Anything that is not a
        two-letter lower-case code is dropped (an operator's typo never becomes a code)."""
        codes = {code.strip().lower() for code in self.locales.split(",")}
        return frozenset({c for c in codes if len(c) == 2 and c.isascii() and c.isalpha()} | {"en"})
