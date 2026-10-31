"""``notify`` of the Withings fake: subscribe, get, list, update and revoke.

Synthetic only, standard library only. Called by ``FakeWithings`` after it authenticated the
Bearer token; ``head_status`` is the answer of the HEAD check of the callback URL (Withings
sends a HEAD to it and wants a 2xx before it registers or moves a subscription). A callback URL
is at most 255 characters. Documented in the public docs but not enforced here: HTTPS on a
domain name (a deployment that fails the rule is limited to 10 linked users), because the tests
call back into in-process receivers.

Assumptions (the docs do not say): subscribing the same URL and appli again updates the comment;
several URLs may exist for one appli; ``update`` accepts any combination of the new_* fields.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from withingswire import (
    APPLI_SCOPE,
    STATUS_BAD_CALLBACK,
    STATUS_BAD_PARAMS,
    STATUS_NO_SCOPE,
    STATUS_NO_SUBSCRIPTION,
    SUBSCRIPTION_CANCEL_AFTER,
    Clock,
    Grant,
    Request,
    Response,
    Subscription,
)

if TYPE_CHECKING:
    from withingscore import FakeWithings


class SubscriptionMixin:
    """The subscription bookkeeping that webhook senders and the HEAD check use."""

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

    def callback_to_check(self, request: Request) -> str | None:
        """The callback URL a ``notify subscribe`` or ``update`` asks Withings to HEAD."""
        params = request.params()
        if request.path != "/notify" or params.get("action") not in ("subscribe", "update"):
            return None
        return params.get("new_callbackurl" if params["action"] == "update" else "callbackurl")


def _appli(raw: str | None) -> int | None:
    return int(raw) if raw and raw.isdigit() and int(raw) in APPLI_SCOPE else None


def _profile(sub: Subscription) -> dict[str, Any]:
    return {"appli": sub.appli, "callbackurl": sub.callbackurl, "comment": sub.comment}


def _callback_ok(fake: FakeWithings, url: str, head_status: int | None) -> bool:
    """At most 255 characters, and the HEAD check answered 2xx."""
    status = fake.default_head_status if head_status is None else head_status
    return 0 < len(url) <= 255 and 200 <= status < 300


def notify(
    fake: FakeWithings, grant: Grant, action: str, p: dict[str, str], head_status: int | None
) -> Response:
    ok, err = fake._ok, fake._err
    mine = [
        s for s in fake.subscriptions if (s.client_id, s.userid) == (grant.client_id, grant.userid)
    ]
    url = p.get("callbackurl", "")
    appli = _appli(p.get("appli"))
    wanted = [s for s in mine if s.callbackurl == url and appli in (None, s.appli)]
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
        if not _callback_ok(fake, url, head_status):
            return err(STATUS_BAD_CALLBACK, "callback URL absent or incorrect")
        if wanted:
            wanted[0].comment = p.get("comment", wanted[0].comment)
        else:
            sub = Subscription(grant.client_id, grant.userid, appli, url, p.get("comment", ""))
            fake.subscriptions.append(sub)
        return ok({})
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
    if new_url is not None and not _callback_ok(fake, new_url, head_status):
        return err(STATUS_BAD_CALLBACK, "callback URL absent or incorrect")
    new_appli = _appli(p.get("new_appli"))
    if "new_appli" in p and (new_appli is None or APPLI_SCOPE[new_appli] not in scopes):
        return err(STATUS_BAD_PARAMS, "invalid_params: new_appli")
    sub = wanted[0]
    sub.callbackurl = sub.callbackurl if new_url is None else new_url
    sub.appli = sub.appli if new_appli is None else new_appli
    sub.comment = p.get("comment", sub.comment)
    return ok({})
