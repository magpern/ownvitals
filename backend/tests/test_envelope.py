"""``ownvitals.crypto.envelope``: the pure envelope primitive (ADR 0004 section 2)."""

import copy
import pickle
import uuid
from collections.abc import Callable

import pytest

from keystools import KEY_A, KEY_B
from ownvitals.config import CONNECTION_KEK_ID, WEBHOOK_PEPPER_ID, WEBHOOK_SEED_ID
from ownvitals.crypto.envelope import (
    CHECK_VALUE_LENGTH,
    DecryptFailed,
    Kek,
    KeyKind,
    RingKey,
    Sealed,
    WebhookPepper,
    WebhookSeed,
    canonical_json,
    check_value,
    connection_aad,
    connection_wrap_aad,
    is_key_id,
    open_sealed,
    rewrap,
    seal,
    totp_aad,
    totp_wrap_aad,
    unwrap,
    wrap,
)

KEK = Kek("kek-1", KEY_A)
OTHER_KEK = Kek("kek-2", KEY_B)
OWNER, ROW = uuid.uuid4(), uuid.uuid4()
# Synthetic token-shaped payload: no real credential.
PAYLOAD = canonical_json({"access_token": "synthetic-access", "refresh_token": "synthetic-refresh"})


def sealed_for(
    owner: uuid.UUID = OWNER, provider: str = "withings", row: uuid.UUID = ROW
) -> Sealed:
    return seal(
        PAYLOAD,
        connection_aad(owner, provider, row),
        KEK,
        connection_wrap_aad(KEK.key_id, owner, provider, row),
    )


def open_for(
    sealed: Sealed,
    owner: uuid.UUID = OWNER,
    provider: str = "withings",
    row: uuid.UUID = ROW,
    kek: Kek = KEK,
    key_id: str | None = None,
) -> bytes:
    return open_sealed(
        sealed.ciphertext,
        sealed.nonce,
        sealed.wrapped_dek,
        sealed.dek_nonce,
        kek,
        connection_aad(owner, provider, row),
        connection_wrap_aad(key_id or kek.key_id, owner, provider, row),
    )


def test_round_trip() -> None:
    sealed = sealed_for()
    assert open_for(sealed) == PAYLOAD
    assert sealed.key_id == "kek-1"
    assert (len(sealed.nonce), len(sealed.dek_nonce), len(sealed.wrapped_dek)) == (12, 12, 48)
    assert PAYLOAD not in sealed.ciphertext
    assert PAYLOAD not in sealed.wrapped_dek


def test_stored_bytes_may_come_back_as_memoryview() -> None:
    sealed = sealed_for()
    assert (
        open_sealed(
            memoryview(sealed.ciphertext),
            memoryview(sealed.nonce),
            memoryview(sealed.wrapped_dek),
            memoryview(sealed.dek_nonce),
            KEK,
            connection_aad(OWNER, "withings", ROW),
            connection_wrap_aad("kek-1", OWNER, "withings", ROW),
        )
        == PAYLOAD
    )


def test_every_write_has_a_new_data_key_and_nonces() -> None:
    """ADR 0004 section 2: a DEK encrypts exactly one payload. The same plaintext sealed again
    for the same row under the same KEK gets another data key, and other nonces."""
    wrap_aad = connection_wrap_aad("kek-1", OWNER, "withings", ROW)
    writes = [sealed_for() for _ in range(16)]
    data_keys = [unwrap(s.wrapped_dek, s.dek_nonce, KEK, wrap_aad) for s in writes]
    assert all(len(dek) == 32 for dek in data_keys)
    assert len(set(data_keys)) == len(writes)  # the data keys themselves differ
    assert KEY_A not in data_keys  # and none is the KEK
    assert len({s.nonce for s in writes}) == len(writes)
    assert len({s.dek_nonce for s in writes}) == len(writes)
    assert len({s.ciphertext for s in writes}) == len(writes)


