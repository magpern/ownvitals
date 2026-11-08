"""The mails we send (ADR 0003 section 5), in the recipient's language (ADR 0015 section 7). Plain
text; links come from ``OWNVITALS_PUBLIC_BASE_URL`` with the token in the URL fragment, never from
the Host header.

Each mail takes ``locale``, an allowlisted code: it picks the table of ``auth.mail_texts`` and is
the message's ``Content-Language``, and nothing else. The caller decides it
(``auth.recipients``): the account's stored language or the request's for the mails sent inside a
request, ``en`` for everything else (the default).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from ownvitals.auth.mail_texts import table
from ownvitals.config import Settings
from ownvitals.mail import MailMessage
from ownvitals.notify.locale_policy import DEFAULT_LOCALE


def link(settings: Settings, path: str, token: str) -> str:
    return f"{settings.public_base_url}{path}#token={token}"


def _mail(to: str, locale: str, kind: str, **values: str) -> MailMessage:
    texts = table(locale)
    return MailMessage(
        to,
        texts[f"{kind}.subject"],
        texts[f"{kind}.body"].format_map(values),
        locale,
    )


def signup_confirmation(
    settings: Settings, to: str, token: str, *, locale: str = DEFAULT_LOCALE
) -> MailMessage:
    return _mail(to, locale, "signup", link=link(settings, "/signup/confirm", token))


def account_exists(settings: Settings, to: str, *, locale: str = DEFAULT_LOCALE) -> MailMessage:
    base = settings.public_base_url
    return _mail(to, locale, "exists", signin=f"{base}/signin", reset=f"{base}/reset")


@dataclass(frozen=True, slots=True)
class ResetRemoved:
    """What ``admin mfa-reset`` removed besides the TOTP, recovery codes and password."""

    passkeys: int = 0
    identities: int = 0


def _removed_text(removed: ResetRemoved, locale: str) -> str:
    texts = table(locale)
    parts = [texts["reset.removed.mfa"]]
    if removed.passkeys:
        one = removed.passkeys == 1
        parts.append(texts["reset.removed.passkey" if one else "reset.removed.passkeys"])
    if removed.identities:
        parts.append(texts["reset.removed.google"])
    parts += [texts["reset.removed.password"], texts["reset.removed.sessions"]]
    return texts["list.separator"].join(parts[:-1]) + texts["list.last"] + parts[-1]


def password_reset(
    settings: Settings,
    to: str,
    token: str,
    *,
    passkeys: Sequence[tuple[str, datetime]] = (),
    after_mfa_reset: ResetRemoved | None = None,
    locale: str = DEFAULT_LOCALE,
) -> MailMessage:
    """The reset link. A plain reset lists the account's passkeys, which it keeps (ADR 0011
    section 3); after ``admin mfa-reset`` the mail says what the reset removed, naming only
    what the account had. A passkey's date is ISO, the same in every language."""
    texts = table(locale)
    intro = ""
    if after_mfa_reset is not None:
        intro = texts["reset.after_mfa_reset"].format_map(
            {"removed": _removed_text(after_mfa_reset, locale)}
        )
    listed = ""
    if passkeys and after_mfa_reset is None:
        names = "".join(
            texts["reset.passkey_line"].format_map(
                {"name": name, "date": created.date().isoformat()}
            )
            for name, created in passkeys
        )
        listed = texts["reset.passkeys_kept"].format_map(
            {"names": names, "security": f"{settings.public_base_url}/settings/security"}
        )
    return _mail(
        to, locale, "reset", intro=intro, link=link(settings, "/reset", token), passkeys=listed
    )


def email_change_confirmation(
    settings: Settings, to: str, token: str, *, locale: str = DEFAULT_LOCALE
) -> MailMessage:
    return _mail(to, locale, "email_change", link=link(settings, "/email-change", token))


def email_change_taken(settings: Settings, to: str, *, locale: str = DEFAULT_LOCALE) -> MailMessage:
    return _mail(to, locale, "email_taken", signin=f"{settings.public_base_url}/signin")


def email_changed_notice(to: str, *, locale: str = DEFAULT_LOCALE) -> MailMessage:
    return _mail(to, locale, "email_changed")


def totp_enrolled_notice(to: str, *, locale: str = DEFAULT_LOCALE) -> MailMessage:
    return _mail(to, locale, "totp_enrolled")


def mfa_failures_notice(to: str, *, locale: str = DEFAULT_LOCALE) -> MailMessage:
    return _mail(to, locale, "mfa_failures")


def provider_signup_confirmation(
    settings: Settings, to: str, provider: str, token: str, *, locale: str = DEFAULT_LOCALE
) -> MailMessage:
    return _mail(
        to,
        locale,
        "provider_signup",
        provider=provider.title(),
        link=link(settings, "/signup/confirm", token),
    )


def identity_linked_notice(to: str, provider: str, *, locale: str = DEFAULT_LOCALE) -> MailMessage:
    return _mail(to, locale, "identity_linked", provider=provider.title())


def passkey_added_notice(to: str, name: str, *, locale: str = DEFAULT_LOCALE) -> MailMessage:
    return _mail(to, locale, "passkey_added", name=name)


def passkey_suspended_notice(to: str, name: str, *, locale: str = DEFAULT_LOCALE) -> MailMessage:
    return _mail(to, locale, "passkey_suspended", name=name)


def qr_login_approved_notice(
    settings: Settings,
    to: str,
    browser: str,
    system: str,
    place: str,
    *,
    locale: str = DEFAULT_LOCALE,
) -> MailMessage:
    """ADR 0011 section 7: every completed QR approval is mailed. The new device as its browser
    reported it, never a code, nonce, token or health data (T59)."""
    return _mail(
        to,
        locale,
        "qr_approved",
        browser=browser,
        system=system,
        place=place,
        security=f"{settings.public_base_url}/settings/security",
    )
