"""Protocol logic of the in-process fake of the public Withings API (synthetic, no network).

``FakeWithings.handle(Request) -> Response`` answers as ``wbsapi.withings.net`` and
``account.withings.com``. It knows nothing about httpx, ASGI or pytest, so ``withingsfake`` (the
httpx transport of the backend tests) and the e2e service (#36) run the same code. Every response
carries ``x-ownvitals-synthetic: true``; the data comes from ``withingsdata`` (seeded, synthetic,
never real health data). Constants, ``Request``/``Response``, ``ManualClock``, ``sign``,
``webhook_form`` and the state records (``Grant``, ``Subscription``, ``Failure``...) are in
``withingswire``; ``notify`` is in ``withingsnotify``.

Behaviour (public spec, ``docs/withings-findings.md`` section 2): authorization code valid 30 s
and single use; access token 3 h; refresh token 1 year; every refresh rotates the pair and the old
refresh token stays valid up to 8 h or until the new access token is first used (a retry with it
after a lost response yields a fresh pair and discards the unused one); errors are HTTP 200 with
a non-zero ``status``; 601 may carry ``body.wait_seconds``; failure injection per request number.

Assumptions (the spec does not say; each is one constant or one place in the code, listed in the
PR): the status codes marked "assumed" in ``withingswire``, the OAuth scope of each endpoint
(``DATA_ROUTES``), and the signed string and unknown-user answer of ``revoke``.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any
from urllib.parse import urlencode

import withingsstore as store
from withingsdata import SyntheticDataset, generate_dataset
from withingsnotify import SubscriptionMixin, notify
from withingsstreams import rng_for
from withingswire import (
    ACCESS_TTL,
    ACCOUNT_HOST,
    API_HOST,
    AUTHORIZE_PATH,
    CODE_TTL,
    DEMO_USERID,
    MASKED,
    NONCE_TTL,
    OK,
    REFRESH_GRACE,
    REFRESH_TTL,
    SCOPES,
    STATUS_BAD_CODE,
    STATUS_BAD_PARAMS,
    STATUS_BAD_REFRESH_TOKEN,
    STATUS_BAD_USERID,
    STATUS_INVALID_TOKEN,
    STATUS_NO_SCOPE,
    STATUS_NOT_IMPLEMENTED,
    SYNTHETIC_HEADER,
    App,
    AuthCode,
    Clock,
    ConnectionDropped,
    Failure,
    FailureKind,
    FakeUser,
    Grant,
    LogEntry,
    ManualClock,
    Request,
    Response,
    Subscription,
    Unroutable,
    sign,
)

# endpoint -> (store function, any one of these scopes). The public docs map scopes to
# notification applis, not to endpoints: this table is assumed from those and the scope texts.
DATA_ROUTES: dict[tuple[str, str], tuple[Callable[..., dict[str, Any]], tuple[str, ...]]] = {
    ("/measure", "getmeas"): (store.getmeas, ("user.metrics",)),
    ("/v2/heart", "list"): (store.heart_list, ("user.metrics",)),
    ("/v2/heart", "get"): (store.heart_get, ("user.metrics",)),
    ("/v2/measure", "getactivity"): (store.getactivity, ("user.activity",)),
    ("/v2/measure", "getintradayactivity"): (store.getintradayactivity, ("user.activity",)),
    ("/v2/measure", "getworkouts"): (store.getworkouts, ("user.activity",)),
    ("/v2/sleep", "get"): (store.sleep_get, ("user.activity",)),
    ("/v2/sleep", "getsummary"): (store.sleep_getsummary, ("user.activity",)),
    ("/v2/user", "getdevice"): (store.getdevice, ("user.info",)),
    ("/v2/user", "getgoals"): (store.getgoals, ("user.info", "user.metrics")),
}


class FakeWithings(SubscriptionMixin):
    def __init__(
        self,
        *,
        clock: Clock | None = None,
        seed: int = 0,
        page_size: int = 100,
        strict_hosts: bool = True,
    ) -> None:
        self.clock: Clock = clock or ManualClock()
        self.page_size = page_size
        self.page_sizes: dict[str, int] = {}  # endpoint key -> page size
        self.strict_hosts = strict_hosts  # False: route by path only (one host, e2e)
        self.apps: dict[str, App] = {}
        self.users: dict[int, FakeUser] = {}
        self.subscriptions: list[Subscription] = []
        self.failures: list[Failure] = []
        self.log: list[LogEntry] = []
        self.request_count = 0
        self.endpoint_counts: dict[str, int] = {}
        self.default_head_status = 200
        self.consenting_userid: int | None = None  # who approves on the authorize page
        self.deny_next_authorization = False
        self._seed = seed
        self._rng = rng_for(seed, "tokens")
        self._codes: dict[str, AuthCode] = {}
        self._grants: dict[tuple[str, int], Grant] = {}
        self._access: dict[str, tuple[Grant, float]] = {}  # token -> (grant, expiry)
        self._refresh: dict[str, Grant] = {}
        self._nonces: dict[str, float] = {}

    # ----------------------------------------------------------------------------- set-up

    def add_app(
        self,
        client_id: str = "synthetic-client",
        client_secret: str = "synthetic-secret",  # noqa: S107
        redirect_uris: Iterable[str] = ("https://app.example.test/api/oauth/withings/callback",),
    ) -> App:
        self.apps[client_id] = App(client_id, client_secret, frozenset(redirect_uris))
        return self.apps[client_id]

    def add_user(
        self,
        userid: int | None = None,
        *,
        dataset: SyntheticDataset | None = None,
        seed: int | None = None,
        **options: Any,
    ) -> FakeUser:
        userid = userid or 1_000_000 + len(self.users) + 1
        seed = self._seed + userid if seed is None else seed
        self.users[userid] = FakeUser(userid, dataset or generate_dataset(seed, **options))
        return self.users[userid]

    def authorize(
        self,
        client_id: str,
        userid: int,
        redirect_uri: str | None = None,
        scope: str | Iterable[str] = SCOPES,
    ) -> str:
        """What the user's consent on the authorize page produces: a code valid for 30 s."""
        app = self.apps[client_id]
        scope = scope if isinstance(scope, str) else ",".join(scope)
        redirect = redirect_uri or next(iter(app.redirect_uris))
        code = self._token()
        self._codes[code] = AuthCode(
            client_id, userid, redirect, scope, self.clock.now() + CODE_TTL
        )
        return code

    def tokens_for(
        self, client_id: str, userid: int, scope: str | Iterable[str] = SCOPES
    ) -> dict[str, Any]:
        """Authorize and exchange in one step; returns the token response ``body``."""
        app = self.apps[client_id]
        code = self.authorize(client_id, userid, scope=scope)
        params = {"client_id": client_id, "client_secret": app.client_secret, "code": code}
        params["redirect_uri"] = self._codes[code].redirect_uri
        return self._exchange_code(app, params).json()["body"]

    def grant(self, client_id: str, userid: int) -> Grant:
        return self._grants[(client_id, userid)]

    def advance(self, seconds: float) -> None:
        """Move the clock forward; only a ``ManualClock`` can be moved."""
        if not isinstance(self.clock, ManualClock):
            raise TypeError("the clock is not a ManualClock")
        self.clock.advance(seconds)

    def inject(
        self,
        kind: FailureKind,
        *,
        at: int | Iterable[int],
        endpoint: str | None = None,
        **opts: Any,
    ) -> Failure:
        failure = Failure(kind, frozenset({at} if isinstance(at, int) else at), endpoint, **opts)
        self.failures.append(failure)
        return failure

    # ---------------------------------------------------------------------------- handling

    def handle(self, request: Request, *, head_status: int | None = None) -> Response:
        """Answer one request. ``head_status`` is the status of the HEAD check of
        ``callback_to_check`` (default ``default_head_status``). May raise ``ConnectionDropped``
        or ``Unroutable``."""
        if self.strict_hosts and request.host not in (API_HOST, ACCOUNT_HOST):
            raise Unroutable(request.host)
        if self.strict_hosts and (request.host == ACCOUNT_HOST) != (request.path == AUTHORIZE_PATH):
            return Response(404, {SYNTHETIC_HEADER: "true"}, b"not found")
        p = request.params()
        key = f"{request.path.strip('/')}:{p.get('action', '')}"
        self.request_count += 1
        count = self.endpoint_counts[key] = self.endpoint_counts.get(key, 0) + 1
        masked = {k: ("***" if k in MASKED else v) for k, v in p.items()}
        entry = LogEntry(self.request_count, key, masked, self.clock.now())
        self.log.append(entry)
        fired = [
            f
            for f in self.failures
            if f.endpoint in (None, key)
            and (self.request_count if f.endpoint is None else count) in f.at
        ]
        for failure in fired:
            failure.fired += 1
            if failure.kind == "mutate" and failure.action is not None:
                failure.action(self)
        replacing = [f for f in fired if f.kind not in ("mutate", "delay")]
        before = next((f for f in replacing if f.when == "before"), None)
        if before is not None:
            return self._failed(before, entry, processed=False)
        response = self._dispatch(request, p, key, head_status)
        response.delay += sum(f.seconds for f in fired if f.kind == "delay")
        after = next((f for f in replacing if f.when == "after"), None)
        if after is not None:
            return self._failed(after, entry, processed=True)
        self._finish(entry, response)
        return response

    def _finish(self, entry: LogEntry, response: Response) -> None:
        entry.http_status = response.status_code
        try:
            entry.api_status = json.loads(response.body).get("status")
        except (ValueError, AttributeError):
            entry.api_status = None

    def _failed(self, failure: Failure, entry: LogEntry, *, processed: bool) -> Response:
        if failure.kind == "drop":
            entry.outcome = "dropped"
            raise ConnectionDropped(processed)
        if failure.kind == "status":
            response = self._err(failure.status, "injected failure", failure.wait_seconds)
        elif failure.kind == "http":
            response = Response(failure.http_status, {"retry-after": "1"}, b"injected failure")
        else:  # garbage: an HTTP 200 whose body is not JSON
            response = Response(200, {"content-type": "text/html"}, b"<html>upstream error")
        response.headers[SYNTHETIC_HEADER] = "true"
        self._finish(entry, response)
        return response

    def _dispatch(
        self, request: Request, p: dict[str, str], key: str, head_status: int | None
    ) -> Response:
        path, action = request.path, p.get("action", "")
        if path == AUTHORIZE_PATH:
            return self._authorize_page(p)
        if (path, action) in DATA_ROUTES:
            return self._data(request, p, key)
        if path == "/v2/oauth2" and action == "requesttoken":
            return self._requesttoken(p)
        if path == "/v2/oauth2" and action in ("revoke", "getdemoaccess"):
            return self._signed_oauth(action, p)
        if path == "/v2/signature" and action == "getnonce":
            return self._getnonce(p)
        if path == "/notify" and action in ("subscribe", "get", "list", "update", "revoke"):
            return self._notify(request, action, p, head_status)
        return self._err(STATUS_NOT_IMPLEMENTED, "wrong action or wrong webservice")

    # ----------------------------------------------------------------------------- helpers

    def _token(self, bits: int = 160) -> str:
        return f"{self._rng.getrandbits(bits):0{bits // 4}x}"

    @staticmethod
    def _json(payload: Mapping[str, Any]) -> Response:
        headers = {"content-type": "application/json", SYNTHETIC_HEADER: "true"}
        return Response(200, headers, json.dumps(payload, separators=(",", ":")).encode())

    @staticmethod
    def _ok(body: Mapping[str, Any]) -> Response:
        return FakeWithings._json({"status": OK, "body": body})

    @staticmethod
    def _err(status: int, text: str, wait_seconds: int | None = None) -> Response:
        payload: dict[str, Any] = {"status": status, "error": text}
        if wait_seconds is not None:
            payload["body"] = {"wait_seconds": wait_seconds}
        return FakeWithings._json(payload)

    def _app_signed(self, action: str, p: dict[str, str]) -> App | None:
        """The app of a signed call; consumes the nonce. Signs ``action,client_id,nonce``."""
        app = self.apps.get(p.get("client_id", ""))
        nonce = p.get("nonce", "")
        expires = self._nonces.pop(nonce, 0.0)
        if app is None or expires <= self.clock.now():
            return None
        expected = sign(app.client_secret, action, app.client_id, nonce)
        return app if hmac.compare_digest(expected, p.get("signature", "")) else None

    # ------------------------------------------------------------------------------ OAuth

    def _authorize_page(self, p: dict[str, str]) -> Response:
        app = self.apps.get(p.get("client_id", ""))
        redirect, state = p.get("redirect_uri", ""), p.get("state", "")
        scopes = [s for s in p.get("scope", "").split(",") if s]
        valid = (
            app is not None and redirect in app.redirect_uris and p.get("response_type") == "code"
        )
        if (
            not valid
            or not state
            or not scopes
            or any(s not in SCOPES for s in scopes)
            or not self.users
        ):
            return Response(400, {"content-type": "text/html"}, b"<h1>invalid request</h1>")
        assert app is not None
        if self.deny_next_authorization:
            self.deny_next_authorization = False
            query = {"error": "access_denied", "state": state}
        else:
            userid = self.consenting_userid or next(iter(self.users))
            code = self.authorize(app.client_id, userid, redirect, scopes)
            query = {"code": code, "state": state}
        location = f"{redirect}{'&' if '?' in redirect else '?'}{urlencode(query)}"
        return Response(302, {"location": location, SYNTHETIC_HEADER: "true"})

    def _requesttoken(self, p: dict[str, str]) -> Response:
        app = self.apps.get(p.get("client_id", ""))
        if app is None or not hmac.compare_digest(app.client_secret, p.get("client_secret", "")):
            return self._err(STATUS_BAD_PARAMS, "invalid_params: unknown client or secret")
        if p.get("grant_type") == "authorization_code":
            return self._exchange_code(app, p)
        if p.get("grant_type") == "refresh_token":
            return self._refresh_token(app, p)
        return self._err(STATUS_BAD_PARAMS, "invalid_params: grant_type")

    def _exchange_code(self, app: App, p: dict[str, str]) -> Response:
        code = self._codes.get(p.get("code", ""))
        if (
            code is None
            or code.used
            or code.client_id != app.client_id
            or code.redirect_uri != p.get("redirect_uri")
            or self.clock.now() >= code.expires_at
        ):
            return self._err(STATUS_BAD_CODE, "invalid_params: authorization code")
        code.used = True
        grant = self._grants.setdefault(
            (app.client_id, code.userid), Grant(app.client_id, code.userid)
        )
        self._drop_tokens(grant)  # a new authorization replaces every earlier token
        grant.scope = code.scope
        self._rotate(grant, None, 0.0)
        return self._ok(self._token_body(grant))

    def _refresh_token(self, app: App, p: dict[str, str]) -> Response:
        token = p.get("refresh_token", "")
        grant = self._refresh.get(token)
        if grant is None or grant.client_id != app.client_id:
            return self._err(STATUS_BAD_REFRESH_TOKEN, "invalid refresh token")
        now = self.clock.now()
        if token == grant.refresh and now < grant.refresh_expires:
            previous, until = token, now + REFRESH_GRACE
        elif token == grant.previous_refresh and now < grant.previous_valid_until:
            # a retry after a lost response: the pair it never saw is discarded, unused
            self._access.pop(grant.access, None)
            self._refresh.pop(grant.refresh, None)
            previous, until = token, grant.previous_valid_until
        else:
            return self._err(STATUS_BAD_REFRESH_TOKEN, "invalid refresh token")
        self._rotate(grant, previous, until)
        return self._ok(self._token_body(grant))

    def _rotate(self, grant: Grant, previous: str | None, valid_until: float) -> None:
        """Issue a new pair. Of the earlier refresh tokens only ``previous`` stays valid."""
        now = self.clock.now()
        if grant.previous_refresh and grant.previous_refresh != previous:
            self._refresh.pop(grant.previous_refresh, None)
        grant.generation += 1
        grant.access, grant.access_expires, grant.access_used = (
            self._token(),
            now + ACCESS_TTL,
            False,
        )
        grant.refresh, grant.refresh_expires = self._token(), now + REFRESH_TTL
        grant.previous_refresh, grant.previous_valid_until = previous, valid_until
        self._access[grant.access] = (grant, grant.access_expires)
        self._refresh[grant.refresh] = grant
        if previous:
            self._refresh[previous] = grant

    def _drop_tokens(self, grant: Grant) -> None:
        self._access = {t: e for t, e in self._access.items() if e[0] is not grant}
        self._refresh = {t: g for t, g in self._refresh.items() if g is not grant}

    def _token_body(self, grant: Grant) -> dict[str, Any]:
        return {
            "userid": grant.userid,
            "access_token": grant.access,
            "refresh_token": grant.refresh,
            "expires_in": ACCESS_TTL,
            "scope": grant.scope,
            "csrf_token": self._token(128),
            "token_type": "Bearer",
        }

    def _getnonce(self, p: dict[str, str]) -> Response:
        app = self.apps.get(p.get("client_id", ""))
        if app is None:
            return self._err(STATUS_BAD_PARAMS, "invalid_params: client_id")
        expected = sign(app.client_secret, "getnonce", app.client_id, p.get("timestamp", ""))
        if not hmac.compare_digest(expected, p.get("signature", "")):
            return self._err(STATUS_BAD_PARAMS, "invalid_params: signature")
        nonce = self._token()
        self._nonces[nonce] = self.clock.now() + NONCE_TTL
        return self._ok({"nonce": nonce})

    def _signed_oauth(self, action: str, p: dict[str, str]) -> Response:
        app = self._app_signed(action, p)
        if app is None:
            return self._err(STATUS_BAD_PARAMS, "invalid_params: nonce or signature")
        if action == "getdemoaccess":
            scope = p.get("scope_oauth2", "")
            if not scope or any(s not in SCOPES for s in scope.split(",")):
                return self._err(STATUS_BAD_PARAMS, "invalid_params: scope_oauth2")
            if DEMO_USERID not in self.users:
                self.add_user(DEMO_USERID, seed=DEMO_USERID)
            body = self.tokens_for(app.client_id, DEMO_USERID, scope)
            return self._ok({k: body[k] for k in ("access_token", "refresh_token", "expires_in")})
        userid = p.get("userid", "")
        grant = self._grants.pop((app.client_id, int(userid) if userid.isdigit() else -1), None)
        if grant is None:  # assumed: an unknown or already revoked user is an error
            return self._err(STATUS_BAD_USERID, "invalid userid")
        self._drop_tokens(grant)
        self.subscriptions[:] = [
            s
            for s in self.subscriptions
            if (s.client_id, s.userid) != (grant.client_id, grant.userid)
        ]
        return self._ok({})

    # --------------------------------------------------------------------- authenticated

    def _authenticate(self, request: Request, scopes: tuple[str, ...]) -> Grant | Response:
        """The grant behind the Bearer token. Earlier access tokens stay valid until their own
        3 h are over; the first use of the newest one ends the previous refresh token's life."""
        token = request.bearer() or ""
        entry = self._access.get(token)
        if entry is None:
            return self._err(STATUS_INVALID_TOKEN, "invalid_token: unknown access token")
        grant, expires = entry
        if self.clock.now() >= expires:
            return self._err(STATUS_INVALID_TOKEN, "invalid_token: expired")
        if not set(grant.scope.split(",")) & set(scopes):
            return self._err(STATUS_NO_SCOPE, "scope not authorized for this endpoint")
        if token == grant.access and not grant.access_used:
            grant.access_used = True
            if grant.previous_refresh:
                self._refresh.pop(grant.previous_refresh, None)
                grant.previous_refresh = None
        return grant

    def _data(self, request: Request, p: dict[str, str], key: str) -> Response:
        function, scopes = DATA_ROUTES[(request.path, p["action"])]
        grant = self._authenticate(request, scopes)
        if isinstance(grant, Response):
            return grant
        size = self.page_sizes.get(key, self.page_size)
        try:
            body = function(
                self.users[grant.userid].data, p, page_size=size, now=int(self.clock.now())
            )
        except store.InvalidParams as error:
            return self._err(STATUS_BAD_PARAMS, f"invalid_params: {error}")
        return self._ok(body)

    # ---------------------------------------------------------------------------- notify

    def _notify(
        self, request: Request, action: str, p: dict[str, str], head_status: int | None
    ) -> Response:
        grant = self._authenticate(request, SCOPES)
        if isinstance(grant, Response):
            return grant
        return notify(self, grant, action, p, head_status)
