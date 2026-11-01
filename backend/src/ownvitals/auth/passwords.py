"""Password hashing: argon2id in a bounded thread pool (ADR 0003 sections 5 and 9)."""

import asyncio
import unicodedata
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from argon2 import PasswordHasher as Argon2
from argon2.exceptions import InvalidHashError, VerificationError

# The OWASP minimum: memory 19 MiB, time 2, parallelism 1.
MEMORY_COST_KIB = 19 * 1024
TIME_COST = 2
PARALLELISM = 1
MIN_LENGTH, MAX_LENGTH = 12, 128
# A request that has waited this long for a free worker gets 503.
WAIT_SECONDS = 5.0


class PasswordHashBusy(Exception):
    """No hashing worker became free in time: the response is 503."""


class PasswordRuleError(ValueError):
    """The password breaks the length rule."""


def normalize_password(password: str) -> str:
    """NFKC, then the length rule (12 to 128 characters); no other composition rules."""
    normalized = unicodedata.normalize("NFKC", password)
    if not MIN_LENGTH <= len(normalized) <= MAX_LENGTH:
        raise PasswordRuleError(f"a password must have {MIN_LENGTH} to {MAX_LENGTH} characters")
    return normalized


class PasswordHasher:
    """Hash and verify without letting a burst of requests exhaust CPU or memory.

    At most ``workers`` hashes run at once; a caller that waits more than ``wait_seconds`` for
    a free worker gets ``PasswordHashBusy``.
    """

    def __init__(self, workers: int, wait_seconds: float = WAIT_SECONDS) -> None:
        self._argon2 = Argon2(
            time_cost=TIME_COST, memory_cost=MEMORY_COST_KIB, parallelism=PARALLELISM
        )
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="argon2")
        self._slots = asyncio.Semaphore(workers)
        self._wait = wait_seconds
        # Verified when a sign-in names an unknown email or a user without a password, so the
        # timing matches a real verification.
        self._dummy_hash = self._argon2.hash("ownvitals-timing-dummy")

    async def _run[T](self, function: Callable[[], T]) -> T:
        try:
            async with asyncio.timeout(self._wait):
                await self._slots.acquire()
        except TimeoutError:
            raise PasswordHashBusy from None
        try:
            return await asyncio.get_running_loop().run_in_executor(self._pool, function)
        finally:
            self._slots.release()

    async def hash(self, password: str) -> str:
        return await self._run(lambda: self._argon2.hash(password))

    async def verify(self, hashed: str, password: str) -> bool:
        def check() -> bool:
            try:
                return self._argon2.verify(hashed, password)
            except (VerificationError, InvalidHashError):
                return False

        return await self._run(check)

    async def verify_dummy(self, password: str) -> None:
        """Run one verification against a fixed hash and discard the result."""
        await self.verify(self._dummy_hash, password)

    def needs_rehash(self, hashed: str) -> bool:
        """True when ``hashed`` was made with other parameters than the current ones."""
        return self._argon2.check_needs_rehash(hashed)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