@pytest.mark.parametrize(
    "change",
    [
        {"owner": uuid.uuid4()},  # another user's row
        {"provider": "other"},  # another provider's row
        {"row": uuid.uuid4()},  # another row of the same user and provider
        {"kek": OTHER_KEK},  # another KEK
        {"key_id": "kek-9"},  # the right key under another id (wrap AAD)
    ],
)
def test_wrong_aad_or_key_fails(change: dict[str, object]) -> None:
    with pytest.raises(DecryptFailed) as raised:
        open_for(sealed_for(), **change)  # pyright: ignore[reportArgumentType]
    assert raised.value.args == ("decrypt_failed",)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.parametrize("field", ["ciphertext", "nonce", "wrapped_dek", "dek_nonce"])
def test_tampered_bytes_fail(field: str) -> None:
    sealed = sealed_for()
    value = bytearray(getattr(sealed, field))
    value[0] ^= 1
    parts = {
        name: bytes(value) if name == field else getattr(sealed, name)
        for name in ("ciphertext", "nonce", "wrapped_dek", "dek_nonce")
    }
    with pytest.raises(DecryptFailed) as raised:
        open_sealed(
            parts["ciphertext"],
            parts["nonce"],
            parts["wrapped_dek"],
            parts["dek_nonce"],
            KEK,
            connection_aad(OWNER, "withings", ROW),
            connection_wrap_aad(KEK.key_id, OWNER, "withings", ROW),
        )
    assert raised.value.__context__ is None


@pytest.mark.parametrize("field", ["ciphertext", "wrapped_dek"])
@pytest.mark.parametrize("length", [0, 15, -1])  # empty, shorter than a tag, one byte short
def test_truncated_bytes_fail(field: str, length: int) -> None:
    sealed = sealed_for()
    parts = {"ciphertext": sealed.ciphertext, "wrapped_dek": sealed.wrapped_dek}
    parts[field] = parts[field][:length]
    with pytest.raises(DecryptFailed):
        open_sealed(
            parts["ciphertext"],
            sealed.nonce,
            parts["wrapped_dek"],
            sealed.dek_nonce,
            KEK,
            connection_aad(OWNER, "withings", ROW),
            connection_wrap_aad("kek-1", OWNER, "withings", ROW),
        )


def test_a_truncated_nonce_fails_closed() -> None:
    sealed = sealed_for()
    with pytest.raises(DecryptFailed):
        unwrap(sealed.wrapped_dek, b"", KEK, connection_wrap_aad("kek-1", OWNER, "withings", ROW))


def test_rewrap_keeps_the_data_key_and_the_ciphertext() -> None:
    sealed = sealed_for()
    wrapped, nonce = rewrap(
        sealed.wrapped_dek,
        sealed.dek_nonce,
        KEK,
        connection_wrap_aad("kek-1", OWNER, "withings", ROW),
        OTHER_KEK,
        connection_wrap_aad("kek-2", OWNER, "withings", ROW),
    )
    assert wrapped != sealed.wrapped_dek
    moved = Sealed(sealed.ciphertext, sealed.nonce, wrapped, nonce, "kek-2")
    assert open_for(moved, kek=OTHER_KEK) == PAYLOAD
    with pytest.raises(DecryptFailed):
        open_for(moved, kek=KEK, key_id="kek-2")  # the old KEK no longer opens the new wrap
    # The old wrap still opens with the old KEK: rewrap is no response to a leaked key.
    assert open_for(sealed) == PAYLOAD


def test_totp_aad_binds_the_user_and_the_key_id() -> None:
    user = uuid.uuid4()
    seed = b"SYNTHETICTOTPSEED"
    sealed = seal(seed, totp_aad(user), KEK, totp_wrap_aad("kek-1", user))
    args = (sealed.ciphertext, sealed.nonce, sealed.wrapped_dek, sealed.dek_nonce, KEK)
    assert open_sealed(*args, totp_aad(user), totp_wrap_aad("kek-1", user)) == seed
    other = uuid.uuid4()
    with pytest.raises(DecryptFailed):
        open_sealed(*args, totp_aad(other), totp_wrap_aad("kek-1", other))
    # A connection payload's AAD never opens a TOTP seed, and the other way round.
    with pytest.raises(DecryptFailed):
        open_sealed(*args, connection_aad(user, "withings", user), totp_wrap_aad("kek-1", user))
    with pytest.raises(DecryptFailed):
        open_sealed(*args, totp_aad(user), connection_wrap_aad("kek-1", user, "withings", user))


