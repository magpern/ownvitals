"""A band-table id is pinned to one SHA-256 (#501 item 1). Synthetic: no data involved."""

import hashlib

from ownvitals.analysis.scores.his_table import REGISTERED
from ownvitals.config_scores import DEFAULT_HIS_BAND_TABLE

# Frozen on purpose. Editing a digest here is the mistake this test exists to stop: the scores
# API (views/scores_routes.py) reports table_sha from REGISTERED by the id a stored row names,
# so a changed table needs a NEW id, and this map gains an entry; it never changes one.
FROZEN = {
    "withings_card_v1": "9f20d86e398e9b9357cf8fd7b7acbcd7b2fa92c6459ad694a24369df441212cb",
}


def test_a_registered_id_keeps_its_digest() -> None:
    """Mutation caught: changing a digest in REGISTERED (or the table file, with REGISTERED
    updated to match) without minting a new id."""
    for table_id, sha in FROZEN.items():
        assert REGISTERED.get(table_id) == sha, f"{table_id} changed digest; use a new id"


def test_no_registered_id_is_missing_from_the_frozen_map() -> None:
    """Mutation caught: registering a new id without freezing its digest here."""
    assert set(REGISTERED) == set(FROZEN)


def test_the_shipped_file_matches_its_frozen_digest() -> None:
    """Mutation caught: editing the shipped table file while leaving the old id."""
    raw = DEFAULT_HIS_BAND_TABLE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == FROZEN["withings_card_v1"]
