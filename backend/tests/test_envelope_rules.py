"""The envelope's rules beyond the round trip (ADR 0004 sections 2 and 3): retired KEKs are
decrypt only, the two AADs of a value are bound to each other and to the KEK, nonces are 12
bytes, and the payload AAD binds the ciphertext on its own. Every key is synthetic."""

import uuid

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from keystools import KEY_A, KEY_B
from ownvitals.crypto.envelope import (
    DecryptFailed,
    Kek,
    KeyRetired,
    RetiredKek,
    Sealed,
    canonical_json,
    connection_aad,
    connection_wrap_aad,
    open_sealed,
    rewrap,
    seal,
    totp_aad,
    totp_wrap_aad,
    unwrap,
    wrap,
)

CURRENT = Kek("kek-2", KEY_B)
RETIRED = RetiredKek("kek-1", KEY_A)
SAME_BYTES_CURRENT = Kek("kek-1", KEY_A)  # what kek-1 was while it was current
OWNER, ROW = uuid.uuid4(), uuid.uuid4()
PAYLOAD = canonical_json({"access_token": "synthetic-access"})


def aads(key_id: str, owner: uuid.UUID = OWNER, row: uuid.UUID = ROW) -> tuple[bytes, bytes]:
    return (
        connection_aad(owner, "withings", row),
        connection_wrap_aad(key_id, owner, "withings", row),
    )


def sealed_under(kek: Kek, owner: uuid.UUID = OWNER, row: uuid.UUID = ROW) -> Sealed:
    aad, wrap_aad = aads(kek.key_id, owner, row)
    return seal(PAYLOAD, aad, kek, wrap_aad)


def open_with(sealed: Sealed, kek: Kek | RetiredKek, aad: bytes, wrap_aad: bytes) -> bytes:
    return open_sealed(
        sealed.ciphertext, sealed.nonce, sealed.wrapped_dek, sealed.dek_nonce, kek, aad, wrap_aad
    )


# --- retired KEKs are decrypt only (ADR 0004 section 3) -------------------------------------------


def test_a_retired_kek_opens_and_unwraps_what_its_key_sealed() -> None:
    sealed = sealed_under(SAME_BYTES_CURRENT)  # sealed while kek-1 was current
    aad, wrap_aad = aads("kek-1")
    assert open_with(sealed, RETIRED, aad, wrap_aad) == PAYLOAD
    assert len(unwrap(sealed.wrapped_dek, sealed.dek_nonce, RETIRED, wrap_aad)) == 32


def test_a_retired_kek_never_seals_wraps_or_receives_a_rewrap() -> None:
    aad, wrap_aad = aads("kek-1")
    with pytest.raises(KeyRetired) as raised:
        seal(PAYLOAD, aad, RETIRED, wrap_aad)  # pyright: ignore[reportArgumentType]
    assert raised.value.args == ("key_retired",)
    with pytest.raises(KeyRetired):
        wrap(bytes(32), RETIRED, wrap_aad)  # pyright: ignore[reportArgumentType]
    sealed = sealed_under(CURRENT)
    with pytest.raises(KeyRetired):
        rewrap(
            sealed.wrapped_dek,
            sealed.dek_nonce,
            CURRENT,
            aads("kek-2")[1],
            RETIRED,  # pyright: ignore[reportArgumentType]
            wrap_aad,
        )


def test_a_rewrap_moves_from_a_retired_kek_to_the_current_one() -> None:
    sealed = sealed_under(SAME_BYTES_CURRENT)
    aad, old_wrap_aad = aads("kek-1")
    new_wrap_aad = aads("kek-2")[1]
    wrapped, nonce = rewrap(
        sealed.wrapped_dek, sealed.dek_nonce, RETIRED, old_wrap_aad, CURRENT, new_wrap_aad
    )
    moved = Sealed(sealed.ciphertext, sealed.nonce, wrapped, nonce, "kek-2")
    assert open_with(moved, CURRENT, aad, new_wrap_aad) == PAYLOAD


# --- the AADs are bound to the KEK and to each other ----------------------------------------------


def test_the_wrap_aad_must_name_the_kek_that_wraps() -> None:
    aad, other_id = aads("kek-9")
    with pytest.raises(ValueError, match="name the id of the KEK"):
        seal(PAYLOAD, aad, CURRENT, other_id)
    with pytest.raises(ValueError, match="name the id of the KEK"):
        wrap(bytes(32), CURRENT, other_id)


def test_a_wrapped_key_opens_only_under_the_kek_its_wrap_aad_names() -> None:
    """Even with the right key bytes: a DEK wrapped for kek-2 doesn't open as kek-7."""
    sealed = sealed_under(CURRENT)
    aad, wrap_aad = aads("kek-2")
    alias = Kek("kek-7", KEY_B)  # the same bytes under another id
    for kek, named in ((alias, wrap_aad), (CURRENT, aads("kek-7")[1]), (alias, aads("kek-7")[1])):
        with pytest.raises(DecryptFailed):
            unwrap(sealed.wrapped_dek, sealed.dek_nonce, kek, named)
        with pytest.raises(DecryptFailed):
            open_with(sealed, kek, aad, named)


