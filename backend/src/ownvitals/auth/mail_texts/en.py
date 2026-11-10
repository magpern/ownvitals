"""The English texts of the account mails, the source of every locale (ADR 0003 section 5, ADR
0015 section 7). Plain text with ``str.format`` placeholders; ``ownvitals.auth.mails`` fills them
with links built from ``OWNVITALS_PUBLIC_BASE_URL`` and the few values each mail names."""

from types import MappingProxyType
from typing import Final

TEXTS: Final = MappingProxyType(
    {
        "signup.subject": "Confirm your ownvitals account",
        "signup.body": (
            "Someone asked to create an ownvitals account with this address.\n\n"
            "To finish, open this link (valid for 24 hours) and type the password you chose:\n"
            "{link}\n\n"
            "If this wasn't you, ignore this mail. No account is created.\n"
        ),
        "provider_signup.subject": "Confirm your ownvitals account",
        "provider_signup.body": (
            "Someone signed in with {provider} and asked to create an ownvitals account "
            "with this address.\n\n"
            "To finish, open this link in the same browser (valid for 24 hours):\n"
            "{link}\n\n"
            "If this wasn't you, ignore this mail. No account is created.\n"
        ),
        "exists.subject": "You already have an ownvitals account",
        "exists.body": (
            "Someone asked to create an ownvitals account with this address, which already has "
            "one.\n\n"
            "Sign in: {signin}\n"
            "Forgot your password? {reset}\n\n"
            "If this wasn't you, ignore this mail.\n"
        ),
        "reset.subject": "Reset your ownvitals password",
        "reset.body": (
            "{intro}To choose a new password, open this link (valid for 30 minutes):\n"
            "{link}\n\n"
            "Resetting signs you out everywhere. If this wasn't you, ignore this mail.\n"
            "{passkeys}"
        ),
        "reset.after_mfa_reset": (
            "The operator reset the two-factor authentication of your ownvitals account: "
            "{removed}. Choose a new password with the link below, then set up two-factor "
            "authentication again.\n\n"
        ),
        "reset.removed.mfa": "your authenticator app and recovery codes were removed",
        "reset.removed.passkey": "your passkey was removed",
        "reset.removed.passkeys": "your passkeys were removed",
        "reset.removed.google": "Google sign-in was unlinked",
        "reset.removed.password": "your password was cleared",
        "reset.removed.sessions": "you were signed out everywhere",
        "reset.passkeys_kept": (
            "\nResetting the password keeps these passkeys on your account:\n"
            "{names}"
            "Review them on the Security page: {security}\n"
        ),
        "reset.passkey_line": "- {name} (added {date})\n",
        "list.separator": ", ",
        "list.last": " and ",
        "email_change.subject": "Confirm your new ownvitals email address",
        "email_change.body": (
            "To use this address for your ownvitals account, sign in and open this link "
            "(valid for 1 hour):\n{link}\n\n"
            "If this wasn't you, ignore this mail.\n"
        ),
        "email_taken.subject": "An ownvitals account already uses this address",
        "email_taken.body": (
            "Someone asked to move an ownvitals account to this address, which already belongs "
            "to an account.\n\nSign in: {signin}\n\n"
            "If this wasn't you, ignore this mail.\n"
        ),
        "email_changed.subject": "Your ownvitals email address was changed",
        "email_changed.body": (
            "The email address of your ownvitals account was changed. This is the old address.\n"
            "If this wasn't you, contact the operator.\n"
        ),
        "totp_enrolled.subject": "Two-factor authentication was set up on your ownvitals account",
        "totp_enrolled.body": (
            "An authenticator app was enrolled (or replaced) on your ownvitals account.\n"
            "If this wasn't you, reset your password and contact the operator.\n"
        ),
        "mfa_failures.subject": "Failed two-factor attempts on your ownvitals account",
        "mfa_failures.body": (
            "There were five failed two-factor attempts on your ownvitals account within an "
            "hour.\n"
            "If this wasn't you, change your password.\n"
        ),
        "identity_linked.subject": "A sign-in method was added to your ownvitals account",
        "identity_linked.body": (
            "{provider} was linked to your ownvitals account as a way to sign in.\n"
            "If this wasn't you, sign in, remove it from your account page and change your "
            "password.\n"
        ),
        "passkey_added.subject": "A passkey was added to your ownvitals account",
        "passkey_added.body": (
            'A passkey named "{name}" was added to your ownvitals account as a way to sign in.\n'
            "If this wasn't you, sign in, remove it on the Security page and change your "
            "password.\n"
        ),
        "passkey_suspended.subject": (
            "A passkey on your ownvitals account was refused and suspended"
        ),
        "passkey_suspended.body": (
            'Your passkey "{name}" was refused and suspended when it was just used: its counter '
            "went backwards, which can mean it was copied.\n"
            "It can't be used any more. Remove it on the Security page and add it again. If you "
            "didn't just use it to sign in or to confirm it's you, change your password.\n"
        ),
        "qr_approved.subject": "A new sign-in to ownvitals was approved from your phone",
        "qr_approved.body": (
            "A new sign-in to your ownvitals account was approved from your phone:\n"
            "{browser} on {system} ({place}), as reported by that browser.\n\n"
            "If this wasn't you, sign that device out and change your password: "
            "{security}\n"
        ),
    }
)
