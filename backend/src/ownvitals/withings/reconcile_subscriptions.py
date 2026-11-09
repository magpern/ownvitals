"""Nightly subscription repair (PLAN section 4.4), run after reconciliation (``reconcile.py``).

List, then revoke, update or subscribe. Callback URLs are never stored and never logged.
"""

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from ownvitals.ingest.calls import Account, Classified, limited
from ownvitals.jobs import JobFailure
from ownvitals.jobs.run import JobRun
from ownvitals.jobs.withings import lane_for
from ownvitals.withings.client import WithingsClient
from ownvitals.withings.errors import RetryBudget
from ownvitals.withings.subscriptions import APPLIS

# The logger keeps the name it had before this module was split out of ``reconcile.py``.
logger = logging.getLogger("ownvitals.withings.reconcile")


_APPLI_SCOPE = {
    1: "user.metrics", 2: "user.metrics", 4: "user.metrics", 16: "user.activity",
    44: "user.activity", 46: "user.info", 54: "user.metrics", 55: "user.metrics",
    58: "user.metrics", 62: "user.metrics", 63: "user.metrics",
}  # fmt: skip
_RESUBSCRIBE = "resubscribe"


@dataclass(frozen=True, slots=True)
class RepairResult:
    revoked: int
    updated: int
    subscribed: int


def marked_for_resubscribe(public: Mapping[str, Any]) -> bool:
    """Whether ``connections.public`` carries the resubscribe mark (a strict ``True``).

    Nothing writes the mark yet: the secret-rotation flow that sets it is a later issue, so a
    caller passes ``resubscribe`` explicitly until then.
    """
    return public.get(_RESUBSCRIBE) is True


def _under_base(url: str, base: str) -> bool:
    root = base.rstrip("/")
    parsed, base_parts = urlsplit(url), urlsplit(root)
    prefix = base_parts.path.rstrip("/") + "/"
    return (
        parsed.scheme == base_parts.scheme
        and parsed.netloc == base_parts.netloc
        and (parsed.path == base_parts.path or parsed.path.startswith(prefix))
    )


def _wanted(scopes: tuple[str, ...], applis: tuple[int, ...]) -> tuple[int, ...]:
    granted = set(scopes)
    return tuple(appli for appli in applis if _APPLI_SCOPE.get(appli) in granted)


async def _notify(
    job: JobRun, account: Account, lane: int, call: Callable[[], Awaitable[Any]]
) -> Any:
    result = await limited(job, account, lane, call)
    if isinstance(result, Classified):
        raise JobFailure("not_connected")
    return result


async def _revoke(
    job: JobRun,
    account: Account,
    client: WithingsClient,
    lane: int,
    budget: RetryBudget,
    appli: int,
    url: str,
) -> None:
    await _notify(
        job,
        account,
        lane,
        lambda appli=appli, url=url: client.notify_revoke(
            account.access_token, callbackurl=url, appli=appli, budget=budget
        ),
    )


async def repair_subscriptions(
    job: JobRun,
    account: Account,
    client: WithingsClient,
    *,
    callback_base: str,
    current_url: str,
    resubscribe: bool,
    applis: tuple[int, ...] = APPLIS,
    lane: int | None = None,
) -> RepairResult:
    """List, then revoke, update or subscribe. URLs are not stored and not logged."""
    if not _under_base(current_url, callback_base):
        # Only a URL under our own base may be subscribed or swapped in; refuse before any call.
        raise JobFailure("invalid_input")
    lane = lane_for("withings_reconcile", "scheduler") if lane is None else lane
    budget = RetryBudget(retries=2)
    listed = await _notify(
        job, account, lane, lambda: client.notify_list(account.access_token, budget=budget)
    )
    on_current: set[int] = set()
    stale: list[tuple[int, str]] = []
    for profile in listed.profiles:
        url, appli = profile.callbackurl.get_secret_value(), int(profile.appli)
        if not _under_base(url, callback_base):
            continue
        if url == current_url:
            on_current.add(appli)
        else:
            stale.append((appli, url))
    wanted = _wanted(account.scopes, applis)
    revoked = updated = subscribed = 0
    grouped: dict[int, list[str]] = {}
    for appli, url in stale:
        grouped.setdefault(appli, []).append(url)
    if resubscribe:
        for appli, urls in grouped.items():
            for url in urls:
                await _revoke(job, account, client, lane, budget, appli, url)
                revoked += 1
    else:
        for appli, urls in grouped.items():
            if appli in on_current or appli not in wanted:
                for url in urls:
                    await _revoke(job, account, client, lane, budget, appli, url)
                    revoked += 1
                continue
            first, *rest = urls
            await _notify(
                job,
                account,
                lane,
                lambda appli=appli, first=first: client.notify_update(
                    account.access_token,
                    callbackurl=first,
                    appli=appli,
                    new_callbackurl=current_url,
                    budget=budget,
                ),
            )
            updated += 1
            for url in rest:
                await _revoke(job, account, client, lane, budget, appli, url)
                revoked += 1
    for appli in wanted:
        if appli in on_current or (not resubscribe and appli in grouped):
            continue
        await _notify(
            job,
            account,
            lane,
            lambda appli=appli: client.notify_subscribe(
                account.access_token, callbackurl=current_url, appli=appli, budget=budget
            ),
        )
        subscribed += 1
    logger.info(
        "withings subscriptions repaired",
        extra={
            "connection_id": str(account.connection_id), "revoked": revoked,
            "updated": updated, "subscribed": subscribed, "resubscribe": resubscribe,
        },
    )  # fmt: skip
    return RepairResult(revoked, updated, subscribed)
