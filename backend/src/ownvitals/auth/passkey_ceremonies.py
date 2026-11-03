"""Passkey ceremonies: options, registration and authentication (ADR 0011 sections 1-3).

The relying party is the host of ``OWNVITALS_PUBLIC_BASE_URL`` (or ``webauthn_rp_id``) and the
only accepted origin is that URL's origin. Registration asks for a discoverable credential with
user verification and no attestation; sign-in asks for a discoverable credential with user
verification; neither restricts the authenticator's attachment or adds ``hints``, so a browser
may offer a phone over the hybrid transport. ``py_webauthn`` verifies the responses; only this
module imports it.

A passkey replaces the first factor, never the second (section 3): a passkey sign-in yields a
session in the same state as a password sign-in, and a ``reauth`` ceremony sets ``reauth_at``
only, never ``stepup_at``.

Verification order (section 1): the challenge (taken by ``DELETE ... RETURNING`` in its own
committed transaction, so it is single-use even when the rest fails; purpose and binding), origin
and type, the credential (known; owned by the ``userHandle``'s account, or by the session user for
re-auth; not suspended; its account active and, for sign-in, holding a confirmed TOTP), then
``py_webauthn``'s checks (``rp.id`` hash, UP and UV flags, the algorithm, the signature; it is
given a stored count of 0 so it never judges the counter before the signature), and last the
sign count.

A sign-count regression suspends the credential (section 2). The new count is written by a
conditional ``UPDATE ... WHERE sign_count = <read value>``, so two assertions with the same count
can't both pass. The final step runs with the account row locked ``FOR SHARE``
(``passkeys.lock_user``), so a sign-in can't create a session after ``reset_mfa`` removed the
account's passkeys and sessions.
"""

import json
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.cose import COSEAlgorithmIdentifier
from webauthn.helpers.options_to_json import options_to_json
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    ClientDataType,
    CredentialDeviceType,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from ownvitals.auth.passkeys import (
    COUNTER_MAX,
    CREDENTIAL_ID_MAX,
    HANDLE_BYTES,
    PASSKEY_COLUMNS,
    TIMEOUT_MS,
    Passkey,
    PasskeyFailure,
    RelyingParty,
    lock_user,
    new_challenge,
    read_client_data,
    take_challenge,
    to_passkey,
)
from ownvitals.auth.tables import mfa_totp, sessions, users, webauthn_credentials
from ownvitals.auth.totp import ISSUER as RP_NAME  # the product name authenticators show
from ownvitals.config import Settings
from ownvitals.db import system_session

# ES256, EdDSA, RS256, in that order (section 1).
ALGORITHMS = (
    COSEAlgorithmIdentifier.ECDSA_SHA_256,
    COSEAlgorithmIdentifier.EDDSA,
    COSEAlgorithmIdentifier.RSASSA_PKCS1_v1_5_SHA_256,
)
TRANSPORTS = frozenset(t.value for t in AuthenticatorTransport)


# --- options ------------------------------------------------------------------------------------


def _json(options: Any) -> dict[str, Any]:
    return json.loads(options_to_json(options))


async def _user_handle(db: AsyncSession, user_id: UUID) -> bytes:
    """The account's WebAuthn user handle, created on first use (32 random bytes)."""
    handle = (
        await db.execute(
            update(users)
            .where(users.c.id == user_id)
            .values(
                webauthn_handle=func.coalesce(
                    users.c.webauthn_handle, secrets.token_bytes(HANDLE_BYTES)
                )
            )
            .returning(users.c.webauthn_handle)
        )
    ).scalar_one()
    return bytes(handle)


