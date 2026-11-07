"""How many ``Transient`` retries a Withings caller allows (PLAN §4.2; the taxonomy is
``errors.py``, which re-exports ``RetryBudget``): exponential backoff with jitter, bounded by a
maximum wait and an optional deadline. Pure: the caller passes the random source and the time."""

import random
from dataclasses import dataclass
from typing import Final

BACKOFF_BASE_SECONDS: Final = 1.0
JITTER: Final = 0.2  # +-20 %
# 2**16 seconds is already far past any max_delay. A larger exponent overflows a float.
MAX_BACKOFF_EXPONENT: Final = 16


@dataclass
class RetryBudget:
    """How many ``Transient`` retries a caller allows, shared by every call it is passed to.
    No single wait is longer than ``max_delay``, and none ends after ``deadline``
    (``time.monotonic()`` seconds) when one is set."""

    retries: int = 0
    max_delay: float = 60.0
    deadline: float | None = None
    used: int = 0

    def next_delay(self, rng: random.Random, now: float, hint: float | None = None) -> float | None:
        """The wait before the next retry, spending one; None when the budget is spent."""
        if self.retries <= 0:
            return None
        exponent = min(max(self.used, 0), MAX_BACKOFF_EXPONENT)
        delay = min(BACKOFF_BASE_SECONDS * 2**exponent, self.max_delay)
        delay *= 1 + rng.uniform(-JITTER, JITTER)
        if hint is not None and hint > 0:
            delay = max(delay, min(hint, self.max_delay))
        if self.deadline is not None and now + delay > self.deadline:
            return None
        self.retries -= 1
        self.used += 1
        return delay
