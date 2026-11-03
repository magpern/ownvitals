"""The texts of the mails we send (ADR 0003 section 5). Plain text; links come from
``OWNVITALS_PUBLIC_BASE_URL`` with the token in the URL fragment, never from the Host header."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from ownvitals.config import Settings
from ownvitals.mail import MailMessage


def link(settings: Settings, path: str, token: str) -> str:
    return f"{settings.public_base_url}{path}#token={token}"


def signup_confirmation(settings: Settings, to: str, token: str) -> MailMessage:
    return MailMessage(
        to,
        "Confirm your ownvitals account",
        "Someone asked to create an ownvitals account with this address.\n\n"
        "To finish, open this link (valid for 24 hours) and type the password you chose:\n"
        f"{link(settings, '/signup/confirm', token)}\n\n"
        "If this wasn't you, ignore this mail. No account is created.\n",
    )


def account_exists(settings: Settings, to: str) -> MailMessage:
    return MailMessage(
        to,
        "You already have an ownvitals account",
        "Someone asked to create an ownvitals account with this address, which already has one.\n\n"
        f"Sign in: {settings.public_base_url}/signin\n"
        f"Forgot your password? {settings.public_base_url}/reset\n\n"
        "If this wasn't you, ignore this mail.\n",
    )


@dataclass(frozen=True, slots=True)
class ResetRemoved:
    """What ``admin mfa-reset`` removed besides the TOTP, recovery codes and password."""

    passkeys: int = 0
    identities: int = 0


def _removed_text(removed: ResetRemoved) -> str:
    parts = ["your authenticator app and recovery codes were removed"]
    if removed.passkeys:
        parts.append(
            "your passkey was removed" if removed.passkeys == 1 else "your passkeys were removed"
        )
    if removed.identities:
        parts.append("Google sign-in was unlinked")
    parts += ["your password was cleared", "you were signed out everywhere"]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def password_reset(
    settings: Settings,
    to: str,
    token: str,
    *,
    passkeys: Sequence[tuple[str, datetime]] = (),
    after_mfa_reset: ResetRemoved | None = None,
) -> MailMessage:
    """The reset link. A plain reset lists the account's passkeys, which it keeps (ADR 0011
    section 3); after ``admin mfa-reset`` the mail says what the reset removed, naming only
    what the account had."""
    intro = ""
    if after_mfa_reset is not None:
        intro = (
            "The operator reset the two-factor authentication of your ownvitals account: "
            f"{_removed_text(after_mfa_reset)}. Choose a new password with the link below, then "
            "set up two-factor authentication again.\n\n"
        )
    listed = ""
    if passkeys and after_mfa_reset is None:
        names = "".join(f"- {name} (added {created:%Y-%m-%d})\n" for name, created in passkeys)
        listed = (
            "\nResetting the password keeps these passkeys on your account:\n"
            f"{names}"
            f"Review them on the Security page: {settings.public_base_url}/settings/security\n"
        )
    return MailMessage(
        to,
        "Reset your ownvitals password",
        f"{intro}To choose a new password, open this link (valid for 30 minutes):\n"
        f"{link(settings, '/reset', token)}\n\n"
        "Resetting signs you out everywhere. If this wasn't you, ignore this mail.\n"
        f"{listed}",
    )


def email_change_confirmation(settings: Settings, to: str, token: str) -> MailMessage:
    return MailMessage(
        to,
        "Confirm your new ownvitals email address",
        "To use this address for your ownvitals account, sign in and open this link "
        f"(valid for 1 hour):\n{link(settings, '/email-change', token)}\n\n"
        "If this wasn't you, ignore this mail.\n",
    )


def email_change_taken(settings: Settings, to: str) -> MailMessage:
    return MailMessage(
        to,
        "An ownvitals account already uses this address",
        "Someone asked to move an ownvitals account to this address, which already belongs "
        f"to an account.\n\nSign in: {settings.public_base_url}/signin\n\n"
        "If this wasn't you, ignore this mail.\n",
    )


def email_changed_notice(to: str) -> MailMessage:
    return MailMessage(
        to,
        "Your ownvitals email address was changed",
        "The email address of your ownvitals account was changed. This is the old address.\n"
        "If this wasn't you, contact the operator.\n",
    )


def totp_enrolled_notice(to: str) -> MailMessage:
    return MailMessage(
        to,
        "Two-factor authentication was set up on your ownvitals account",
        "An authenticator app was enrolled (or replaced) on your ownvitals account.\n"
        "If this wasn't you, reset your password and contact the operator.\n",
    )


def mfa_failures_notice(to: str) -> MailMessage:
    return MailMessage(
        to,
        "Failed two-factor attempts on your ownvitals account",
        "There were five failed two-factor attempts on your ownvitals account within an hour.\n"
        "If this wasn't you, change your password.\n",
    )


def provider_signup_confirmation(
    settings: Settings, to: str, provider: str, token: str
) -> MailMessage:
    return MailMessage(
        to,
        "Confirm your ownvitals account",
        f"Someone signed in with {provider.title()} and asked to create an ownvitals account "
        "with this address.\n\n"
        "To finish, open this link in the same browser (valid for 24 hours):\n"
        f"{link(settings, '/signup/confirm', token)}\n\n"
        "If this wasn't you, ignore this mail. No account is created.\n",
    )


def identity_linked_notice(to: str, provider: str) -> MailMessage:
    return MailMessage(
        to,
        "A sign-in method was added to your ownvitals account",
        f"{provider.title()} was linked to your ownvitals account as a way to sign in.\n"
        "If this wasn't you, sign in, remove it from your account page and change your "
        "password.\n",
    )


def passkey_added_notice(to: str, name: str) -> MailMessage:
    return MailMessage(
        to,
        "A passkey was added to your ownvitals account",
        f'A passkey named "{name}" was added to your ownvitals account as a way to sign in.\n'
        "If this wasn't you, sign in, remove it on the Security page and change your password.\n",
    )


def passkey_suspended_notice(to: str, name: str) -> MailMessage:
    return MailMessage(
        to,
        "A passkey on your ownvitals account was refused and suspended",
        f'A sign-in with your passkey "{name}" was refused and the passkey was suspended: its '
        "counter went backwards, which can mean it was copied.\n"
        "It can't be used any more. Remove it on the Security page and add it again. If you "
        "didn't just try to sign in with it, change your password.\n",
    )