async def registration_options(
    db: AsyncSession, rp: RelyingParty, user_id: UUID, email: str, session_id: UUID
) -> dict[str, Any]:
    """Section 1's registration options, with a new ``register`` challenge for this session."""
    handle = await _user_handle(db, user_id)
    existing = (
        await db.execute(
            select(webauthn_credentials.c.credential_id, webauthn_credentials.c.transports).where(
                webauthn_credentials.c.user_id == user_id
            )
        )
    ).all()
    challenge = await new_challenge(db, "register", user_id=user_id, session_id=session_id)
    options = generate_registration_options(
        rp_id=rp.rp_id,
        rp_name=RP_NAME,
        user_name=email,
        user_id=handle,
        user_display_name=email,
        challenge=challenge,
        timeout=TIMEOUT_MS,
        attestation=AttestationConveyancePreference.NONE,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            require_resident_key=True,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[_descriptor(r.credential_id, r.transports) for r in existing],
        supported_pub_key_algs=list(ALGORITHMS),
    )
    return _json(options)


def _descriptor(
    credential_id: bytes, transports: list[str] | None
) -> PublicKeyCredentialDescriptor:
    return PublicKeyCredentialDescriptor(
        id=bytes(credential_id),
        transports=[AuthenticatorTransport(t) for t in transports or () if t in TRANSPORTS] or None,
    )


async def signin_options(db: AsyncSession, rp: RelyingParty, presession_id: UUID) -> dict[str, Any]:
    """Discoverable sign-in: no ``allowCredentials``, user verification required."""
    challenge = await new_challenge(db, "signin", presession_id=presession_id)
    return _json(
        generate_authentication_options(
            rp_id=rp.rp_id,
            challenge=challenge,
            timeout=TIMEOUT_MS,
            allow_credentials=[],
            user_verification=UserVerificationRequirement.REQUIRED,
        )
    )


async def reauth_options(
    db: AsyncSession, rp: RelyingParty, user_id: UUID, session_id: UUID
) -> dict[str, Any] | None:
    """Re-auth: only the session user's credentials that are not suspended. ``None`` when the
    user has none."""
    rows = (
        await db.execute(
            select(webauthn_credentials.c.credential_id, webauthn_credentials.c.transports).where(
                webauthn_credentials.c.user_id == user_id,
                webauthn_credentials.c.suspended_at.is_(None),
            )
        )
    ).all()
    if not rows:
        return None
    challenge = await new_challenge(db, "reauth", user_id=user_id, session_id=session_id)
    return _json(
        generate_authentication_options(
            rp_id=rp.rp_id,
            challenge=challenge,
            timeout=TIMEOUT_MS,
            allow_credentials=[_descriptor(r.credential_id, r.transports) for r in rows],
            user_verification=UserVerificationRequirement.REQUIRED,
        )
    )


# --- registration -------------------------------------------------------------------------------


async def finish_registration(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    rp: RelyingParty,
    *,
    user_id: UUID,
    session_id: UUID,
    credential: dict[str, Any],
    client_data: str,
    transports: list[str],
    name: str,
) -> Passkey:
    """Verify a registration response and store the passkey. ``PasskeyFailure`` otherwise."""
    challenge = read_client_data(client_data, ClientDataType.WEBAUTHN_CREATE.value, rp)
    await take_challenge(factory, challenge, "register", session_id=session_id, user_id=user_id)
    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=rp.rp_id,
            expected_origin=rp.origin,
            require_user_presence=True,
            require_user_verification=True,
            supported_pub_key_algs=list(ALGORITHMS),
        )
    except Exception:  # the library's messages are never shown or logged
        raise PasskeyFailure("verification", user_id=user_id) from None
    if not 1 <= len(verified.credential_id) <= CREDENTIAL_ID_MAX or not (
        0 <= verified.sign_count <= COUNTER_MAX
    ):
        raise PasskeyFailure("verification", user_id=user_id)
    async with system_session(factory) as db:
        user = await lock_user(db, user_id)
        live = (
            await db.execute(
                select(sessions.c.id).where(
                    sessions.c.id == session_id, sessions.c.user_id == user_id
                )
            )
        ).first()
        if user is None or user.disabled_at is not None or live is None:
            raise PasskeyFailure("session", user_id=user_id)
        count = (
            await db.execute(
                select(func.count())
                .select_from(webauthn_credentials)
                .where(webauthn_credentials.c.user_id == user_id)
            )
        ).scalar_one()
        if count >= settings.passkeys_per_account:
            raise PasskeyFailure("limit", user_id=user_id)
        row = (
            await db.execute(
                pg_insert(webauthn_credentials)
                .values(
                    user_id=user_id,
                    credential_id=verified.credential_id,
                    public_key=verified.credential_public_key,
                    sign_count=verified.sign_count,
                    transports=sorted({t for t in transports if t in TRANSPORTS}),
                    aaguid=_aaguid(verified.aaguid),
                    backup_eligible=verified.credential_device_type
                    == CredentialDeviceType.MULTI_DEVICE,
                    backup_state=verified.credential_backed_up,
                    name=name,
                )
                .on_conflict_do_nothing(index_elements=[webauthn_credentials.c.credential_id])
                .returning(*PASSKEY_COLUMNS)
            )
        ).first()
        if row is None:
            raise PasskeyFailure("duplicate", user_id=user_id)
    return to_passkey(row)


