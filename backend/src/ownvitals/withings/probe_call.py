"""How the probe (``probe.py``) calls Withings: one rate-limiter slot per call, one token
refresh for the whole probe, and every answer classified into the report as a fixed category."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final, Protocol

from ownvitals.withings.capabilities import (
    NOT_AUTHORIZED,
    UNKNOWN,
    after_success,
    from_permanent,
    from_scopes,
)
from ownvitals.withings.errors import (
    PAGINATION,
    Permanent,
    RateLimited,
    RetryBudget,
    TokenInvalid,
    Transient,
)
from ownvitals.withings.probe_report import Report
from ownvitals.withings.streams import stream_named

SKIPPED_SCOPE: Final = "skipped (no scope)"


# ----------------------------------------------------------------------------------- calling


class Stopped(Exception):
    """The probe stops: ``args == (reason,)``, a fixed category."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Limiter(Protocol):
    """One rate-limiter slot per call; ``pause`` is the 601 hold."""

    def slot(self) -> Any: ...  # an async context manager

    async def pause(self, wait_seconds: float) -> None: ...


class Tokens(Protocol):
    access: str
    state: str

    async def renew(self) -> bool:
        """A new access token after a refusal, once. False: the probe stops."""
        ...


@dataclass(frozen=True, slots=True)
class _Classified:
    capability: str


_FAILED: Final = object()


def _permanent(error: Permanent) -> str:
    parts = ["permanent", error.category]
    if error.api_status is not None:
        parts.append(f"api_status {int(error.api_status)}")
    if error.http_status is not None and error.http_status != 200:
        parts.append(f"http {int(error.http_status)}")
    return " ".join(parts)


class _Caller:
    def __init__(
        self, report: Report, tokens: Tokens, limiter: Limiter, budget: RetryBudget
    ) -> None:
        self.report = report
        self.tokens = tokens
        self.limiter = limiter
        self.budget = budget
        self.renewed = False  # one refresh for the whole probe

    def _stop(self, label: str, reason: str) -> object:
        self.report.calls[label] = reason
        self.report.stopped = reason
        return _FAILED

    async def __call__(self, label: str, fn: Callable[[str], Awaitable[Any]]) -> Any:
        """The answer, a ``_Classified`` capability, or ``_FAILED`` (recorded in the report)."""
        if self.report.stopped is not None:
            return _FAILED
        while True:
            try:
                async with self.limiter.slot():
                    result = await fn(self.tokens.access)
            except TokenInvalid:
                if not self.renewed:
                    self.renewed = True
                    if await self.tokens.renew():
                        continue
                self.report.grant = self.tokens.state
                return self._stop(label, "token_invalid")
            except RateLimited as exc:
                await self.limiter.pause(exc.wait_seconds)
                return self._stop(label, "rate_limited")
            except Stopped as exc:
                return self._stop(label, exc.reason)
            except Transient as exc:
                self.report.calls[label] = f"transient {exc.category}"
                return _FAILED
            except Permanent as exc:
                self.report.calls[label] = _permanent(exc)
                found = from_permanent(exc)
                return _FAILED if found is None else _Classified(found)
            self.report.grant = self.tokens.state
            self.report.calls.setdefault(label, "ok")
            return result


async def _pages(
    call: _Caller, label: str, fetch: Callable[[str, int | None], Awaitable[Any]], cap: int
) -> tuple[list[Any], Any, bool]:
    """Offset pages up to ``cap``: ``(pages, first failure or None, truncated)``."""
    pages: list[Any] = []
    offset: int | None = None
    for _ in range(cap):
        page = await call(label, lambda token, at=offset: fetch(token, at))
        if page is _FAILED or isinstance(page, _Classified):
            return pages, page, False
        pages.append(page)
        if not page.more:
            return pages, None, False
        if page.offset <= (offset or 0):
            call.report.calls[label] = f"permanent {PAGINATION}"
            return pages, None, False
        offset = page.offset
    return pages, None, True


def _capability(stream: str, got: Any, *, empty: bool, devices_empty: bool) -> str:
    if isinstance(got, _Classified):
        return got.capability
    if got is _FAILED:
        return UNKNOWN
    return after_success(stream=stream, empty=empty, devices_known_empty=devices_empty)


def _allowed(report: Report, stream: str, label: str, scopes: tuple[str, ...]) -> bool:
    if from_scopes(stream_named(stream), scopes) == NOT_AUTHORIZED:
        report.capabilities[stream] = NOT_AUTHORIZED
        report.calls.setdefault(label, SKIPPED_SCOPE)
        return False
    return True
