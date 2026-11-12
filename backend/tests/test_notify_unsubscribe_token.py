"""The signed one-click unsubscribe token (ADR 0019; #459). Pure: no database, no clock.

- a token verifies to exactly the owner, kind and generation it was issued for, under the key
  derived for this purpose only (``test_a_token_round_trips*``, ``test_the_key_*``);
- tampered, expired, another user's, another kind's, another key's and malformed tokens are all
  refused, the same way (``None``) whatever the reason (``test_*_is_refused``); a replay after a
  change is the database's refusal (``test_notify_email_prefs_db.py``);
- only a suppressible kind gets a token, and the link is the endpoint's fixed path
  (``test_issue_*``, ``test_the_link_*``).

Synthetic owners and keys only.
"""

import base64
import hmac
import struct
import uuid
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from ownvitals.auth.keys import Keys, derive_key
from ownvitals.notify import unsubscribe
from ownvitals.notify.email_policy import ALWAYS_SENT_KINDS, SUPPRESSIBLE_KINDS
from ownvitals.notify.outbox import OWNER_NOTICE_KINDS

SECRET = b"synthetic-app-secret-for-the-token-tests-0123456789"
KEY = unsubscribe.unsubscribe_key(SECRET)
ALICE = uuid.UUID("00000000-0000-4000-8000-0000000a11ce")
BOB = uuid.UUID("00000000-0000-4000-8000-000000000b0b")
NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)


def _token(owner: uuid.UUID = ALICE, kind: str = "findings_waiting", generation: int = 0) -> str:
    return unsubscribe.issue(KEY, owner, kind, generation, NOW)


def _raw(token: str) -> bytearray:
    return bytearray(base64.urlsafe_b64decode(token + "="))


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _signed(payload: bytes, key: bytes = KEY) -> str:
    """A token over ``payload`` with a valid MAC: what only the key holder can make."""
    return _encode(payload + hmac.new(key, unsubscribe.DOMAIN + payload, sha256).digest()[:16])


@pytest.mark.parametrize("kind", SUPPRESSIBLE_KINDS)
@pytest.mark.parametrize("generation", [0, 1, 7, unsubscribe.MAX_GENERATION])
def test_a_token_round_trips_to_its_owner_kind_and_generation(kind: str, generation: int) -> None:
    token = _token(ALICE, kind, generation)
    assert len(token) == unsubscribe.TOKEN_CHARS
    assert unsubscribe.TOKEN_PATTERN.fullmatch(token)
    claim = unsubscribe.verify(KEY, token, NOW)
    assert claim is not None
    assert (claim.owner_id, claim.kind, claim.generation) == (ALICE, kind, generation)
    assert repr(claim) == "<unsubscribe claim>"  # never the owner in a log line


def test_a_token_is_valid_until_its_expiry_and_not_after() -> None:
    token = _token()
    assert unsubscribe.verify(KEY, token, NOW + unsubscribe.LIFETIME - timedelta(seconds=1))
    assert unsubscribe.verify(KEY, token, NOW + unsubscribe.LIFETIME) is None
    assert unsubscribe.verify(KEY, token, NOW + timedelta(days=365)) is None


def test_the_key_is_derived_for_this_purpose_only() -> None:
    assert unsubscribe.LABEL == "unsubscribe-v1"
    assert derive_key(SECRET, "unsubscribe-v1") == KEY
    keys = Keys.from_secret(SECRET)
    others = {keys.csrf, keys.rate_limit, keys.device, keys.recovery_codes, keys.totp_pending}
    assert KEY not in others
    assert derive_key(SECRET, "ai-audit") != KEY


def test_a_token_under_another_key_is_refused() -> None:
    other = unsubscribe.unsubscribe_key(b"another-synthetic-app-secret-0123456789abcdef")
    assert unsubscribe.verify(other, _token(), NOW) is None
    csrf = Keys.from_secret(SECRET).csrf  # a key of another purpose from the same secret
    assert unsubscribe.verify(csrf, _token(), NOW) is None


