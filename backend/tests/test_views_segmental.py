"""Segmental muscle mass tiles of ``GET /api/metrics`` (issue #324): the latest reading of each
body position, read as the signed-in user under forced RLS. Synthetic data only; every value and
identifier is invented.
"""

from datetime import timedelta
from typing import Any

from authtools import Env, auth_schema_fixture, clean_auth_fixture  # noqa: F401 - fixtures
from flows import Browser
from viewtools import (
    TODAY,
    connected_with_streams,
    env_fixture,  # noqa: F401 - fixture
    epoch,
    group,
    ingest,
    run,
    user,
)

SYNTHETIC = True
KEY = "segmental_muscle_mass"
LEFT_ARM, RIGHT_ARM, LEFT_LEG = 3, 2, 10  # withings-findings section 2 positions


async def segment_tiles(browser: Browser) -> dict[str, dict[str, Any]]:
    response = await browser.get("/api/metrics")
    assert response.status_code == 200, response.text
    return {m["key"]: m for m in response.json()["metrics"] if m["key"].startswith(KEY)}


def reading(grpid: int, days_ago: int, *measures: tuple[int, int, int, int | None]) -> Any:
    return group(grpid, epoch(TODAY - timedelta(days=days_ago)), *measures)


async def test_latest_segmental_value_per_position_as_a_tile(env: Env) -> None:
    browser, owner = await user(env, "a@example.com")
    link = connected_with_streams(owner)
    older = reading(5_000_001, 9, (175, 2500, -3, LEFT_ARM), (175, 2600, -3, RIGHT_ARM))
    newer = reading(5_000_002, 2, (175, 2700, -3, LEFT_ARM), (175, 12_000, -3, LEFT_LEG))
    fat = reading(5_000_003, 1, (174, 9_999, -3, LEFT_ARM))  # another segmental type: not listed
    await ingest(env, owner, link, extra=[older, newer, fat])
    tiles = await segment_tiles(browser)
    assert sorted(tiles) == [f"{KEY}:left_arm", f"{KEY}:left_leg", f"{KEY}:right_arm"]
    arm = tiles[f"{KEY}:left_arm"]
    assert (arm["area"], arm["unit"], arm["stream"], arm["state"]) == (
        "body",
        "kg",
        "measures",
        "up_to_date",
    )
    assert arm["latest"]["value"] == 2.7  # the newer reading, not the older 2.5
    assert arm["latest"]["day"] == (TODAY - timedelta(days=2)).isoformat()
    assert tiles[f"{KEY}:right_arm"]["latest"]["value"] == 2.6
    assert tiles[f"{KEY}:left_leg"]["latest"]["value"] == 12.0
    assert arm["sparkline"] == []
    assert arm["typical_range"] is None
    assert "5000001" not in str(tiles)  # no Withings group id


async def test_a_user_never_sees_another_users_segmental_values(env: Env) -> None:
    a, owner_a = await user(env, "a@example.com")
    b, owner_b = await user(env, "b@example.com")
    await ingest(
        env,
        owner_a,
        connected_with_streams(owner_a),
        extra=[reading(5_100_001, 1, (175, 3100, -3, LEFT_ARM))],
    )
    await ingest(
        env,
        owner_b,
        connected_with_streams(owner_b, withings_userid=424_242_425),
        extra=[reading(5_100_002, 1, (175, 4200, -3, RIGHT_ARM))],
    )
    tiles_a, tiles_b = await segment_tiles(a), await segment_tiles(b)
    assert sorted(tiles_a) == [f"{KEY}:left_arm"]
    assert tiles_a[f"{KEY}:left_arm"]["latest"]["value"] == 3.1
    assert sorted(tiles_b) == [f"{KEY}:right_arm"]
    assert tiles_b[f"{KEY}:right_arm"]["latest"]["value"] == 4.2


async def test_no_segmental_reading_lists_no_tile_and_a_blocked_stream_says_why(env: Env) -> None:
    browser, owner = await user(env, "a@example.com")
    connected_with_streams(owner)
    assert await segment_tiles(browser) == {}  # the page reads an unlisted position as unavailable
    other, owner2 = await user(env, "b@example.com")
    connected_with_streams(
        owner2, withings_userid=424_242_425, measures={"capability": "unsupported"}
    )
    tiles = await segment_tiles(other)
    assert sorted(tiles) == [
        f"{KEY}:{p}" for p in ("left_arm", "left_leg", "right_arm", "right_leg")
    ]
    assert {t["state"] for t in tiles.values()} == {"not_supported"}
    assert all(t["latest"] is None for t in tiles.values())


async def test_an_excluded_reading_is_not_the_latest(env: Env) -> None:
    browser, owner = await user(env, "a@example.com")
    link = connected_with_streams(owner)
    await ingest(
        env,
        owner,
        link,
        extra=[
            reading(5_200_001, 3, (175, 2800, -3, LEFT_ARM)),
            reading(5_200_002, 1, (175, 9100, -3, LEFT_ARM)),
        ],
    )
    run(
        "UPDATE observations SET quality_flags = ARRAY['implausible'] WHERE owner_id = :o "
        "AND metric_key = 'segmental_muscle_mass:left_arm' AND value > 9",
        o=owner,
    )
    tiles = await segment_tiles(browser)
    assert tiles[f"{KEY}:left_arm"]["latest"]["value"] == 2.8
