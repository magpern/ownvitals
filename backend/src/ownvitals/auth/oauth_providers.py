"""The three OAuth providers and which of them are switched on (ADR 0003 section 5).

A provider is on only when both its client id setting and its ``oauth_<provider>_client_secret``
secret file exist. The endpoints are the real ones; tests put an in-process fake behind them with
an httpx ``MockTransport`` (``OAuthRuntime.transport``), so nothing here ever needs the network.
"""

from dataclasses import dataclass, field

from pydantic import SecretStr

from ownvitals.config import Settings

GOOGLE, GITHUB, MICROSOFT = "google", "github", "microsoft"
MICROSOFT_ISSUER = "https://login.microsoftonline.com/{tid}/v2.0"


@dataclass(frozen=True, slots=True)
class Provider:
    name: str
    client_id: str
    authorize_url: str
    token_url: str
    scope: str
    jwks_url: str | None  # None: not OIDC (GitHub)
    client_secret: str = field(repr=False, default="")
    issuers: tuple[str, ...] = ()  # empty: Microsoft, whose issuer follows the token's tid

    @property
    def oidc(self) -> bool:
        return self.jwks_url is not None


def _secret(value: SecretStr | None) -> str | None:
    return value.get_secret_value() if value is not None else None


def enabled_providers(settings: Settings) -> dict[str, Provider]:
    """Providers whose client id and client secret are both set."""
    found: dict[str, Provider] = {}
    google_secret = _secret(settings.oauth_google_client_secret)
    if settings.oauth_google_client_id and google_secret:
        found[GOOGLE] = Provider(
            GOOGLE,
            settings.oauth_google_client_id,
            "https://accounts.google.com/o/oauth2/v2/auth",
            "https://oauth2.googleapis.com/token",
            "openid email",
            "https://www.googleapis.com/oauth2/v3/certs",
            google_secret,
            ("https://accounts.google.com", "accounts.google.com"),
        )
    microsoft_secret = _secret(settings.oauth_microsoft_client_secret)
    if settings.oauth_microsoft_client_id and microsoft_secret:
        found[MICROSOFT] = Provider(
            MICROSOFT,
            settings.oauth_microsoft_client_id,
            "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
            "https://login.microsoftonline.com/common/oauth2/v2.0/token",
            "openid profile",
            "https://login.microsoftonline.com/common/discovery/v2.0/keys",
            microsoft_secret,
        )
    github_secret = _secret(settings.oauth_github_client_secret)
    if settings.oauth_github_client_id and github_secret:
        found[GITHUB] = Provider(
            GITHUB,
            settings.oauth_github_client_id,
            "https://github.com/login/oauth/authorize",
            "https://github.com/login/oauth/access_token",
            "read:user user:email",
            None,
            github_secret,
        )
    return found
