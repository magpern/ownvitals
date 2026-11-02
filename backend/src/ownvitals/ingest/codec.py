"""Canonical payloads, the ECG frame and the NULL-signal fingerprint (ADR 0006 section 1 and 9).

The uncompressed ECG payload is ``OVE1``, the lead count as uint16 LE, a reserved uint16 of 0,
the sample count of each lead as uint32 LE, then the samples as int32 LE, lead after lead, in
the order the API returned them. It is stored as one zstd frame at level 19 with a content
checksum. The frame is decoded and compared with the API arrays before it is returned. The
payload hash is SHA-256 of the uncompressed bytes, so it does not depend on the compressor.

A row kept with ``signal`` NULL has no payload hash. Its ``input_fingerprint`` is SHA-256 over
this UTF-8 text, with an empty hash field when the signal is absent::

    v1
    signalid:<decimal>
    payload_hash:<64 lowercase hex, or empty>
    sampling_hz:<decimal, or empty>

That encoding is fixed: a ``signal_missing`` estimate built from it stays current, so a
notification hold whose only linked recording is that row ends at once instead of waiting out
the 24-hour bound (ADR 0006 section 11).
"""

import hashlib
import json
import struct
from decimal import Decimal
from typing import Any

import zstandard

from ownvitals.ecg.pipeline import analyse, to_estimated

MAGIC = b"OVE1"
ZSTD_LEVEL = 19
MAX_SAMPLES = 1_000_000
MAX_RATE_HZ = 2_000
HOLD_BOUND_SECONDS = 86_400
OUT_OF_CONTRACT = "ecg_signal_out_of_contract"


class SignalRejected(Exception):
    """The signal is outside the ingest contract. ``args`` is the category only."""

    def __init__(self) -> None:
        super().__init__(OUT_OF_CONTRACT)
        self.category = OUT_OF_CONTRACT


def canonical(value: Any) -> Any:
    """A JSON-ready value. Decimals become their exact decimal text. Floats are refused."""
    if value is None or isinstance(value, str | int | bool):
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {str(key): canonical(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, list | tuple):
        return [canonical(item) for item in value]
    raise TypeError("a payload value must be JSON-shaped")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(canonical(value), sort_keys=True, separators=(",", ":")).encode()


def payload_hash(value: Any) -> bytes:
    """SHA-256 of the canonical JSON of a source payload."""
    return hashlib.sha256(canonical_bytes(value)).digest()


def fixed_point(value: Decimal) -> tuple[int, int]:
    """``(raw, unit)`` so ``raw * 10^unit`` is ``value``. Refuses a non-finite or huge number."""
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int):
        raise ValueError("not a finite number")
    if exponent < 0:
        raw, unit = int(value.scaleb(-exponent)), exponent
    else:
        raw, unit = int(value), 0
    if raw.bit_length() > 62:
        raise ValueError("wider than a bigint")
    return raw, unit


def _leads(leads: list[list[int]]) -> None:
    if not leads:
        raise SignalRejected
    for lead in leads:
        if not lead or len(lead) > MAX_SAMPLES:
            raise SignalRejected
        if any(not isinstance(sample, int) or isinstance(sample, bool) for sample in lead):
            raise SignalRejected
        if any(sample < -2_147_483_648 or sample > 2_147_483_647 for sample in lead):
            raise SignalRejected


def uncompressed(leads: list[list[int]]) -> bytes:
    """The version-1 payload. The arrays are copied as given; nothing is rescaled."""
    _leads(leads)
    counts = struct.pack("<" + "I" * len(leads), *(len(lead) for lead in leads))
    samples = b"".join(struct.pack("<" + "i" * len(lead), *lead) for lead in leads)
    return MAGIC + struct.pack("<HH", len(leads), 0) + counts + samples


def parse_uncompressed(payload: bytes) -> list[list[int]]:
    if len(payload) < 8 or payload[:4] != MAGIC:
        raise SignalRejected
    n_leads, reserved = struct.unpack_from("<HH", payload, 4)
    if reserved != 0 or n_leads < 1:
        raise SignalRejected
    header = 8 + 4 * n_leads
    if len(payload) < header:
        raise SignalRejected
    counts = struct.unpack_from("<" + "I" * n_leads, payload, 8)
    leads: list[list[int]] = []
    cursor = header
    for count in counts:
        if count < 1 or count > MAX_SAMPLES or cursor + 4 * count > len(payload):
            raise SignalRejected
        leads.append(list(struct.unpack_from("<" + "i" * count, payload, cursor)))
        cursor += 4 * count
    if cursor != len(payload):
        raise SignalRejected
    return leads


def frame_signal(leads: list[list[int]]) -> tuple[bytes, bytes]:
    """``(zstd frame, sha256 of the uncompressed payload)``. The frame is checked by decoding it."""
    plain = uncompressed(leads)
    frame = zstandard.ZstdCompressor(level=ZSTD_LEVEL, write_checksum=True).compress(plain)
    decoded = zstandard.ZstdDecompressor().decompress(frame)
    if decoded != plain or parse_uncompressed(decoded) != leads:
        raise SignalRejected
    return frame, hashlib.sha256(plain).digest()


def decode_signal(frame: bytes) -> list[list[int]]:
    return parse_uncompressed(zstandard.ZstdDecompressor().decompress(frame))


def check_rate(sampling_hz: int | None) -> int:
    if (
        not isinstance(sampling_hz, int)
        or isinstance(sampling_hz, bool)
        or sampling_hz < 1
        or sampling_hz > MAX_RATE_HZ
    ):
        raise SignalRejected
    return sampling_hz


def input_fingerprint(signalid: int, digest: bytes | None, sampling_hz: int | None) -> str:
    """Fingerprint of one recording. A NULL signal hashes an empty payload-hash field."""
    hashed = "" if digest is None else digest.hex()
    rate = "" if sampling_hz is None else str(sampling_hz)
    text = f"v1\nsignalid:{signalid}\npayload_hash:{hashed}\nsampling_hz:{rate}\n"
    return hashlib.sha256(text.encode()).hexdigest()


def missing_estimate(signalid: int, sampling_hz: int | None, revision: int) -> dict[str, Any]:
    """The ``signal_missing`` estimate. It is current for a NULL signal with this fingerprint."""
    result = analyse(None, sampling_hz)
    fingerprint = input_fingerprint(signalid, None, sampling_hz)
    return to_estimated(result, input_fingerprint=fingerprint, input_revision=revision)


def estimate_current(
    estimated: dict[str, Any] | None,
    *,
    signalid: int,
    digest: bytes | None,
    sampling_hz: int | None,
) -> bool:
    if not estimated:
        return False
    return estimated.get("input_fingerprint") == input_fingerprint(signalid, digest, sampling_hz)


def hold_pending(
    *,
    estimates_current: bool,
    another_group_may_link: bool,
    elapsed_seconds: float,
    bound_seconds: float = HOLD_BOUND_SECONDS,
) -> bool:
    """Whether an E4/E6 notification is still held (ADR 0006 section 11).

    It ends at once when every linked recording has a current estimate and no other trigger
    group can still be linked, including when that estimate is ``signal_missing`` and so has
    no gate result. The 24-hour bound still ends a hold that is waiting for something else.
    """
    if elapsed_seconds >= bound_seconds:
        return False
    return another_group_may_link or not estimates_current
