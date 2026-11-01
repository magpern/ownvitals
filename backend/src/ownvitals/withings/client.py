"""The public Withings API client (PLAN §4.2). Worker only, no database; ``docs/dev/withings.md``.

One ``WithingsClient`` per Withings **app** (``WithingsApp``: id, secret and base URL, set per app
and never per connection, PLAN §4.1); each call takes the connection's access token. The HTTP
client is ``egress.withings_async_client`` (worker only, the Withings hosts only, no redirects, a
total deadline). Every call is a form-encoded ``POST`` with the token in the ``Authorization``
header, never in the URL; every answer's JSON ``status`` is checked (errors come back as HTTP
200) by ``errors.interpret``, and the body is capped at ``MAX_RESPONSE_BYTES``. Only transport
failures (``egress.TRANSPORT_ERRORS``) are classified; anything else propagates. ``Transient``
is retried only within the caller's ``RetryBudget``; ``refresh`` never retries (PLAN §4.3 step 5).
Logs carry the endpoint key, the category and the numbers: never a token, a nonce, a signature,
a callback URL, a measurement or the provider's text.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import random
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any, Final, TypeVar
from urllib.parse import urlsplit

from ownvitals import egress
from ownvitals.egress import TIMEOUT_ERRORS, TRANSPORT_ERRORS
from ownvitals.withings import models as m
from ownvitals.withings.errors import (
    NETWORK,
    TIMEOUT,
    TOO_LARGE,
    UNEXPECTED_BODY,
    Permanent,
    RetryBudget,
    Transient,
    WithingsError,
    interpret,
    paginate,
)

if TYPE_CHECKING:  # annotations only
    import httpx

log = logging.getLogger("ownvitals.withings")

DEFAULT_BASE_URL: Final = f"https://{egress.WITHINGS_API_HOST}"
MAX_RESPONSE_BYTES: Final = 32 * 1024 * 1024
_TOKEN = re.compile(r"[\x21-\x7e]+")  # printable ASCII, no space
_FIELD = re.compile(r"[a-z0-9_]+")

M = TypeVar("M", bound=m.WithingsModel)


@dataclass(frozen=True)
class WithingsApp:
    """A Withings developer app (PLAN §4.1). ``client_secret`` never shows in a ``repr``."""

    client_id: str
    client_secret: str = field(repr=False)
    base_url: str = DEFAULT_BASE_URL

    def __post_init__(self) -> None:
        parts = urlsplit(self.base_url)
        if (
            parts.scheme != "https"
            or parts.hostname not in egress.WITHINGS_HOSTS
            or parts.port not in (None, egress.HTTPS_PORT)
            or parts.username is not None
            or parts.path not in ("", "/")
            or parts.query
            or parts.fragment
        ):
            raise ValueError("base_url must be https://<a Withings API host> with no path")
        if not self.client_id or not self.client_secret:
            raise ValueError("client_id and client_secret are required")


def _int(value: int) -> str:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("an integer parameter is not an integer")
    return str(value)


def _params(**values: object) -> dict[str, str]:
    """The form fields that are set: integers, ``date`` as YYYY-MM-DD, field and type lists
    comma-joined, strings as they are."""
    form: dict[str, str] = {}
    for name, value in values.items():
        if value is None:
            continue
        if isinstance(value, str):
            form[name] = value
        elif isinstance(value, date):
            form[name] = value.isoformat()
        elif isinstance(value, int):
            form[name] = _int(value)
        elif isinstance(value, Iterable):
            items = [str(v) if isinstance(v, str) else _int(v) for v in value]  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
            if name == "data_fields" and not all(_FIELD.fullmatch(i) for i in items):
                raise ValueError("a data field name is malformed")
            form[name] = ",".join(items)
        else:
            raise TypeError(f"unsupported value for {name}")
    return form


def signature(secret: str, *values: str) -> str:
    """Withings' signature of a signed call: HMAC-SHA256 with the client secret over the values
    of ``action``, ``client_id`` and ``nonce`` (or ``timestamp``) in key order, comma-joined."""
    return hmac.new(secret.encode(), ",".join(values).encode(), hashlib.sha256).hexdigest()


class WithingsClient:
    """The endpoints of PLAN §4.2 for one Withings app. Build it with ``open``."""

    def __init__(
        self,
        app: WithingsApp,
        http: httpx.AsyncClient,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.app = app
        self._http = http
        self._sleep = sleep
        self._rng = rng or random.Random()  # noqa: S311 - jitter, not a secret
        self._clock = clock
        self._monotonic = monotonic

    @classmethod
    @asynccontextmanager
    async def open(
        cls,
        app: WithingsApp,
        *,
        inner: httpx.AsyncBaseTransport | None = None,
        total_seconds: float = egress.WITHINGS_TOTAL_SECONDS,
        **options: Any,
    ) -> AsyncIterator[WithingsClient]:
        """A client over ``egress.withings_async_client`` (``inner`` replaces the network in
        tests; every egress check still runs)."""
        async with egress.withings_async_client(total_seconds=total_seconds, inner=inner) as http:
            yield cls(app, http, **options)

    # ------------------------------------------------------------------------------ core

    async def _send(
        self, endpoint: str, path: str, form: dict[str, str], token: str | None
    ) -> tuple[int, bytes | None, float | None]:
        """One POST: ``(http status, body or None when too large, Retry-After seconds)``."""
        headers = {"Accept": "application/json"}
        if token is not None:
            if not isinstance(token, str) or not _TOKEN.fullmatch(token):
                raise ValueError("the access token is malformed")
            headers["Authorization"] = f"Bearer {token}"
        url = self.app.base_url.rstrip("/") + path
        failure: WithingsError | None = None
        result: tuple[int, bytes | None, float | None] = (0, None, None)
        try:
            async with self._http.stream("POST", url, data=form, headers=headers) as response:
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        chunks = []
                        break
                    chunks.append(chunk)
                hint = response.headers.get("retry-after", "")
                after = float(hint) if hint.isascii() and hint.isdigit() else None
                body = b"".join(chunks) if size <= MAX_RESPONSE_BYTES else None
                result = (response.status_code, body, after)
        except TIMEOUT_ERRORS:
            failure = Transient(TIMEOUT, endpoint=endpoint)
        except TRANSPORT_ERRORS:
            failure = Transient(NETWORK, endpoint=endpoint)
        if failure is not None:
            raise failure
        return result

    async def _once(
        self, endpoint: str, path: str, form: dict[str, str], token: str | None, model: type[M]
    ) -> tuple[M | None, WithingsError | None, float | None]:
        hint: float | None = None
        try:
            status, body, hint = await self._send(endpoint, path, form, token)
            if body is None:
                raise Permanent(TOO_LARGE, endpoint=endpoint, http_status=status)
            return interpret(endpoint, status, body, model), None, None
        except WithingsError as exc:
            return None, exc, hint

    async def _call(
        self,
        path: str,
        action: str,
        model: type[M],
        *,
        token: str | None = None,
        budget: RetryBudget | None = None,
        **params: object,
    ) -> M:
        endpoint = f"{path.strip('/')}:{action}"
        form = {"action": action, **_params(**params)}
        while True:
            result, failure, hint = await self._once(endpoint, path, form, token, model)
            if failure is None:
                if result is None:  # pragma: no cover - _once returns one or the other
                    raise Permanent(UNEXPECTED_BODY, endpoint=endpoint)
                return result
            delay = None
            if isinstance(failure, Transient) and budget is not None:
                delay = budget.next_delay(self._rng, self._monotonic(), hint)
            if delay is None:
                log.warning(
                    "withings call failed",
                    extra={
                        "endpoint": endpoint,
                        "category": failure.category,
                        "error_class": type(failure).__name__,
                        "http_status": failure.http_status,
                        "api_status": failure.api_status,
                    },
                )
                raise failure
            log.info(
                "withings call retried",
                extra={"endpoint": endpoint, "category": failure.category, "delay": delay},
            )
            await self._sleep(delay)

    # ----------------------------------------------------------------------------- OAuth

    async def refresh(self, refresh_token: str) -> m.TokenResponse:
        """``oauth2 requesttoken`` with ``grant_type=refresh_token``. **Never retried** (PLAN
        §4.3 step 5): on an ``ambiguous`` error the caller's recovery decides."""
        return await self._call(
            "/v2/oauth2",
            "requesttoken",
            m.TokenResponse,
            grant_type="refresh_token",
            client_id=self.app.client_id,
            client_secret=self.app.client_secret,
            refresh_token=refresh_token,
        )

    async def _nonce(self, budget: RetryBudget | None) -> str:
        stamp = str(int(self._clock()))
        answer = await self._call(
            "/v2/signature",
            "getnonce",
            m.NonceResponse,
            budget=budget,
            client_id=self.app.client_id,
            timestamp=stamp,
            signature=signature(self.app.client_secret, "getnonce", self.app.client_id, stamp),
        )
        return answer.nonce.get_secret_value()

    async def revoke(self, userid: int, *, budget: RetryBudget | None = None) -> None:
        """``oauth2 revoke`` for one Withings user (disconnect, ADR 0004 §7), signed with a fresh
        nonce. ``budget`` applies to the nonce only: a nonce is single use, so the revoke itself
        is not retried here."""
        nonce = await self._nonce(budget)
        await self._call(
            "/v2/oauth2",
            "revoke",
            m.EmptyBody,
            client_id=self.app.client_id,
            nonce=nonce,
            signature=signature(self.app.client_secret, "revoke", self.app.client_id, nonce),
            userid=userid,
        )

    # ------------------------------------------------------------------------------ data

    async def getmeas(
        self,
        token: str,
        *,
        meastypes: Iterable[int] | None = None,
        category: int | None = None,
        startdate: int | None = None,
        enddate: int | None = None,
        lastupdate: int | None = None,
        offset: int | None = None,
        budget: RetryBudget | None = None,
    ) -> m.MeasureBody:
        return await self._call(
            "/measure", "getmeas", m.MeasureBody, token=token, budget=budget,
            meastypes=meastypes, category=category, startdate=startdate, enddate=enddate,
            lastupdate=lastupdate, offset=offset,
        )  # fmt: skip

    async def heart_list(
        self,
        token: str,
        *,
        startdate: int | None = None,
        enddate: int | None = None,
        offset: int | None = None,
        budget: RetryBudget | None = None,
    ) -> m.HeartListBody:
        return await self._call(
            "/v2/heart", "list", m.HeartListBody, token=token, budget=budget,
            startdate=startdate, enddate=enddate, offset=offset,
        )  # fmt: skip

    async def heart_get(
        self, token: str, signalid: int, *, budget: RetryBudget | None = None
    ) -> m.HeartSignal:
        return await self._call(
            "/v2/heart", "get", m.HeartSignal, token=token, budget=budget, signalid=signalid
        )

    async def getactivity(
        self,
        token: str,
        *,
        startdateymd: date | None = None,
        enddateymd: date | None = None,
        lastupdate: int | None = None,
        offset: int | None = None,
        data_fields: Iterable[str] | None = None,
        budget: RetryBudget | None = None,
    ) -> m.ActivityBody:
        return await self._call(
            "/v2/measure", "getactivity", m.ActivityBody, token=token, budget=budget,
            startdateymd=startdateymd, enddateymd=enddateymd, lastupdate=lastupdate,
            offset=offset, data_fields=data_fields,
        )  # fmt: skip

    async def getintradayactivity(
        self,
        token: str,
        *,
        startdate: int | None = None,
        enddate: int | None = None,
        data_fields: Iterable[str] | None = None,
        budget: RetryBudget | None = None,
    ) -> m.IntradayBody:
        """At most 24 h from ``startdate`` per call (the API's limit)."""
        return await self._call(
            "/v2/measure", "getintradayactivity", m.IntradayBody, token=token, budget=budget,
            startdate=startdate, enddate=enddate, data_fields=data_fields,
        )  # fmt: skip

    async def getworkouts(
        self,
        token: str,
        *,
        startdateymd: date | None = None,
        enddateymd: date | None = None,
        lastupdate: int | None = None,
        offset: int | None = None,
        data_fields: Iterable[str] | None = None,
        budget: RetryBudget | None = None,
    ) -> m.WorkoutsBody:
        return await self._call(
            "/v2/measure", "getworkouts", m.WorkoutsBody, token=token, budget=budget,
            startdateymd=startdateymd, enddateymd=enddateymd, lastupdate=lastupdate,
            offset=offset, data_fields=data_fields,
        )  # fmt: skip

    async def sleep_get(
        self,
        token: str,
        *,
        startdate: int,
        enddate: int,
        data_fields: Iterable[str] | None = None,
        budget: RetryBudget | None = None,
    ) -> m.SleepBody:
        """At most 24 h per call (the API's limit)."""
        return await self._call(
            "/v2/sleep", "get", m.SleepBody, token=token, budget=budget,
            startdate=startdate, enddate=enddate, data_fields=data_fields,
        )  # fmt: skip

    async def sleep_getsummary(
        self,
        token: str,
        *,
        startdateymd: date | None = None,
        enddateymd: date | None = None,
        lastupdate: int | None = None,
        offset: int | None = None,
        data_fields: Iterable[str] | None = None,
        budget: RetryBudget | None = None,
    ) -> m.SleepSummaryBody:
        return await self._call(
            "/v2/sleep", "getsummary", m.SleepSummaryBody, token=token, budget=budget,
            startdateymd=startdateymd, enddateymd=enddateymd, lastupdate=lastupdate,
            offset=offset, data_fields=data_fields,
        )  # fmt: skip

    async def getdevice(self, token: str, *, budget: RetryBudget | None = None) -> m.DevicesBody:
        return await self._call("/v2/user", "getdevice", m.DevicesBody, token=token, budget=budget)

    async def getgoals(self, token: str, *, budget: RetryBudget | None = None) -> m.GoalsBody:
        return await self._call("/v2/user", "getgoals", m.GoalsBody, token=token, budget=budget)

    # ---------------------------------------------------------------------------- notify

    async def notify_subscribe(
        self,
        token: str,
        *,
        callbackurl: str,
        appli: int,
        comment: str | None = None,
        budget: RetryBudget | None = None,
    ) -> None:
        await self._call(
            "/notify", "subscribe", m.EmptyBody, token=token, budget=budget,
            callbackurl=callbackurl, appli=appli, comment=comment,
        )  # fmt: skip

    async def notify_list(
        self, token: str, *, appli: int | None = None, budget: RetryBudget | None = None
    ) -> m.NotifyListBody:
        return await self._call(
            "/notify", "list", m.NotifyListBody, token=token, budget=budget, appli=appli
        )

    async def notify_update(
        self,
        token: str,
        *,
        callbackurl: str,
        appli: int,
        new_callbackurl: str | None = None,
        new_appli: int | None = None,
        comment: str | None = None,
        budget: RetryBudget | None = None,
    ) -> None:
        await self._call(
            "/notify", "update", m.EmptyBody, token=token, budget=budget,
            callbackurl=callbackurl, appli=appli, new_callbackurl=new_callbackurl,
            new_appli=new_appli, comment=comment,
        )  # fmt: skip

    async def notify_revoke(
        self, token: str, *, callbackurl: str, appli: int, budget: RetryBudget | None = None
    ) -> None:
        await self._call(
            "/notify", "revoke", m.EmptyBody, token=token, budget=budget,
            callbackurl=callbackurl, appli=appli,
        )  # fmt: skip

    # ------------------------------------------------------------------------ pagination

    def getmeas_pages(self, token: str, **filters: Any) -> AsyncIterator[m.MeasureBody]:
        """Every page of ``getmeas`` with ``filters`` (``paginate``)."""
        return paginate(lambda offset: self.getmeas(token, offset=offset, **filters))

    def heart_list_pages(self, token: str, **filters: Any) -> AsyncIterator[m.HeartListBody]:
        return paginate(lambda offset: self.heart_list(token, offset=offset, **filters))

    def getactivity_pages(self, token: str, **filters: Any) -> AsyncIterator[m.ActivityBody]:
        return paginate(lambda offset: self.getactivity(token, offset=offset, **filters))

    def getworkouts_pages(self, token: str, **filters: Any) -> AsyncIterator[m.WorkoutsBody]:
        return paginate(lambda offset: self.getworkouts(token, offset=offset, **filters))

    def sleep_getsummary_pages(
        self, token: str, **filters: Any
    ) -> AsyncIterator[m.SleepSummaryBody]:
        return paginate(lambda offset: self.sleep_getsummary(token, offset=offset, **filters))
