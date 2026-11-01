"""The one OAuth provider, Google, and whether it is switched on (ADR 0003 sections 1 and 6).

Google is on only when both its client id setting and the ``oauth_google_client_secret`` secret
file exist. GitHub and Microsoft are not ported (PLAN §3). The endpoints are the real ones; the
code exchange and the JWKS fetch go through ``ownvitals.egress``'s Google sign-in clients, and
tests put an in-process fake behind them (``OAuthRuntime.transport``, the clients' ``inner``), so
nothing here ever needs the network.
"""

from dataclasses import dataclass, field

from pydantic import SecretStr

from ownvitals.config import Settings

GOOGLE = "google"
GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - an endpoint
GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
GOOGLE_SCOPE = "openid email"


@dataclass(frozen=True, slots=True)
class Provider:
    name: str
    client_id: str
    authorize_url: str
    token_url: str
    scope: str
    jwks_url: str
    client_secret: str = field(repr=False, default="")
    issuers: tuple[str, ...] = ()


def _secret(value: SecretStr | None) -> str | None:
    return value.get_secret_value() if value is not None else None


def google(client_id: str, client_secret: str) -> Provider:
    return Provider(
        GOOGLE,
        client_id,
        GOOGLE_AUTHORIZE_URL,
        GOOGLE_TOKEN_URL,
        GOOGLE_SCOPE,
        GOOGLE_JWKS_URL,
        client_secret,
        GOOGLE_ISSUERS,
    )


def enabled_providers(settings: Settings) -> dict[str, Provider]:
    """``{"google": ...}`` when its client id and client secret are both set, else empty."""
    secret = _secret(settings.oauth_google_client_secret)
    if settings.oauth_google_client_id and secret:
        return {GOOGLE: google(settings.oauth_google_client_id, secret)}
    return {}
