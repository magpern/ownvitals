"""Loads the HIS band table (ADR 0012 §2): the one file read of the scores pass.

The bytes go to ``analysis.scores.his_table.parse_table`` (pure), which checks the registered
SHA-256. A missing, unreadable or invalid file is not an error of the pass: HIS is ``not_set``,
with a short code (never the file's content) in ``TableState.error``.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from ownvitals.analysis.scores.his_table import BandTable, InvalidBandTable, parse_table

MAX_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class TableState:
    table: BandTable | None
    error: str | None
    digest: str  # SHA-256 of the file read, "" when there is none (part of the fingerprint)


def load_table(path: Path | None) -> TableState:
    if path is None:
        return TableState(None, None, "")
    try:
        raw = path.read_bytes()[: MAX_BYTES + 1]
    except OSError:
        return TableState(None, "unreadable", "")
    digest = hashlib.sha256(raw).hexdigest()
    if len(raw) > MAX_BYTES:
        return TableState(None, "too_large", digest)
    try:
        return TableState(parse_table(raw), None, digest)
    except InvalidBandTable as exc:
        return TableState(None, str(exc), digest)
