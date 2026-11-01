"""Accounts, sessions and sign-in (ADR 0003).

``ownvitals.auth`` is the web issuer of ``UserContext`` and the only package that touches the
account tables (``ownvitals.auth.tables``; guard 8 in ``tests/test_auth_tables_guard.py``). The
worker's purge task and the CLI call the functions exported here.

The package is lazy (PEP 562): importing ``ownvitals.auth`` or one of its modules loads only
that module, and a name below is imported on first use. So the worker, which needs only
``purge``, never loads the web-only sign-in and mail modules (``oauth_client``, ``mail``) that
the outbound-allowlist guard exempts (ADR 0006 section 9; ``tests/test_egress_guard.py``).
"""

from importlib import import_module
from importlib.util import find_spec
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ownvitals.auth.accounts import (
        AccountError,
        InviteRow,
        UserRow,
        create_admin_invite,
        create_invite,
        create_user,
        disable_invite,
        find_user,
        get_invite,
        grant_admin,
        grant_admin_if_listed,
        list_invites,
        reset_mfa,
        revoke_admin,
    )
    from ownvitals.auth.admin_accounts import (
        AccountRow,
        delete_account,
        disable_account,
        enable_account,
        find_accounts,
        get_account,
    )
    from ownvitals.auth.admin_lookup import account_emails
    from ownvitals.auth.clientip import bucket_ip, client_ip
    from ownvitals.auth.cookies import (
        CSRF_COOKIE,
        DEVICE_COOKIE,
        SESSION_COOKIE,
        clear_session_cookie,
        set_device_cookie,
        set_session_cookie,
    )
    from ownvitals.auth.deps import (
        current_context,
        current_session,
        operator_contexts,
        request_device_token,
        request_ip,
        request_session_token,
        require_admin,
        require_recent_auth,
        require_stepup,
        require_user,
    )
    from ownvitals.auth.devices import forget_devices, is_known_device, remember_device
    from ownvitals.auth.events import record_event
    from ownvitals.auth.flowtools import EmailAddress
    from ownvitals.auth.hooks import register_reset_link_sender, send_reset_link
    from ownvitals.auth.keys import (
        Keys,
        decrypt_totp_secret,
        derive_key,
        encrypt_totp_secret,
        hmac_digest,
        token_hash,
    )
    from ownvitals.auth.notices import SecurityNotifier, register_security_notifier
    from ownvitals.auth.operator import (
        WipeEvent,
        mark_wipe_mailed,
        pending_wipe_mails,
        vacuum_wiped_tables,
        wipe_mail,
        wipe_user,
    )
    from ownvitals.auth.owners import owner_active
    from ownvitals.auth.passwords import (
        PasswordHashBusy,
        PasswordHasher,
        PasswordRuleError,
        normalize_password,
    )
    from ownvitals.auth.purge import purge_expired
    from ownvitals.auth.ratelimit import (
        Attempt,
        Bucket,
        RateLimited,
        RateLimiter,
        admin_buckets,
        connect_buckets,
        mfa_buckets,
        password_buckets,
        redeem_buckets,
        spotify_start_buckets,
    )
    from ownvitals.auth.reset_routes import make_reset_link_sender
    from ownvitals.auth.sessions import (
        IssuedSession,
        Resolution,
        SessionGone,
        SessionInfo,
        list_sessions,
        resolve_session,
        revoke_session,
        revoke_user_sessions,
        rotate_session,
        start_session,
    )
    from ownvitals.auth.setup import attach_state, detach_state, install
    from ownvitals.auth.totp_keys import (
        TotpUnreadable,
        migrate_v1_secrets,
        rewrap_v2_secrets,
        totp_rows_by_key,
    )