@pytest.mark.parametrize("position", range(unsubscribe.TOKEN_BYTES))
def test_a_tampered_token_is_refused(position: int) -> None:
    """Any flipped bit, in the owner, the kind, the generation, the expiry or the MAC."""
    raw = _raw(_token())
    raw[position] ^= 0x01
    assert unsubscribe.verify(KEY, _encode(bytes(raw)), NOW) is None


def test_another_user_s_token_is_refused_for_this_user() -> None:
    """The owner is bound: Bob's bytes in Alice's token break the MAC, and Bob's own token says
    Bob."""
    raw = _raw(_token(ALICE))
    raw[1:17] = BOB.bytes
    assert unsubscribe.verify(KEY, _encode(bytes(raw)), NOW) is None
    claim = unsubscribe.verify(KEY, _token(BOB), NOW)
    assert claim is not None
    assert claim.owner_id == BOB


def test_another_kind_is_refused() -> None:
    """The kind is bound: another kind's index in the token breaks the MAC."""
    raw = _raw(_token(kind="findings_waiting"))
    raw[17] = SUPPRESSIBLE_KINDS.index("ai_digest_ready")
    assert unsubscribe.verify(KEY, _encode(bytes(raw)), NOW) is None


def test_another_generation_is_refused() -> None:
    raw = _raw(_token(generation=1))
    raw[18:26] = struct.pack(">Q", 0)
    assert unsubscribe.verify(KEY, _encode(bytes(raw)), NOW) is None


def test_a_signed_token_of_an_unknown_version_kind_or_generation_is_refused() -> None:
    """Even with a valid MAC (only the key holder could make one): a later version, a kind index
    outside the list, a generation beyond the column's range."""
    expires = int((NOW + timedelta(days=1)).timestamp())
    pack = struct.Struct(">B16sBQQ").pack
    assert unsubscribe.verify(KEY, _signed(pack(1, ALICE.bytes, 0, 0, expires)), NOW) is not None
    assert unsubscribe.verify(KEY, _signed(pack(2, ALICE.bytes, 0, 0, expires)), NOW) is None
    unknown = len(SUPPRESSIBLE_KINDS)
    assert unsubscribe.verify(KEY, _signed(pack(1, ALICE.bytes, unknown, 0, expires)), NOW) is None
    beyond = unsubscribe.MAX_GENERATION + 1
    assert unsubscribe.verify(KEY, _signed(pack(1, ALICE.bytes, 0, beyond, expires)), NOW) is None


@pytest.mark.parametrize(
    "token",
    [
        None,
        "",
        b"bytes",
        12,
        "A" * 66,
        "A" * 68,
        "A" * 67,
        "=" * 67,
        "%" + "A" * 66,
        " " + _token()[1:],
        _token()[:-1] + "=",
        _token() + "\n",
    ],
)
def test_a_malformed_token_is_refused(token: object) -> None:
    assert unsubscribe.verify(KEY, token, NOW) is None


@pytest.mark.parametrize("kind", [*ALWAYS_SENT_KINDS, *OWNER_NOTICE_KINDS, "unknown_kind"])
def test_issue_refuses_a_kind_that_cannot_be_switched_off(kind: str) -> None:
    with pytest.raises(ValueError, match="not a suppressible"):
        unsubscribe.issue(KEY, ALICE, kind, 0, NOW)


@pytest.mark.parametrize("generation", [-1, unsubscribe.MAX_GENERATION + 1])
def test_issue_refuses_a_generation_out_of_range(generation: int) -> None:
    with pytest.raises(ValueError, match="generation"):
        unsubscribe.issue(KEY, ALICE, "findings_waiting", generation, NOW)


def test_the_link_is_the_endpoint_under_the_public_base_url() -> None:
    token = _token()
    expected = f"https://ownvitals.example.test/api/unsubscribe?t={token}"
    assert unsubscribe.link("https://ownvitals.example.test/", token) == expected
    for bad in ("", "x", token + "&next=https://evil.example", "<script>"):
        with pytest.raises(ValueError, match="not an unsubscribe token"):
            unsubscribe.link("https://ownvitals.example.test", bad)


def test_the_token_names_no_owner_in_clear() -> None:
    """The owner is in the token's bytes, not as its id's text."""
    token = _token()
    assert str(ALICE) not in token
    assert ALICE.hex not in token
