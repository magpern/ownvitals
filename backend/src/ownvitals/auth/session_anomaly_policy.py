"""Whether a request looks like scripted use of a stolen session (#461, ADR 0017). Pure.

A request is scored against what its session has seen so far (``History``: the address and
``User-Agent`` the session was issued to, the country of its first request when a geo source
exists, the request counter of its current window, and whether it was flagged already). Three
signals, each a code:

- ``new_ip_and_agent``: the address is on another network than the one the session was issued
  to (``clientip.same_network``: the same IPv4 address, or the same IPv6 /64) **and** the
  browser or operating system family differs (``useragent.describe``). One of the two alone is
  ordinary (a phone moving between networks, a browser update) and is no signal. An unknown
  address or agent on either side is never a change.
- ``request_rate``: more than ``RATE_LIMIT`` requests in one fixed ``RATE_WINDOW`` of the
  session. A person clicking through the app, whose pages each make a handful of calls, stays
  far below it; a script replaying the cookie does not.
- ``country_change``: the country differs from the one the session first came from. Only with
  a geo source (``geo_source = cloudflare``); with the default ``none`` every country is
  unknown and this signal never fires. ``XX`` (Cloudflare's "unknown") is unknown too.

It is only a signal. ``assess`` never signs anyone out: the caller records one
``session_anomaly`` event per session, the first time any signal fires (``record``), and
nothing else happens. Nothing here reads a clock, the network or the database: the time and
every value are passed in.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from ownvitals.auth.clientip import same_network
from ownvitals.auth.useragent import describe

KIND: Final = "session_anomaly"
NEW_IP_AND_AGENT: Final = "new_ip_and_agent"
REQUEST_RATE: Final = "request_rate"
COUNTRY_CHANGE: Final = "country_change"
SIGNALS: Final = (NEW_IP_AND_AGENT, REQUEST_RATE, COUNTRY_CHANGE)

RATE_WINDOW: Final = timedelta(seconds=60)
RATE_LIMIT: Final = 300  # requests of one session in one window; above it is a signal
AGENT_LIMIT: Final = 200  # ``sessions.USER_AGENT_LIMIT``: the stored agent is cut there
UNKNOWN_COUNTRIES: Final = frozenset({"XX"})


@dataclass(frozen=True, slots=True)
class History:
    """What the session has seen before this request."""

    ip: str | None  # the address the session was issued to
    user_agent: str | None  # the agent it was issued to (at most ``AGENT_LIMIT`` characters)
    country: str | None  # the country of its first request with one, or None
    window_at: datetime | None  # when its current counting window began
    window_count: int  # requests counted in that window
    flagged: bool  # a ``session_anomaly`` event was recorded for it already


@dataclass(frozen=True, slots=True)
class Request:
    """This request."""

    ip: str | None
    user_agent: str | None
    country: str | None  # ISO 3166-1 alpha-2 from the geo source, or None
    now: datetime


@dataclass(frozen=True, slots=True)
class Verdict:
    signals: tuple[str, ...]  # in ``SIGNALS`` order; empty for an ordinary request
    record: bool  # write the one event now: a signal fired and the session was not flagged yet
    window_at: datetime  # the session's counting window after this request
    window_count: int
    country: str | None  # the session's country after this request


def _known(country: str | None) -> str | None:
    return None if country is None or country in UNKNOWN_COUNTRIES else country


def agent_family(user_agent: str | None) -> tuple[str, str] | None:
    """``(browser, os)`` of the agent as the session stores it, or None when there is none."""
    if not user_agent:
        return None
    return describe(user_agent[:AGENT_LIMIT])


def new_ip_and_agent(history: History, request: Request) -> bool:
    if history.ip is None or request.ip is None:
        return False
    before, now = agent_family(history.user_agent), agent_family(request.user_agent)
    if before is None or now is None:
        return False
    return not same_network(history.ip, request.ip) and before != now


def next_window(history: History, now: datetime) -> tuple[datetime, int]:
    """The counting window after this request: a new one of 1 once ``RATE_WINDOW`` has passed
    (or the clock went back), else the same one counted up."""
    start = history.window_at
    if start is None or now < start or now - start >= RATE_WINDOW:
        return now, 1
    return start, history.window_count + 1


def request_rate(window_count: int) -> bool:
    return window_count > RATE_LIMIT


def country_change(history: History, request: Request) -> bool:
    before, now = _known(history.country), _known(request.country)
    return before is not None and now is not None and before != now


def assess(history: History, request: Request) -> Verdict:
    window_at, window_count = next_window(history, request.now)
    fired = {
        NEW_IP_AND_AGENT: new_ip_and_agent(history, request),
        REQUEST_RATE: request_rate(window_count),
        COUNTRY_CHANGE: country_change(history, request),
    }
    signals = tuple(code for code in SIGNALS if fired[code])
    return Verdict(
        signals=signals,
        record=bool(signals) and not history.flagged,
        window_at=window_at,
        window_count=window_count,
        country=_known(history.country) or _known(request.country),
    )
