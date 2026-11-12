"""``windowtools.clear_of_window``: it waits past a window boundary it is too close to (#486).
A fake clock and sleep stand in for the database; synthetic."""

import pytest

from windowtools import MARGIN_SECONDS, NEEDED_SECONDS, clear_of_window

WINDOW = 600
NEEDS = NEEDED_SECONDS + MARGIN_SECONDS


class FakeTime:
    def __init__(self, now: float) -> None:
        self.now = now
        self.sleeps: list[float] = []

    async def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


async def run(start: float, window: int = WINDOW) -> FakeTime:
    fake = FakeTime(start)
    await clear_of_window(window, clock=fake.clock, sleep=fake.sleep)
    return fake


async def test_far_from_a_boundary_it_does_not_wait() -> None:
    fake = await run(1_000 * WINDOW + 100.0)
    assert fake.sleeps == []


async def test_just_before_a_boundary_it_waits_past_it() -> None:
    boundary = 1_001 * WINDOW
    fake = await run(boundary - 1.5)
    assert len(fake.sleeps) == 1
    assert fake.now > boundary
    assert (WINDOW - fake.now % WINDOW) > NEEDS  # the whole test now fits in the window


@pytest.mark.parametrize("left", [NEEDS - 0.01, NEEDS / 2, 0.001])
async def test_inside_the_needed_span_it_waits(left: float) -> None:
    boundary = 1_001 * WINDOW
    fake = await run(boundary - left)
    assert fake.sleeps
    assert (WINDOW - fake.now % WINDOW) > NEEDS


async def test_exactly_on_a_boundary_it_does_not_wait() -> None:
    assert (await run(1_000 * WINDOW)).sleeps == []


async def test_a_window_too_short_for_the_test_is_refused() -> None:
    with pytest.raises(ValueError, match="too short"):
        await run(0.0, window=60)


async def test_the_default_clock_is_the_database_clock(database_url: str) -> None:
    from windowtools import database_epoch

    before = await database_epoch()
    assert abs(before - (await database_epoch())) < 5
