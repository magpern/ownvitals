"""``notify`` of the Withings fake: subscribe, get, list, update and revoke.

Synthetic only, standard library only. Called by ``FakeWithings`` after it authenticated the
Bearer token. A callback URL must follow ``withingsrules.callback_url_problem`` (at most 255
characters, HTTPS, port 443, a domain name) and is refused with status 293 otherwise. Withings
then sends a HEAD to it and wants a 2xx before it registers or moves a subscription: ``notify``
returns a ``HeadCheck`` at that point, after the token, the appli, the scope and the URL rules
passed and just before the subscription changes, so a request that was going to fail anyway
causes no HEAD (assumed: the public docs only say the HEAD is part of creating the subscription).

Assumptions (the docs do not say): subscribing the same URL and appli again updates the comment;
several URLs may exist for one appli; ``update`` accepts any combination of the new_* fields.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from withingsrules import callback_url_problem
from withingswire import (
    APPLI_SCOPE,
    STATUS_BAD_CALLBACK,
    STATUS_BAD_PARAMS,
    STATUS_NO_SCOPE,
    STATUS_NO_SUBSCRIPTION,
    SUBSCRIPTION_CANCEL_AFTER,
    Clock,
    Grant,
    Response,
    Subscription,
)

if TYPE_CHECKING:
    from withingscore import FakeWithings


class SubscriptionMixin:
    """The subscription bookkeeping that webhook senders use."""

    subscriptions: list[Subscription]
    clock: Clock

    def webhook_targets(self, userid: int, appli: int) -> list[Subscription]:
        return [s for s in self.subscriptions if s.userid == userid and s.appli == appli]

    def record_delivery(self, sub: Subscription, ok: bool) -> None:
        """Bookkeeping of a webhook delivery: 20 days of failures cancel the subscription."""
        if ok:
            sub.failing_since = None
        elif sub.failing_since is None:
            sub.failing_since = self.clock.now()
        elif self.clock.now() - sub.failing_since >= SUBSCRIPTION_CANCEL_AFTER:
            self.subscriptions.remove(sub)


@dataclass
class HeadCheck:
    """``notify`` waits for the HEAD of ``url``: ``finish(status)`` gives the final response."""

    url: str
    finish: Callable[[int], Response]


def _appli(raw: str | None) -> int | None:
    return int(raw) if raw and raw.isdigit() and int(raw) in APPLI_SCOPE else None


def _profile(sub: Subscription) -> dict[str, Any]:
    return {"appli": sub.appli, "callbackurl": sub.callbackurl, "comment": sub.comment}


def _head_ok(status: int) -> bool:
    return 200 <= status < 300


def notify(
    fake: FakeWithings, grant: Grant, action: str, p: dict[str, str]
) -> Response | HeadCheck:
    ok, err = fake._ok, fake._err
    bad_callback = err(STATUS_BAD_CALLBACK, "callback URL absent or incorrect")
    url = p.get("callbackurl", "")
    appli = _appli(p.get("appli"))

    def owned() -> list[Subscription]:  # read again after a HEAD: the world moved on meanwhile
        return [
            s
            for s in fake.subscriptions
            if (s.client_id, s.userid) == (grant.client_id, grant.userid)
        ]

    def lookup() -> list[Subscription]:
        return [s for s in owned() if s.callbackurl == url and appli in (None, s.appli)]

    mine, wanted = owned(), lookup()
    scopes = set(grant.scope.split(","))
    if action == "list":
        return ok({"profiles": [_profile(s) for s in mine if appli in (None, s.appli)]})
    if p.get("appli") and appli is None:
        return err(STATUS_BAD_PARAMS, "invalid_params: appli")
    if action == "subscribe":
        if appli is None:
            return err(STATUS_BAD_PARAMS, "invalid_params: appli")
        if APPLI_SCOPE[appli] not in scopes:
            return err(STATUS_NO_SCOPE, "scope not authorized for this appli")
        if callback_url_problem(url):
            return bad_callback

        def subscribe(status: int) -> Response:
            if not _head_ok(status):
                return bad_callback
            if existing := lookup():
                existing[0].comment = p.get("comment", existing[0].comment)
            else:
                sub = Subscription(grant.client_id, grant.userid, appli, url, p.get("comment", ""))
                fake.subscriptions.append(sub)
            return ok({})

        return HeadCheck(url, subscribe)
    if action == "get":  # without appli, appli 1 is meant
        found = [s for s in mine if s.callbackurl == url and s.appli == (appli or 1)]
        return (
            ok(_profile(found[0])) if found else err(STATUS_NO_SUBSCRIPTION, "no such subscription")
        )
    if not wanted:
        return err(STATUS_NO_SUBSCRIPTION, "no such subscription")
    if action == "revoke":
        for sub in wanted:
            fake.subscriptions.remove(sub)
        return ok({})
    new_url = p.get("new_callbackurl")  # update
    if new_url is not None and callback_url_problem(new_url):
        return bad_callback
    new_appli = _appli(p.get("new_appli"))
    if "new_appli" in p and (new_appli is None or APPLI_SCOPE[new_appli] not in scopes):
        return err(STATUS_BAD_PARAMS, "invalid_params: new_appli")

    def apply() -> Response:
        if not (current := lookup()):  # revoked while Withings was HEADing the new URL
            return err(STATUS_NO_SUBSCRIPTION, "no such subscription")
        sub = current[0]
        sub.callbackurl = sub.callbackurl if new_url is None else new_url
        sub.appli = sub.appli if new_appli is None else new_appli
        sub.comment = p.get("comment", sub.comment)
        return ok({})

    if new_url is None:
        return apply()
    return HeadCheck(new_url, lambda status: apply() if _head_ok(status) else bad_callback)