def test_the_aad_strings_are_exactly_the_adr_forms() -> None:
    owner = uuid.UUID("0f8e0d6c-1d4a-4c5e-9b1e-2a3b4c5d6e7f")
    row = uuid.UUID("11111111-2222-3333-4444-555555555555")
    assert connection_aad(owner, "withings", row) == (
        b"ownvitals-conn-v1|0f8e0d6c-1d4a-4c5e-9b1e-2a3b4c5d6e7f|withings|"
        b"11111111-2222-3333-4444-555555555555"
    )
    assert connection_wrap_aad("kek-1", owner, "withings", row) == (
        b"ownvitals-dek-v1|kek-1|0f8e0d6c-1d4a-4c5e-9b1e-2a3b4c5d6e7f|withings|"
        b"11111111-2222-3333-4444-555555555555"
    )
    assert totp_aad(owner) == b"ownvitals-totp-v1|0f8e0d6c-1d4a-4c5e-9b1e-2a3b4c5d6e7f"
    assert totp_wrap_aad("kek-a", owner) == (
        b"ownvitals-totp-dek-v1|kek-a|0f8e0d6c-1d4a-4c5e-9b1e-2a3b4c5d6e7f"
    )


@pytest.mark.parametrize(
    ("call", "error"),
    [
        (lambda: connection_aad(OWNER, "withings|x", ROW), ValueError),
        (lambda: connection_aad(OWNER, "Withings", ROW), ValueError),
        (lambda: connection_aad(OWNER, "", ROW), ValueError),
        (lambda: connection_aad(str(OWNER), "withings", ROW), TypeError),  # pyright: ignore[reportArgumentType]
        (lambda: connection_wrap_aad("kek-1|x", OWNER, "withings", ROW), ValueError),
        (lambda: connection_wrap_aad("seed-1", OWNER, "withings", ROW), ValueError),
        (lambda: totp_wrap_aad("pep-1", OWNER), ValueError),
        (lambda: totp_aad(str(OWNER)), TypeError),  # pyright: ignore[reportArgumentType]
    ],
)
def test_aad_fields_cannot_carry_a_separator_or_another_kind(
    call: Callable[[], bytes], error: type[Exception]
) -> None:
    with pytest.raises(error):
        call()


@pytest.mark.parametrize(
    ("value", "kind", "ok"),
    [
        ("kek-1", KeyKind.KEK, True),
        ("kek-2026-10-a", KeyKind.KEK, True),
        ("kek-" + "a" * 28, KeyKind.KEK, True),
        ("kek-" + "a" * 29, KeyKind.KEK, False),
        ("kek-", KeyKind.KEK, False),
        ("KEK-1", KeyKind.KEK, False),
        ("kek_1", KeyKind.KEK, False),
        ("kek-1\n", KeyKind.KEK, False),
        ("../kek-1", KeyKind.KEK, False),
        ("kek-1/../x", KeyKind.KEK, False),
        (None, KeyKind.KEK, False),
        ("seed-1", KeyKind.SEED, True),
        ("seed-" + "a" * 28, KeyKind.SEED, True),
        ("seed-" + "a" * 29, KeyKind.SEED, False),
        ("pep-1", KeyKind.PEPPER, True),
        ("pep-x\n", KeyKind.PEPPER, False),
        # An id of one kind is never an id of another.
        ("seed-1", KeyKind.KEK, False),
        ("pep-1", KeyKind.KEK, False),
        ("kek-1", KeyKind.SEED, False),
        ("pep-1", KeyKind.SEED, False),
        ("kek-1", KeyKind.PEPPER, False),
        ("seed-1", KeyKind.PEPPER, False),
    ],
)
def test_the_key_id_patterns(value: object, kind: KeyKind, ok: bool) -> None:
    assert is_key_id(value, kind) is ok