# Exported name -> its module in this package. ``tests/test_egress_runtime.py`` checks it matches
# the ``TYPE_CHECKING`` imports above and ``__all__``.
_EXPORTS: dict[str, str] = {
    "AccountError": "accounts",
    "UserRow": "accounts",
    "create_invite": "accounts",
    "InviteRow": "accounts",
    "create_admin_invite": "accounts",
    "disable_invite": "accounts",
    "get_invite": "accounts",
    "list_invites": "accounts",
    "create_user": "accounts",
    "find_user": "accounts",
    "grant_admin": "accounts",
    "grant_admin_if_listed": "accounts",
    "reset_mfa": "accounts",
    "revoke_admin": "accounts",
    "AccountRow": "admin_accounts",
    "get_account": "admin_accounts",
    "delete_account": "admin_accounts",
    "disable_account": "admin_accounts",
    "enable_account": "admin_accounts",
    "find_accounts": "admin_accounts",
    "account_emails": "admin_lookup",
    "bucket_ip": "clientip",
    "client_ip": "clientip",
    "CSRF_COOKIE": "cookies",
    "DEVICE_COOKIE": "cookies",
    "SESSION_COOKIE": "cookies",
    "clear_session_cookie": "cookies",
    "set_device_cookie": "cookies",
    "set_session_cookie": "cookies",
    "current_context": "deps",
    "current_session": "deps",
    "operator_contexts": "deps",
    "request_device_token": "deps",
    "request_ip": "deps",
    "request_session_token": "deps",
    "require_admin": "deps",
    "require_recent_auth": "deps",
    "require_stepup": "deps",
    "require_user": "deps",
    "forget_devices": "devices",
    "is_known_device": "devices",
    "remember_device": "devices",
    "record_event": "events",
    "EmailAddress": "flowtools",
    "register_reset_link_sender": "hooks",
    "send_reset_link": "hooks",
    "Keys": "keys",
    "decrypt_totp_secret": "keys",
    "derive_key": "keys",
    "encrypt_totp_secret": "keys",
    "hmac_digest": "keys",
    "token_hash": "keys",
    "SecurityNotifier": "notices",
    "register_security_notifier": "notices",
    "owner_active": "owners",
    "WipeEvent": "operator",
    "mark_wipe_mailed": "operator",
    "pending_wipe_mails": "operator",
    "vacuum_wiped_tables": "operator",
    "wipe_mail": "operator",
    "wipe_user": "operator",
    "PasswordHashBusy": "passwords",
    "PasswordHasher": "passwords",
    "PasswordRuleError": "passwords",
    "normalize_password": "passwords",
    "purge_expired": "purge",
    "Attempt": "ratelimit",
    "Bucket": "ratelimit",
    "RateLimited": "ratelimit",
    "RateLimiter": "ratelimit",
    "connect_buckets": "ratelimit",
    "mfa_buckets": "ratelimit",
    "password_buckets": "ratelimit",
    "redeem_buckets": "ratelimit",
    "spotify_start_buckets": "ratelimit",
    "admin_buckets": "ratelimit",
    "make_reset_link_sender": "reset_routes",
    "IssuedSession": "sessions",
    "Resolution": "sessions",
    "SessionGone": "sessions",
    "SessionInfo": "sessions",
    "list_sessions": "sessions",
    "resolve_session": "sessions",
    "revoke_session": "sessions",
    "revoke_user_sessions": "sessions",
    "rotate_session": "sessions",
    "start_session": "sessions",
    "attach_state": "setup",
    "detach_state": "setup",
    "install": "setup",
    "TotpUnreadable": "totp_keys",
    "migrate_v1_secrets": "totp_keys",
    "rewrap_v2_secrets": "totp_keys",
    "totp_rows_by_key": "totp_keys",
}


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is not None:
        value = getattr(import_module(f"{__name__}.{module}"), name)
        globals()[name] = value
        return value
    if not name.startswith("_") and "." not in name and _is_submodule(name):
        return import_module(f"{__name__}.{name}")  # a submodule, as ``auth.tables``
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _is_submodule(name: str) -> bool:
    try:
        return find_spec(f"{__name__}.{name}") is not None
    except (
        ImportError
    ):  # not a package below ``name``: an attribute lookup answers "no", never raises
        return False


def __dir__() -> list[str]:
    return sorted({*globals(), *_EXPORTS})


__all__ = [
    "CSRF_COOKIE",
    "DEVICE_COOKIE",
    "SESSION_COOKIE",
    "AccountError",
    "AccountRow",
    "Attempt",
    "Bucket",
    "EmailAddress",
    "InviteRow",
    "IssuedSession",
    "Keys",
    "PasswordHashBusy",
    "PasswordHasher",
    "PasswordRuleError",
    "RateLimited",
    "RateLimiter",
    "Resolution",
    "SecurityNotifier",
    "SessionGone",
    "SessionInfo",
    "TotpUnreadable",
    "UserRow",
    "WipeEvent",
    "account_emails",
    "admin_buckets",
    "attach_state",
    "bucket_ip",
    "clear_session_cookie",
    "client_ip",
    "connect_buckets",
    "create_admin_invite",
    "create_invite",
    "create_user",
    "current_context",
    "current_session",
    "decrypt_totp_secret",
    "delete_account",
    "derive_key",
    "detach_state",
    "disable_account",
    "disable_invite",
    "enable_account",
    "encrypt_totp_secret",
    "find_accounts",
    "find_user",
    "forget_devices",
    "get_account",
    "get_invite",
    "grant_admin",
    "grant_admin_if_listed",
    "hmac_digest",
    "install",
    "is_known_device",
    "list_invites",
    "list_sessions",
    "make_reset_link_sender",
    "mark_wipe_mailed",
    "mfa_buckets",
    "migrate_v1_secrets",
    "normalize_password",
    "operator_contexts",
    "owner_active",
    "password_buckets",
    "pending_wipe_mails",
    "purge_expired",
    "record_event",
    "redeem_buckets",
    "register_reset_link_sender",
    "register_security_notifier",
    "remember_device",
    "request_device_token",
    "request_ip",
    "request_session_token",
    "require_admin",
    "require_recent_auth",
    "require_stepup",
    "require_user",
    "reset_mfa",
    "resolve_session",
    "revoke_admin",
    "revoke_session",
    "revoke_user_sessions",
    "rewrap_v2_secrets",
    "rotate_session",
    "send_reset_link",
    "set_device_cookie",
    "set_session_cookie",
    "spotify_start_buckets",
    "start_session",
    "token_hash",
    "totp_rows_by_key",
    "vacuum_wiped_tables",
    "wipe_mail",
    "wipe_user",
]
