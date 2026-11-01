"""The texts of the mails we send (ADR 0003 section 5). Plain text; links come from
``OWNVITALS_PUBLIC_BASE_URL`` with the token in the URL fragment, never from the Host header."""

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


def password_reset(settings: Settings, to: str, token: str) -> MailMessage:
    return MailMessage(
        to,
        "Reset your ownvitals password",
        "To choose a new password, open this link (valid for 30 minutes):\n"
        f"{link(settings, '/reset', token)}\n\n"
        "Resetting signs you out everywhere. If this wasn't you, ignore this mail.\n",
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