def test_the_settings_use_the_same_id_patterns() -> None:
    assert KeyKind.KEK.id_pattern.pattern == CONNECTION_KEK_ID.pattern
    assert KeyKind.SEED.id_pattern.pattern == WEBHOOK_SEED_ID.pattern
    assert KeyKind.PEPPER.id_pattern.pattern == WEBHOOK_PEPPER_ID.pattern


@pytest.mark.parametrize("key", [Kek("kek-1", KEY_A), WebhookSeed("seed-1", KEY_A)])
def test_a_ring_key_hides_its_key_and_cannot_be_pickled_or_copied(key: RingKey) -> None:
    assert repr(key) == f"{type(key).__name__}(key_id={key.key_id!r}, key=...)"
    assert KEY_A.hex() not in repr(key)
    assert KEY_A.hex() not in str(key)
    with pytest.raises(TypeError):
        pickle.dumps(key)
    with pytest.raises(TypeError):
        copy.copy(key)
    with pytest.raises(TypeError):
        copy.deepcopy(key)
    assert "ciphertext" not in repr(sealed_for())


@pytest.mark.parametrize(
    ("build", "error", "match"),
    [
        (lambda: Kek("kek-1", b"short"), ValueError, "32 bytes"),
        (lambda: Kek("kek-1", bytearray(32)), ValueError, "32 bytes"),  # pyright: ignore[reportArgumentType]
        (lambda: Kek("bad", bytes(32)), ValueError, "does not match"),
        (lambda: Kek("seed-1", bytes(32)), ValueError, "does not match"),
        (lambda: WebhookSeed("kek-1", bytes(32)), ValueError, "does not match"),
        (lambda: WebhookPepper("seed-1", bytes(32)), ValueError, "does not match"),
        (lambda: RingKey("kek-1", bytes(32)), TypeError, "Kek, a WebhookSeed or"),
    ],
)
def test_a_ring_key_refuses_a_wrong_id_or_length(
    build: Callable[[], RingKey], error: type[Exception], match: str
) -> None:
    with pytest.raises(error, match=match):
        build()


@pytest.mark.parametrize("not_a_kek", [WebhookSeed("seed-1", KEY_A), WebhookPepper("pep-1", KEY_A)])
def test_only_a_kek_wraps_or_unwraps(not_a_kek: RingKey) -> None:
    """A seed or pepper with the KEK's very bytes still can't wrap: the kind is the type."""
    aad = connection_wrap_aad("kek-1", OWNER, "withings", ROW)
    with pytest.raises(TypeError, match="only a KEK"):
        wrap(bytes(32), not_a_kek, aad)  # pyright: ignore[reportArgumentType]
    with pytest.raises(TypeError, match="only a KEK"):
        seal(PAYLOAD, connection_aad(OWNER, "withings", ROW), not_a_kek, aad)  # pyright: ignore[reportArgumentType]
    sealed = sealed_for()
    with pytest.raises(TypeError, match="only a KEK"):
        unwrap(sealed.wrapped_dek, sealed.dek_nonce, not_a_kek, aad)  # pyright: ignore[reportArgumentType]


def test_wrap_refuses_a_data_key_of_the_wrong_size() -> None:
    aad = connection_wrap_aad("kek-1", OWNER, "withings", ROW)
    for dek in (b"", bytes(31), bytes(33)):
        with pytest.raises(ValueError, match="32 bytes"):
            wrap(dek, KEK, aad)


def test_check_values_differ_by_key_and_by_kind() -> None:
    values = {
        check_value(Kek("kek-1", KEY_A)),
        check_value(Kek("kek-2", KEY_B)),
        check_value(WebhookSeed("seed-1", KEY_A)),
        check_value(WebhookPepper("pep-1", KEY_A)),
    }
    assert len(values) == 4  # the same bytes as another kind give another check value
    assert all(len(value) == CHECK_VALUE_LENGTH for value in values)
    assert check_value(Kek("kek-1", KEY_A)) == check_value(Kek("kek-9", KEY_A))  # id-free


def test_canonical_json() -> None:
    assert canonical_json({"b": 1, "a": "ü"}) == '{"a":"ü","b":1}'.encode()