@pytest.mark.parametrize(
    "payload_aad",
    [
        connection_aad(uuid.uuid4(), "withings", ROW),  # another owner
        connection_aad(OWNER, "withings", uuid.uuid4()),  # another row
        connection_aad(OWNER, "other", ROW),  # another provider
        totp_aad(OWNER),  # another kind
        b"",
    ],
)
def test_the_payload_aad_must_name_the_value_the_wrap_aad_names(payload_aad: bytes) -> None:
    wrap_aad = aads("kek-2")[1]
    with pytest.raises(ValueError, match="name the same value"):
        seal(PAYLOAD, payload_aad, CURRENT, wrap_aad)
    with pytest.raises(DecryptFailed):
        open_with(sealed_under(CURRENT), CURRENT, payload_aad, wrap_aad)


@pytest.mark.parametrize(
    "wrap_aad",
    [
        b"",
        b"ownvitals-conn-v1|kek-2|x|y|z",  # a payload AAD in the wrap place
        b"ownvitals-dek-v1|kek-2",  # no value
        b"other-dek-v1|kek-2|x",
    ],
)
def test_a_wrap_aad_of_no_known_kind_refuses(wrap_aad: bytes) -> None:
    with pytest.raises(ValueError, match="name the id of the KEK"):
        wrap(bytes(32), CURRENT, wrap_aad)
    sealed = sealed_under(CURRENT)
    with pytest.raises(DecryptFailed):
        unwrap(sealed.wrapped_dek, sealed.dek_nonce, CURRENT, wrap_aad)


def test_the_totp_kind_is_bound_the_same_way() -> None:
    user = uuid.uuid4()
    sealed = seal(b"SYNTHETICSEED", totp_aad(user), CURRENT, totp_wrap_aad("kek-2", user))
    assert open_with(sealed, CURRENT, totp_aad(user), totp_wrap_aad("kek-2", user)) == (
        b"SYNTHETICSEED"
    )
    with pytest.raises(ValueError, match="name the same value"):
        seal(b"x", totp_aad(uuid.uuid4()), CURRENT, totp_wrap_aad("kek-2", user))


def test_a_rewrap_keeps_the_value_and_changes_only_the_kek() -> None:
    sealed = sealed_under(SAME_BYTES_CURRENT)
    old_wrap_aad = aads("kek-1")[1]
    for target in (aads("kek-2", row=uuid.uuid4())[1], totp_wrap_aad("kek-2", OWNER)):
        with pytest.raises(ValueError, match="keeps the value"):
            rewrap(sealed.wrapped_dek, sealed.dek_nonce, RETIRED, old_wrap_aad, CURRENT, target)


# --- the payload AAD binds the ciphertext on its own ----------------------------------------------


@pytest.mark.parametrize(
    ("owner", "row"),
    [(uuid.uuid4(), ROW), (OWNER, uuid.uuid4())],
    ids=["another owner", "another row"],
)
def test_ciphertext_moved_to_another_row_with_its_key_rewrapped_there_fails(
    owner: uuid.UUID, row: uuid.UUID
) -> None:
    """Someone with the KEK re-wraps a row's data key for another row of the same kind and
    copies the ciphertext there: the wrap now opens, and only the payload AAD refuses."""
    sealed = sealed_under(CURRENT)
    dek = unwrap(sealed.wrapped_dek, sealed.dek_nonce, CURRENT, aads("kek-2")[1])
    there_aad, there_wrap_aad = aads("kek-2", owner, row)
    wrapped, nonce = wrap(dek, CURRENT, there_wrap_aad)
    moved = Sealed(sealed.ciphertext, sealed.nonce, wrapped, nonce, "kek-2")
    assert len(unwrap(wrapped, nonce, CURRENT, there_wrap_aad)) == 32  # the wrap opens there
    with pytest.raises(DecryptFailed) as raised:
        open_with(moved, CURRENT, there_aad, there_wrap_aad)
    assert raised.value.__context__ is None


# --- nonces are 12 bytes --------------------------------------------------------------------------


@pytest.mark.parametrize("length", [0, 11, 13, 16])
@pytest.mark.parametrize("field", ["nonce", "dek_nonce"])
def test_a_nonce_of_another_length_fails_even_when_it_authenticates(
    field: str, length: int
) -> None:
    """AES-GCM takes other nonce lengths (8 to 128 bytes); a stored nonce is always 12 bytes
    (ADR 0004 section 2), so another length is refused before decrypting: here the payload or
    the data key is encrypted under that nonce for real, so only the length check refuses."""
    aad, wrap_aad = aads("kek-2")
    sealed = sealed_under(CURRENT)
    dek = unwrap(sealed.wrapped_dek, sealed.dek_nonce, CURRENT, wrap_aad)
    odd = bytes(range(1, length + 1))
    ciphertext, nonce = sealed.ciphertext, sealed.nonce
    wrapped_dek, dek_nonce = sealed.wrapped_dek, sealed.dek_nonce
    if field == "nonce":
        nonce = odd
        if length >= 8:  # what AES-GCM accepts: an authentic payload under that nonce
            ciphertext = AESGCM(dek).encrypt(odd, PAYLOAD, aad)
    else:
        dek_nonce = odd
        if length >= 8:
            wrapped_dek = AESGCM(CURRENT.key).encrypt(odd, dek, wrap_aad)
    with pytest.raises(DecryptFailed):
        open_sealed(ciphertext, nonce, wrapped_dek, dek_nonce, CURRENT, aad, wrap_aad)
