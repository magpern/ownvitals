"""The mail and Google sign-in settings of ``ownvitals.config`` (fields only)."""

from typing import Literal

from pydantic_settings import BaseSettings

DEFAULT_SMTP_TOTAL_SECONDS = 20.0


class MailSettings(BaseSettings):
    """Mail and OAuth fields. Their validators and the secret fields stay in ``Settings``."""

    # Mail. Unset ``smtp_host``: no mail is sent (a warning is logged instead).
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_sender: str = "ownvitals@localhost"
    smtp_tls: Literal["starttls", "tls", "none"] = "starttls"
    smtp_username: str | None = None
    # The total deadline of one delivery, every SMTP step included (mail runs inside web
    # requests; ``ownvitals.mail``). Positive and finite.
    smtp_total_seconds: float = DEFAULT_SMTP_TOTAL_SECONDS

    # Google sign-in, the only OAuth provider. It is on only when the client id and the secret
    # file ``oauth_google_client_secret`` (never the environment) both exist.
    oauth_google_client_id: str | None = None