def _aaguid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except (TypeError, ValueError):
        return None


# --- authentication -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoredCredential:
    id: UUID
    user_id: UUID
    email: str
    email_normalized: str
    name: str
    public_key: bytes
    sign_count: int
    suspended: bool
    disabled: bool
    handle: bytes | None
    totp_confirmed: bool


def _totp_confirmed() -> Any:
    return (
        select(mfa_totp.c.id)
        .where(mfa_totp.c.user_id == users.c.id, mfa_totp.c.confirmed_at.is_not(None))
        .exists()
    )


async def find_credential(db: AsyncSession, credential_id: bytes) -> StoredCredential | None:
    row = (
        await db.execute(
            select(
                webauthn_credentials.c.id,
                webauthn_credentials.c.user_id,
                users.c.email,
                users.c.email_normalized,
                webauthn_credentials.c.name,
                webauthn_credentials.c.public_key,
                webauthn_credentials.c.sign_count,
                webauthn_credentials.c.suspended_at.is_not(None).label("suspended"),
                users.c.disabled_at.is_not(None).label("disabled"),
                users.c.webauthn_handle,
                _totp_confirmed().label("totp_confirmed"),
            )
            .join(users, users.c.id == webauthn_credentials.c.user_id)
            .where(webauthn_credentials.c.credential_id == credential_id)
        )
    ).first()
    if row is None:
        return None
    return StoredCredential(
        row.id,
        row.user_id,
        row.email,
        row.email_normalized,
        row.name,
        bytes(row.public_key),
        int(row.sign_count),
        row.suspended,
        row.disabled,
        None if row.webauthn_handle is None else bytes(row.webauthn_handle),
        row.totp_confirmed,
    )


@dataclass(frozen=True, slots=True)
class Assertion:
    """The parts of an authentication response the checks before the signature need."""

    credential: dict[str, Any]  # as the browser sent it, for py_webauthn
    credential_id: bytes
    client_data: str
    user_handle: bytes | None


Gate = Callable[[StoredCredential | None], Awaitable[None]]


async def authenticate[T](
    factory: async_sessionmaker[AsyncSession],
    rp: RelyingParty,
    assertion: Assertion,
    *,
    purpose: Literal["signin", "reauth"],
    gate: Gate,
    finish: Callable[[AsyncSession, StoredCredential], Awaitable[T]],
    presession_id: UUID | None = None,
    session_id: UUID | None = None,
    session_user: UUID | None = None,
) -> T:
    """One authentication ceremony, in section 1's order.

    ``gate`` runs exactly once, after the credential lookup and before any refusal or the
    signature check: the routes reserve their rate-limit slots there (per IP, and per account
    once the credential names one), and it may raise ``RateLimited``. ``finish`` runs in the
    final transaction, with the account row locked and the new sign count written, and creates
    or rotates the session.
    """
    failure: str | None = None
    challenge = b""
    try:
        challenge = read_client_data(assertion.client_data, ClientDataType.WEBAUTHN_GET.value, rp)
    except PasskeyFailure as exc:
        failure = exc.reason
    if failure is None:
        try:
            await take_challenge(
                factory,
                challenge,
                purpose,
                presession_id=presession_id,
                session_id=session_id,
                user_id=session_user,
            )
        except PasskeyFailure as exc:
            failure = exc.reason
    async with system_session(factory) as db:
        stored = await find_credential(db, assertion.credential_id)
    if stored is not None and purpose == "reauth" and stored.user_id != session_user:
        stored = None  # another user's credential is unknown here
    await gate(stored)
    known = stored.user_id if stored is not None else session_user
    if failure is not None:
        raise PasskeyFailure(failure, user_id=known)
    if stored is None:
        raise PasskeyFailure("unknown_credential", user_id=known)
    _check_stored(stored, assertion, purpose)
    try:
        verified = verify_authentication_response(
            credential=assertion.credential,
            expected_challenge=challenge,
            expected_rp_id=rp.rp_id,
            expected_origin=rp.origin,
            credential_public_key=stored.public_key,
            credential_current_sign_count=0,  # the count is judged below, after the signature
            require_user_verification=True,
        )
    except Exception:
        raise PasskeyFailure("verification", user_id=stored.user_id) from None
    new_count = verified.new_sign_count
    if not 0 <= new_count <= COUNTER_MAX:
        raise PasskeyFailure("verification", user_id=stored.user_id)
    if (new_count > 0 or stored.sign_count > 0) and new_count <= stored.sign_count:
        async with system_session(factory) as db:
            suspended = await _suspend(db, stored.id)
        raise PasskeyFailure(
            "sign_count",
            user_id=stored.user_id,
            suspended=(stored.name, stored.email) if suspended else None,
        )
    async with system_session(factory) as db:
        user = await lock_user(db, stored.user_id, share=True)
        if user is None or user.disabled_at is not None:
            raise PasskeyFailure("account", user_id=stored.user_id)
        if (
            purpose == "signin"
            and not (
                await db.execute(select(_totp_confirmed()).where(users.c.id == stored.user_id))
            ).scalar_one()
        ):
            raise PasskeyFailure("no_totp", user_id=stored.user_id)
        moved = (
            await db.execute(
                update(webauthn_credentials)
                .where(
                    webauthn_credentials.c.id == stored.id,
                    webauthn_credentials.c.user_id == stored.user_id,
                    webauthn_credentials.c.sign_count == stored.sign_count,
                    webauthn_credentials.c.suspended_at.is_(None),
                )
                .values(
                    sign_count=new_count,
                    last_used_at=func.now(),
                    backup_state=verified.credential_backed_up,
                )
                .returning(webauthn_credentials.c.id)
            )
        ).first()
        if moved is None:
            raise PasskeyFailure("race", user_id=stored.user_id)
        return await finish(db, stored)


def _check_stored(stored: StoredCredential, assertion: Assertion, purpose: str) -> None:
    """The credential checks that come before the signature (section 1)."""
    missing = purpose == "signin" and (assertion.user_handle is None or stored.handle is None)
    if missing or (assertion.user_handle is not None and assertion.user_handle != stored.handle):
        raise PasskeyFailure("user_handle", user_id=stored.user_id)
    if stored.suspended:
        raise PasskeyFailure("suspended", user_id=stored.user_id)
    if stored.disabled:
        raise PasskeyFailure("account", user_id=stored.user_id)
    if purpose == "signin" and not stored.totp_confirmed:
        raise PasskeyFailure("no_totp", user_id=stored.user_id)


async def _suspend(db: AsyncSession, credential_row: UUID) -> bool:
    """Suspend a credential; ``True`` when this call suspended it (not already suspended)."""
    row = (
        await db.execute(
            update(webauthn_credentials)
            .where(
                webauthn_credentials.c.id == credential_row,
                webauthn_credentials.c.suspended_at.is_(None),
            )
            .values(suspended_at=func.now())
            .returning(webauthn_credentials.c.id)
        )
    ).first()
    return row is not None
